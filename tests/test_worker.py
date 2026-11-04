import asyncio

import pytest
from google.genai import errors

from app import prompts
from app.control import ControlCenter
from app.parsing import IncomingMessage

from .conftest import LUIS, MARIA, make_user, make_worker, model_call, model_text, receipt

pytestmark = pytest.mark.asyncio


def msg(mid, text="", wa=MARIA, **kw):
    return IncomingMessage(wa_id=wa, message_id=mid, type=kw.pop("type", "text"), text=text, **kw)


async def state(repo, wa=MARIA):
    return (await repo.get_conversation(wa)).state


async def test_debt_flow_uses_area_tools_and_discloses_ai(store, repo, channel, isp, handoff):
    worker, client = make_worker(store, repo, channel, isp, handoff, [
        model_call("buscar_cliente"),
        model_call("consultar_deuda", contrato="C-1001"),
        model_text("Tu contrato C-1001 debe *$50,00*."),
    ])
    await worker.handle_incoming(msg("m1", "cuánto debo del C-1001"))
    assert channel.sent[0]["text"].startswith(prompts.ai_disclosure(ControlCenter()))
    assert "debe *$50,00*" in channel.sent[0]["text"]
    call = client.models.calls[0]
    declared = {d.name for d in call["config"].tools[0].function_declarations}
    assert "consultar_deuda" in declared and "reiniciar_ont" not in declared  # solo herramientas de cobros
    assert "asistente de cobros" in call["config"].system_instruction
    fr = client.models.calls[2]["contents"][-1].parts[0].function_response
    assert fr.response["total_usd"] == 50.0
    view = await repo.conversation_view(MARIA)
    assert view["meta"]["area"] == "cobros" and view["messages"][-1]["prompt_version"].startswith("cobros@")


async def test_receipt_payment_end_to_end(store, repo, channel, isp, handoff):
    worker, _ = make_worker(store, repo, channel, isp, handoff, [
        model_call("validar_comprobante"), model_call("buscar_cliente"),
        model_call("registrar_pago", contrato="C-1001"), model_text("Pago registrado."),
    ])
    channel.media["MID"] = (receipt(), "image/jpeg")
    await worker.handle_incoming(msg("m1", "", type="image", media_id="MID", mime_type="image/jpeg"))
    assert "Pago registrado" in channel.sent[-1]["text"]
    assert isp.nc.payments[0]["amount_cents"] == 5000
    assert (await store.metrics(1))[-1]["payments_cents"] == 5000


async def test_dedupe_and_burst(store, repo, channel, isp, handoff):
    worker, client = make_worker(store, repo, channel, isp, handoff, [model_text("¿En qué te ayudo?")])
    await asyncio.gather(worker.handle_incoming(msg("m1", "hola")), worker.handle_incoming(msg("m2", "buenas")),
                         worker.handle_incoming(msg("m2", "buenas")))
    assert len(client.models.calls) == 1 and len(channel.sent) == 1
    assert client.models.calls[0]["contents"][-1].parts[0].text == "hola\nbuenas"


async def test_escalation_states_and_return_to_bot(store, repo, channel, isp, handoff):
    worker, client = make_worker(store, repo, channel, isp, handoff, [
        model_call("escalate_to_human", summary="Reclamo de cobro", priority="alta"),
        model_text("Hola de nuevo, ¿en qué te ayudo?"),
    ])
    await worker.handle_incoming(msg("m1", "me cobraron doble, esto es un robo"))
    assert await state(repo) == "pendiente_humano" and handoff.events[0]["priority"] == "alta"
    await worker.handle_incoming(msg("m2", "¿hola?"))
    assert len(client.models.calls) == 1  # con humano, la IA no responde

    asesor = await make_user(repo, "marta@isp.ec", "asesor")
    await worker.take(MARIA, asesor)
    assert (await repo.get_conversation(MARIA)).assigned_to == asesor.id
    await worker.send_agent_message(MARIA, "Hola, soy Marta. Ya lo reviso.", asesor)
    await worker.close(MARIA, asesor)
    assert await state(repo) == "cerrada"
    await worker.handle_incoming(msg("m3", "gracias, otra consulta"))
    assert await state(repo) == "bot" and channel.sent[-1]["text"].startswith("Hola de nuevo")
    session = await store.load(MARIA)
    assert any("Mensaje del asesor Marta" in str(c) for c in session.history)


