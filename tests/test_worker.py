import asyncio

import pytest

from app import prompts
from app.parsing import IncomingMessage

from .conftest import make_worker, model_call, model_text

pytestmark = pytest.mark.asyncio

WA = "34600000000"


def msg(mid: str, text: str = "", **kw) -> IncomingMessage:
    return IncomingMessage(wa_id=WA, message_id=mid, type=kw.pop("type", "text"), text=text, **kw)


async def test_order_flow_uses_tool_and_discloses_ai(store, wa, handoff):
    worker, client = make_worker(store, wa, handoff, [
        model_call("get_order_status", order_id="TX-123456"),
        model_text("Tu pedido TX-123456 llega el **5 de nov**."),
    ])
    await worker.handle_incoming(msg("m1", "¿dónde está mi pedido TX-123456?"))

    assert len(wa.sent) == 1
    sent = wa.sent[0]["text"]
    assert sent.startswith(prompts.AI_DISCLOSURE)
    assert "llega el **5 de nov**" in sent  # el cliente real convierte ** -> *
    # La segunda llamada al modelo incluye la respuesta real de la herramienta.
    fr = client.models.calls[1]["contents"][-1].parts[0].function_response
    assert fr.name == "get_order_status" and fr.response["status"] == "en_reparto"

    session = await store.load(WA)
    assert session.disclosed and len(session.history) == 4  # user, call, response, text


async def test_dedupe_ignores_redelivery(store, wa, handoff):
    worker, client = make_worker(store, wa, handoff, [model_text("hola")])
    await worker.handle_incoming(msg("m1", "hola"))
    await worker.handle_incoming(msg("m1", "hola"))
    assert len(client.models.calls) == 1 and len(wa.sent) == 1


async def test_burst_is_merged_into_one_llm_call(store, wa, handoff):
    worker, client = make_worker(store, wa, handoff, [model_text("¿Qué pedido?")])
    await asyncio.gather(
        worker.handle_incoming(msg("m1", "hola")),
        worker.handle_incoming(msg("m2", "quiero devolver")),
        worker.handle_incoming(msg("m3", "algo")),
    )
    assert len(client.models.calls) == 1
    user_text = client.models.calls[0]["contents"][-1].parts[0].text
    assert user_text == "hola\nquiero devolver\nalgo"


async def test_offer_options_sends_buttons(store, wa, handoff):
    worker, _ = make_worker(store, wa, handoff, [
        model_call("offer_options", text="¿Qué necesitas?", options=["Mi pedido", "Devolución"]),
    ])
    await worker.handle_incoming(msg("m1", "hola"))
    assert wa.sent[0]["type"] == "buttons"
    assert [o["title"] for o in wa.sent[0]["options"]] == ["Mi pedido", "Devolución"]


async def test_escalation_pauses_bot(store, wa, handoff):
    worker, client = make_worker(store, wa, handoff, [
        model_call("escalate_to_human", summary="Cliente molesto por retraso", priority="alta"),
    ])
    await worker.handle_incoming(msg("m1", "esto es un desastre"))
    assert handoff.events[0]["type"] == "handoff" and handoff.events[0]["priority"] == "alta"
    assert prompts.HANDOFF_MESSAGE in wa.sent[0]["text"]

    await worker.handle_incoming(msg("m2", "¿hola?"))
    assert len(client.models.calls) == 1  # el bot ya no responde
    assert handoff.events[1]["type"] == "message"


async def test_human_button_escalates_without_llm(store, wa, handoff):
    worker, client = make_worker(store, wa, handoff, [])
    await worker.handle_incoming(msg("m1", "Hablar con agente", type="interactive", button_id="human"))
    assert not client.models.calls and handoff.events[0]["type"] == "handoff"


async def test_commands_stop_and_delete(store, wa, handoff):
    worker, client = make_worker(store, wa, handoff, [])
    await worker.handle_incoming(msg("m1", "Stop"))
    assert await store.is_opted_out(WA)
    await worker.handle_incoming(msg("m2", "borrar mis datos"))
    assert wa.sent[-1]["text"] == prompts.DELETE_CONFIRMED
    assert not client.models.calls


async def test_fallback_model_then_apology(store, wa, handoff):
    worker, client = make_worker(store, wa, handoff,
                                 [RuntimeError("503"), model_text("ok")], fallback="backup-model")
    await worker.handle_incoming(msg("m1", "hola"))
    assert client.models.calls[1]["model"] == "backup-model"

    worker2, _ = make_worker(store, wa, handoff, [RuntimeError("a"), RuntimeError("b")], fallback="backup")
    await worker2.handle_incoming(msg("m2", "hola"))
    assert wa.sent[-1]["text"] == prompts.FALLBACK_ERROR


async def test_image_is_sent_to_model(store, wa, handoff):
    worker, client = make_worker(store, wa, handoff, [model_text("Veo que llegó roto.")])
    await worker.handle_incoming(msg("m1", "llegó así", type="image", media_id="MID", mime_type="image/jpeg"))
    parts = client.models.calls[0]["contents"][-1].parts
    assert parts[0].text == "llegó así" and parts[1].inline_data.mime_type == "image/jpeg"
    session = await store.load(WA)
    assert "inline_data" not in str(session.history)


async def test_ai_error_is_visible_in_panel_but_not_to_customer(store, wa, handoff):
    from google.genai import errors

    not_found = errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND",
                                                   "message": "models/gemini-x is not found"}})
    worker, _ = make_worker(store, wa, handoff, [not_found, RuntimeError("quota")], fallback="backup")
    await worker.handle_incoming(msg("m1", "hola"))
    assert wa.sent[-1]["text"] == prompts.FALLBACK_ERROR
    system = [m["text"] for m in await store.transcript(WA) if m["role"] == "system"]
    assert "404" in system[0] and "NOT_FOUND" in system[0] and "quota" in system[0]
