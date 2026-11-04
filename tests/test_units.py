from datetime import datetime

import pytest
from google.genai import types

from app import auth, states
from app.agent import Agent, TurnSpec, dump_history, load_history, trim_history
from app.areas import KeywordClassifier, area
from app.control import ControlCenter, describe_opening
from app.formatting import split_message, to_whatsapp
from app.parsing import parse_webhook
from app.prompts import build_system_prompt
from app.security import redact_pii, verify_signature

from .conftest import APP_SECRET, sign, text_payload


def test_signature():
    body = b'{"a":1}'
    assert verify_signature(body, sign(body), APP_SECRET)
    assert not verify_signature(body, sign(b"otro"), APP_SECRET)
    assert not verify_signature(body, None, APP_SECRET)


def test_redact_pii():
    out = redact_pii("Soy ana@mail.com, tel +593 99 123 4567, tarjeta 4111 1111 1111 1111")
    assert "ana@mail.com" not in out and "123 4567" not in out and "4111" not in out


def test_parse_messages_statuses_and_pricing():
    msgs, _ = parse_webhook(text_payload("346", "m1", "hola"))
    assert msgs[0].text == "hola" and msgs[0].profile_name == "Ana"
    body = {"entry": [{"changes": [{"field": "messages", "value": {
        "messages": [{"from": "346", "id": "m2", "type": "interactive",
                      "interactive": {"type": "button_reply", "button_reply": {"id": "human", "title": "Hablar"}}},
                     {"from": "346", "id": "m3", "type": "image",
                      "image": {"id": "MEDIA", "mime_type": "image/jpeg", "caption": "comprobante"}}],
        "statuses": [{"id": "w1", "recipient_id": "346", "status": "delivered",
                      "pricing": {"billable": True, "category": "utility"}}]}}]}]}
    msgs, statuses = parse_webhook(body)
    assert msgs[0].button_id == "human" and msgs[1].media_id == "MEDIA"
    assert statuses[0].pricing["category"] == "utility"


def test_formatting():
    assert to_whatsapp("## Hola\n**negrita** y [web](https://x.com)") == "*Hola*\n*negrita* y web: https://x.com"
    chunks = split_message("a" * 5000 + "\n\n" + "b" * 10, limit=4096)
    assert all(len(c) <= 4096 for c in chunks) and "".join(chunks).count("b") == 10


def test_history_roundtrip_keeps_signature_and_strips_media():
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text="mira"),
                                          types.Part.from_bytes(data=b"img", mime_type="image/png")]),
        types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(name="x", args={}), thought_signature=b"sig")]),
    ]
    restored = load_history(dump_history(contents, 30))
    assert restored[1].parts[0].thought_signature == b"sig"
    assert restored[0].parts[1].text == "[El usuario envió un image]"


def test_trim_starts_on_real_user_turn():
    fr = types.Content(role="user", parts=[types.Part.from_function_response(name="x", response={})])
    u = types.Content(role="user", parts=[types.Part.from_text(text="hola")])
    m = types.Content(role="model", parts=[types.Part.from_text(text="hey")])
    out = trim_history([u, m, fr, m, u, m], 4)
    assert out[0] is u and len(out) == 2


def test_thinking_budget_only_for_25_models():
    agent = Agent(object(), "gemini-3.5-flash", thinking_budget=0)  # type: ignore[arg-type]
    spec = TurnSpec(system_prompt="x", declarations=area("general").declarations, allowed=set())
    assert agent.config_for("gemini-3.5-flash", spec).thinking_config is None
    assert agent.config_for("gemini-2.5-flash", spec).thinking_config.thinking_budget == 0


def test_state_machine():
    assert states.next_state("bot", "escalate") == "pendiente_humano"
    assert states.next_state("pendiente_humano", "take") == "asignada"
    assert states.next_state("asignada", "close") == "cerrada"
    assert states.next_state("cerrada", "customer_message") == "bot"
    assert states.next_state("asignada", "customer_message") == "asignada"
    with pytest.raises(states.InvalidTransition):
        states.next_state("pendiente_humano", "escalate")


def test_business_hours_and_holidays():
    cc = ControlCenter()
    tz = cc.tz
    assert cc.is_open(datetime(2026, 11, 4, 10, 0, tzinfo=tz))  # miércoles laborable
    assert not cc.is_open(datetime(2026, 11, 2, 10, 0, tzinfo=tz))  # Día de Difuntos
    assert not cc.is_open(datetime(2026, 11, 8, 10, 0, tzinfo=tz))  # domingo
    nxt = cc.next_opening(datetime(2026, 11, 1, 20, 0, tzinfo=tz))  # domingo noche, lunes y martes feriados
    assert nxt.date().isoformat() == "2026-11-04" and nxt.hour == 8
    assert "fuera de horario" in describe_opening(cc, datetime(2026, 11, 7, 15, 0, tzinfo=tz))
    assert cc.account_is_ours("2200-123-456") and not cc.account_is_ours("999")


async def test_keyword_classifier():
    k = KeywordClassifier()
    assert (await k.classify("no tengo internet", "", False)).area == "soporte"
    assert (await k.classify("cuánto debo?", "", False)).area == "cobros"
    assert (await k.classify("Contrato C-1001", "cobros", False)).area == "cobros"  # selección: sigue el tema
    assert (await k.classify("", "", True)).area == "cobros"  # una foto suele ser un comprobante
    assert (await k.classify("quiero hablar con un asesor", "", False)).quiere_humano


def test_prompt_contains_rules_facts_and_date():
    cc = ControlCenter(months_collectable=2)
    now = datetime(2026, 11, 4, 9, 30, tzinfo=cc.tz)
    prompt, version = build_system_prompt("cobros", cc, ["Envía un solo mensaje por respuesta."], now=now)
    assert "PAALNET" in prompt and "solo los 2 meses" in prompt and "2026-11-04 09:30" in prompt
    assert "1. Envía un solo mensaje" in prompt and version == "cobros@v1+v1+r1"


def test_passwords_and_tokens():
    h = auth.hash_password("una-clave-larga")
    assert auth.verify_password("una-clave-larga", h) and not auth.verify_password("otra", h)
    token = auth.issue_token(7, "s")
    assert auth.read_token(token, "s") == 7 and auth.read_token(token, "otro") is None
    assert auth.read_token(auth.issue_token(7, "s", ttl=-1), "s") is None
    assert auth.can("owner", "users.manage") and not auth.can("asesor", "control.manage")