async def test_human_request_command_and_classifier(store, repo, channel, isp, handoff):
    worker, client = make_worker(store, repo, channel, isp, handoff, [])
    await worker.handle_incoming(msg("m1", "AGENTE"))
    assert await state(repo) == "pendiente_humano" and not client.models.calls
    await worker.handle_incoming(msg("m2", "quiero hablar con un asesor", wa=LUIS))
    assert await state(repo, LUIS) == "pendiente_humano" and not client.models.calls


async def test_emergency_switch_and_disabled_area(store, repo, channel, isp, handoff):
    worker, client = make_worker(store, repo, channel, isp, handoff, [])
    cc = ControlCenter(bot_enabled=False)
    await repo.set_control(cc, "test")
    await worker.handle_incoming(msg("m1", "cuánto debo"))
    assert await state(repo) == "pendiente_humano" and prompts.BOT_OFF_MESSAGE in channel.sent[-1]["text"]

    cc = ControlCenter()
    cc.areas["soporte"].enabled = False
    await repo.set_control(cc, "test")
    await worker.handle_incoming(msg("m2", "no tengo internet", wa=LUIS))
    assert await state(repo, LUIS) == "pendiente_humano" and not client.models.calls


async def test_ai_error_is_visible_in_panel_only(store, repo, channel, isp, handoff):
    not_found = errors.ClientError(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "models/x not found"}})
    worker, _ = make_worker(store, repo, channel, isp, handoff, [not_found, RuntimeError("quota")], fallback="backup")
    await worker.handle_incoming(msg("m1", "hola"))
    assert channel.sent[-1]["text"] == prompts.FALLBACK_ERROR
    system = [m["text"] for m in (await repo.conversation_view(MARIA))["messages"] if m["role"] == "system"]
    assert "404" in system[0] and "quota" in system[0]


async def test_steps_exhausted_hands_off(store, repo, channel, isp, handoff):
    worker, _ = make_worker(store, repo, channel, isp, handoff, [model_call("consultar_planes")] * 5)
    await worker.handle_incoming(msg("m1", "qué planes hay", wa=LUIS))
    assert await state(repo, LUIS) == "pendiente_humano"
    assert "5 pasos" in (await repo.get_conversation(LUIS)).handoff_summary


async def test_delete_requires_code(store, repo, channel, isp, handoff):
    worker, _ = make_worker(store, repo, channel, isp, handoff, [])
    await worker.handle_incoming(msg("m1", "STOP"))
    assert (await repo.get_conversation(MARIA)).opted_out
    await worker.handle_incoming(msg("m2", "borrar mis datos"))
    code = await store.get_json(f"delete:{MARIA}")
    await worker.handle_incoming(msg("m3", "CONFIRMAR BORRADO 9999" if code != "9999" else "CONFIRMAR BORRADO 0000"))
    assert await repo.get_conversation(MARIA) is not None  # código incorrecto: no borra
    await worker.handle_incoming(msg("m4", "borrar mis datos"))
    code = await store.get_json(f"delete:{MARIA}")
    await worker.handle_incoming(msg("m5", f"confirmar borrado {code}"))
    assert await repo.get_conversation(MARIA) is None
    assert channel.sent[-1]["text"] == prompts.DELETE_CONFIRMED
    assert any(a["action"] == "data.deleted" for a in await repo.list_audit())


async def test_template_outside_window(store, repo, channel, isp, handoff):
    worker, _ = make_worker(store, repo, channel, isp, handoff, [])
    await repo.record(MARIA, "user", "hola", ts=1.0)  # mensaje de hace mucho: ventana cerrada
    asesor = await make_user(repo, "marta@isp.ec", "asesor")
    from app.worker import ActionError, WindowClosedError
    with pytest.raises(WindowClosedError):
        await worker.send_agent_message(MARIA, "hola", asesor)
    with pytest.raises(ActionError):
        await worker.send_template(MARIA, "recordatorio_pago", ["María"], asesor)
    await worker.send_template(MARIA, "recordatorio_pago", ["María", "noviembre", "25,00", "10/11"], asesor)
    assert channel.sent[-1] == {"to": MARIA, "type": "template", "name": "recordatorio_pago",
                                "params": ["María", "noviembre", "25,00", "10/11"]}
    view = await repo.conversation_view(MARIA)
    assert view["messages"][-1]["text"].startswith("[Plantilla recordatorio_pago] Hola María")
    assert view["meta"]["state"] == "asignada"


async def test_recover_orphan_buffer(store, repo, channel, isp, handoff):
    worker, _ = make_worker(store, repo, channel, isp, handoff, [model_text("Hola")])
    await store.push(MARIA, msg("m1", "hola").to_dict())
    assert await worker.recover() == 1 and channel.sent
