import json

import pytest

from app.control import ControlCenter
from app.tools import ToolContext, call_tool

from .conftest import JORGE, LUIS, MARIA, receipt

pytestmark = pytest.mark.asyncio


def ctx_for(wa_id, repo, store, isp, media=None, **control):
    return ToolContext(wa_id=wa_id, repo=repo, store=store, isp=isp, control=ControlCenter(**control),
                       media=media or [])


async def test_customer_lookup_links_number_and_protects_other_cedulas(repo, store, isp):
    ctx = ctx_for(MARIA, repo, store, isp)
    out = await call_tool("buscar_cliente", {}, ctx)  # encontrado por su número
    assert {c["id"] for c in out["contratos"]} == {"C-1001", "C-1005"}
    assert (await repo.identity(MARIA)).customer_id == "0102030405"
    other = await call_tool("buscar_cliente", {"cedula": "0912345678"}, ctx)  # cédula de Jorge
    assert other["error"] == "NUMERO_NO_VINCULADO" and "contratos" not in other


async def test_debt_requires_own_contract_and_respects_collectable_months(repo, store, isp):
    ctx = ctx_for(MARIA, repo, store, isp)
    assert (await call_tool("consultar_deuda", {"contrato": "C-1001"}, ctx))["error"] == "CLIENTE_NO_IDENTIFICADO"
    await call_tool("buscar_cliente", {}, ctx)
    assert (await call_tool("consultar_deuda", {"contrato": "C-1002"}, ctx))["error"] == "CONTRATO_NO_ENCONTRADO"
    full = await call_tool("consultar_deuda", {"contrato": "c1001"}, ctx)
    assert full["total_usd"] == 50.0 and len(full["cuotas"]) == 2
    one = await call_tool("consultar_deuda", {"contrato": "C-1001"}, ctx_for(MARIA, repo, store, isp, months_collectable=1))
    assert one["total_usd"] == 25.0 and "nota" in one


async def test_receipt_validation_rules(repo, store, isp):
    bad = receipt(cuenta_destino="1234567", fecha="2020-01-01", confianza=0.3)
    out = await call_tool("validar_comprobante", {}, ctx_for(MARIA, repo, store, isp, media=[(bad, "image/jpeg")]))
    assert out["valido"] is False
    assert {"la cuenta de destino no es de la empresa", "la imagen no se lee con claridad"} <= set(out["problemas"])
    assert any("más de 30 días" in p for p in out["problemas"])
    assert (await call_tool("validar_comprobante", {}, ctx_for(MARIA, repo, store, isp)))["error"] == "SIN_IMAGEN"


async def test_payment_registered_once_with_amount_from_receipt(repo, store, isp):
    img = receipt()
    ctx = ctx_for(MARIA, repo, store, isp, media=[(img, "image/jpeg")])
    await call_tool("buscar_cliente", {}, ctx)
    assert (await call_tool("validar_comprobante", {}, ctx))["valido"] is True
    paid = await call_tool("registrar_pago", {"contrato": "C-1001", "monto": 9999}, ctx)  # el monto del modelo se ignora
    assert paid["monto_usd"] == 50.0 and paid["saldo_usd"] == 0.0
    # La misma imagen otra vez: rechazada por hash; con otra imagen pero mismo número: rechazada por clave única.
    again = await call_tool("validar_comprobante", {}, ctx)
    assert again["error"] == "COMPROBANTE_YA_REGISTRADO"
    same_number = ctx_for(MARIA, repo, store, isp, media=[(receipt(monto=45.0, confianza=0.96), "image/jpeg")])
    out = await call_tool("validar_comprobante", {}, same_number)
    assert "ese número de comprobante ya fue registrado" in out["problemas"]
    assert isp.nc.payments == [{"contract": "C-1001", "amount_cents": 5000, "reference": "Banco Pichincha:889911"}]


async def test_payment_amount_mismatch_goes_to_advisor(repo, store, isp):
    ctx = ctx_for(MARIA, repo, store, isp, media=[(receipt(monto=30.0), "image/jpeg")])
    await call_tool("buscar_cliente", {}, ctx)
    await call_tool("validar_comprobante", {}, ctx)
    out = await call_tool("registrar_pago", {"contrato": "C-1001"}, ctx)
    assert out["error"] == "MONTO_NO_COINCIDE" and out["deuda_usd"] == 50.0
    assert isp.nc.payments == []


async def test_support_tools(repo, store, isp):
    jorge = ctx_for(JORGE, repo, store, isp)
    await call_tool("buscar_cliente", {}, jorge)
    status = await call_tool("estado_ont", {"contrato": "C-1002"}, jorge)
    assert status["incidente_en_zona"]["solucion_estimada"] == "hoy a las 18:00"
    assert (await call_tool("crear_ticket_visita", {"contrato": "C-1002", "problema": "sin internet"}, jorge))["error"] == "INCIDENTE_ACTIVO"

    luis = ctx_for(LUIS, repo, store, isp)
    await call_tool("buscar_cliente", {}, luis)
    assert "potencia óptica baja" in (await call_tool("estado_ont", {"contrato": "C-1003"}, luis))["alerta"]
    assert (await call_tool("reiniciar_ont", {"contrato": "C-1003"}, luis))["reiniciada"] is True
    assert (await call_tool("reiniciar_ont", {"contrato": "C-1003"}, luis))["error"] == "REINICIO_RECIENTE"
    ticket = await call_tool("crear_ticket_visita", {"contrato": "C-1003", "problema": "lento"}, luis)
    assert ticket["ticket"].startswith("T-")


async def test_olt_failure_never_reports_ok(repo, store, isp):
    async def broken(contract_id):
        raise TimeoutError("OLT")
    isp.olt.ont_status = broken
    ctx = ctx_for(LUIS, repo, store, isp)
    await call_tool("buscar_cliente", {}, ctx)
    out = await call_tool("estado_ont", {"contrato": "C-1003"}, ctx)
    assert out["error"] == "OLT_NO_RESPONDE" and "en_linea" not in out


async def test_extension_limit_and_tool_policy(repo, store, isp):
    from datetime import timedelta

    ctx = ctx_for(MARIA, repo, store, isp)
    await call_tool("buscar_cliente", {}, ctx)
    today = ctx.control.now().date()
    ok = await call_tool("registrar_prorroga", {"contrato": "C-1001", "fecha": (today + timedelta(days=3)).isoformat()}, ctx)
    too_long = await call_tool("registrar_prorroga", {"contrato": "C-1001", "fecha": (today + timedelta(days=20)).isoformat()}, ctx)
    assert ok["aprobada"] is True and too_long["aprobada"] is False
    blocked = await call_tool("reiniciar_ont", {"contrato": "C-1001"}, ctx, allowed={"consultar_deuda"})
    assert blocked["error"] == "HERRAMIENTA_NO_PERMITIDA"
    assert (await store.metrics(1))[-1]["tool_error:reiniciar_ont"] == 1
    audit = [a["action"] for a in await repo.list_audit()]
    assert "extension.granted" in audit
