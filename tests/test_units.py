from google.genai import types

from app.agent import dump_history, load_history, trim_history
from app.formatting import split_message, to_whatsapp
from app.parsing import parse_webhook
from app.security import redact_pii, verify_signature

from .conftest import APP_SECRET, sign, text_payload


def test_signature():
    body = b'{"a":1}'
    assert verify_signature(body, sign(body), APP_SECRET)
    assert not verify_signature(body, sign(b"otro"), APP_SECRET)
    assert not verify_signature(body, None, APP_SECRET)


def test_redact_pii():
    out = redact_pii("Soy ana@mail.com, tel +34 600 123 456, tarjeta 4111 1111 1111 1111")
    assert "ana@mail.com" not in out and "600 123" not in out and "4111" not in out


def test_parse_text_button_image_status():
    msgs, _ = parse_webhook(text_payload("346", "m1", "hola"))
    assert msgs[0].text == "hola" and msgs[0].profile_name == "Ana"

    body = {"entry": [{"changes": [{"field": "messages", "value": {
        "messages": [
            {"from": "346", "id": "m2", "type": "interactive",
             "interactive": {"type": "button_reply", "button_reply": {"id": "human", "title": "Hablar"}}},
            {"from": "346", "id": "m3", "type": "image",
             "image": {"id": "MEDIA", "mime_type": "image/jpeg", "caption": "roto"}},
        ],
        "statuses": [{"id": "w1", "recipient_id": "346", "status": "failed", "errors": [{"code": 131047}]}],
    }}]}]}
    msgs, statuses = parse_webhook(body)
    assert msgs[0].button_id == "human"
    assert msgs[1].media_id == "MEDIA" and msgs[1].text == "roto"
    assert statuses[0].status == "failed"


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
    assert restored[0].parts[1].inline_data is None


def test_trim_starts_on_real_user_turn():
    fr = types.Content(role="user", parts=[types.Part.from_function_response(name="x", response={})])
    u = types.Content(role="user", parts=[types.Part.from_text(text="hola")])
    m = types.Content(role="model", parts=[types.Part.from_text(text="hey")])
    out = trim_history([u, m, fr, m, u, m], 4)
    assert out[0] is u and len(out) == 2


def test_thinking_budget_only_applies_to_25_models():
    from app.agent import Agent

    agent = Agent(object(), "gemini-3.5-flash", thinking_budget=0)  # type: ignore[arg-type]
    assert agent.config_for("gemini-3.5-flash").thinking_config is None
    assert agent.config_for("gemini-2.5-flash").thinking_config.thinking_budget == 0
