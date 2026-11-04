"""Herramientas del agente (function calling) para un ISP.

Reglas comunes:
- El servidor valida todo: que el contrato pertenezca al número que escribe, los límites del
  Centro de control y los datos del comprobante. Nunca se confía en lo que diga el modelo.
- Los montos que se registran salen del comprobante validado, no de los argumentos del modelo.
- Los errores vuelven al modelo como {"error": "CODIGO", ...} legibles, nunca como trazas.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Awaitable, Callable

from google.genai import types

from .control import ControlCenter, months_ago
from .isp import ISP, Contract, contract_dict
from .repo import DuplicatePayment, Repo
from .session import SessionStore

log = logging.getLogger(__name__)

OFFER_OPTIONS = "offer_options"
ESCALATE = "escalate_to_human"
RECEIPT_TTL = 3600
MIN_RECEIPT_CONFIDENCE = 0.7


@dataclass
class ToolContext:
    wa_id: str
    repo: Repo
    store: SessionStore
    isp: ISP
    control: ControlCenter
    media: list[tuple[bytes, str]] = field(default_factory=list)  # imágenes/PDF del turno actual
    errors: list[str] = field(default_factory=list)  # códigos de error devueltos en el turno


ToolFn = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


def _decl(name: str, description: str, properties: dict[str, Any] | None = None,
          required: list[str] | None = None) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(name=name, description=description, parameters_json_schema={
        "type": "object", "properties": properties or {}, "required": required or []})


CONTRATO = {"contrato": {"type": "string", "description": "ID del contrato, formato C-1234"}}

DECLARATIONS: dict[str, types.FunctionDeclaration] = {d.name: d for d in [
    _decl("buscar_cliente",
          "Identifica al cliente y lista sus contratos. Sin cédula, busca por el número de WhatsApp. "
          "Úsala antes de cualquier consulta de deuda, pago o servicio.",
          {"cedula": {"type": "string", "description": "Cédula o RUC del titular, solo dígitos"}}),
    _decl("consultar_deuda", "Cuotas pendientes y total a pagar de un contrato. Única fuente de montos.",
          CONTRATO, ["contrato"]),
    _decl("validar_comprobante",
          "Lee y valida el comprobante de pago que el cliente envió como imagen o PDF en este mensaje. "
          "No registra el pago."),
    _decl("registrar_pago",
          "Registra el pago del último comprobante validado en el contrato indicado. Úsala solo si "
          "validar_comprobante devolvió valido=true.", CONTRATO, ["contrato"]),
    _decl("registrar_prorroga", "Solicita una prórroga de pago hasta una fecha.",
          {**CONTRATO, "fecha": {"type": "string", "description": "Fecha límite pedida, AAAA-MM-DD"}},
          ["contrato", "fecha"]),
    _decl("estado_ont",
          "Estado del equipo de fibra (ONT) del cliente: en línea, potencia óptica e incidentes en la zona.",
          CONTRATO, ["contrato"]),
    _decl("reiniciar_ont", "Reinicia remotamente la ONT del cliente. Pide confirmación antes.",
          CONTRATO, ["contrato"]),
    _decl("crear_ticket_visita", "Agenda una visita técnica. No la uses si hay un incidente activo en la zona.",
          {**CONTRATO, "problema": {"type": "string", "description": "Descripción breve del problema"}},
          ["contrato", "problema"]),
    _decl("consultar_planes", "Planes de internet disponibles con velocidad y precio."),
    _decl(OFFER_OPTIONS,
          "Envía un mensaje con 2 o 3 botones de respuesta rápida para preguntas cerradas. Termina tu turno.",
          {"text": {"type": "string", "description": "Texto del mensaje (máx. 3 frases)"},
           "options": {"type": "array", "minItems": 2, "maxItems": 3,
                       "items": {"type": "string", "description": "Etiqueta del botón, máx. 20 caracteres"}}},
          ["text", "options"]),
    _decl(ESCALATE, "Transfiere la conversación a un asesor humano. Termina tu turno.",
          {"summary": {"type": "string", "description": "Resumen del caso para el asesor"},
           "priority": {"type": "string", "enum": ["baja", "media", "alta"]}}, ["summary"]),
]}


# --- utilidades --------------------------------------------------------------------------------


def usd(cents: int) -> float:
    return round(cents / 100, 2)


def _norm_contract(value: Any) -> str:
    m = re.search(r"(\d{3,6})", str(value or ""))
    return f"C-{m.group(1)}" if m else ""


async def _linked_customer(ctx: ToolContext) -> str | None:
    ident = await ctx.repo.identity(ctx.wa_id)
    return ident.customer_id if ident and ident.customer_id else None


async def _own_contract(ctx: ToolContext, raw: Any) -> Contract | dict[str, Any]:
    contract_id = _norm_contract(raw)
    if not contract_id:
        return {"error": "CONTRATO_INVALIDO", "hint": "El formato es C-1234"}
    customer = await _linked_customer(ctx)
    if customer is None:
        return {"error": "CLIENTE_NO_IDENTIFICADO", "hint": "Usa buscar_cliente primero"}
    contract = await ctx.isp.nc.contract(contract_id)
    if contract is None or contract.customer_id != customer:
        return {"error": "CONTRATO_NO_ENCONTRADO", "hint": "No es un contrato de este cliente"}
    return contract


def _today(ctx: ToolContext) -> date:
    return ctx.control.now().date()


async def _collectable(ctx: ToolContext, contract_id: str) -> tuple[list[Any], list[Any]]:
    items = await ctx.isp.nc.installments(contract_id)
    n = ctx.control.months_collectable
    if n <= 0:
        return items, []
    oldest = months_ago(_today(ctx), n - 1)
    return [i for i in items if i.month >= oldest], [i for i in items if i.month < oldest]


# --- herramientas ------------------------------------------------------------------------------


async def buscar_cliente(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    cedula = "".join(ch for ch in str(args.get("cedula", "")) if ch.isdigit())
    linked = await _linked_customer(ctx)
    if not cedula:
        if linked:
            cedula = linked
        else:
            matches = await ctx.isp.nc.customers_by_phone(ctx.wa_id)
            if len(matches) != 1:
                return {"error": "CLIENTE_NO_IDENTIFICADO", "hint": "Pide la cédula del titular del servicio"}
            cedula = matches[0]["id"]
    customer = await ctx.isp.nc.customer(cedula)
    if customer is None:
        return {"error": "CLIENTE_NO_ENCONTRADO", "hint": "Verifica la cédula con el cliente"}
    if linked != cedula and ctx.wa_id not in customer.get("phones", []):
        # Protección de datos: no se revela nada de una cédula que no corresponde a este número.
        return {"error": "NUMERO_NO_VINCULADO",
                "hint": "Por seguridad no se pueden dar datos de esa cédula desde este número. "
                        "Deriva a un asesor para verificar la identidad."}
    if linked != cedula:
        await ctx.repo.link_customer(ctx.wa_id, cedula)
    contracts = await ctx.isp.nc.contracts(cedula)
    return {"cliente": customer["name"], "contratos": [contract_dict(c) for c in contracts]}


async def consultar_deuda(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    contract = await _own_contract(ctx, args.get("contrato"))
    if isinstance(contract, dict):
        return contract
    due, excluded = await _collectable(ctx, contract.id)
    out: dict[str, Any] = {
        "contrato": contract.id, "estado_servicio": contract.status,
        "cuotas": [{"mes": i.month, "concepto": i.concept, "valor_usd": usd(i.amount_cents), "vence": i.due_date}
                   for i in due],
        "total_usd": usd(sum(i.amount_cents for i in due)),
    }
    if excluded:
        out["nota"] = (f"Hay {len(excluded)} cuota(s) más antiguas por {usd(sum(i.amount_cents for i in excluded))} USD "
                       "que debe gestionar un asesor.")
    return out


async def validar_comprobante(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    images = [(d, m) for d, m in ctx.media if m.startswith("image/") or m == "application/pdf"]
    if not images:
        return {"error": "SIN_IMAGEN", "hint": "Pide al cliente la foto o el PDF del comprobante"}
    data, mime = images[-1]
    sha = hashlib.sha256(data).hexdigest()
    if await ctx.repo.payment_by_image(sha):
        return {"error": "COMPROBANTE_YA_REGISTRADO", "hint": "Ese comprobante ya fue registrado antes"}
    receipt = await ctx.isp.receipts.read(data, mime)
    problems: list[str] = []
    if not receipt.es_comprobante:
        problems.append("la imagen no parece un comprobante de pago")
    if receipt.monto <= 0:
        problems.append("no se lee un monto válido")
    try:
        paid = datetime.strptime(receipt.fecha, "%Y-%m-%d").date()
        age = (_today(ctx) - paid).days
        if age < 0:
            problems.append("la fecha es futura")
        elif age > ctx.control.receipt_max_age_days:
            problems.append(f"tiene más de {ctx.control.receipt_max_age_days} días")
    except ValueError:
        problems.append("no se lee la fecha")
    if not ctx.control.account_is_ours(receipt.cuenta_destino):
        problems.append("la cuenta de destino no es de la empresa")
    if receipt.confianza < MIN_RECEIPT_CONFIDENCE:
        problems.append("la imagen no se lee con claridad")
    if receipt.banco and receipt.numero and await ctx.repo.payment_by_receipt(receipt.banco, receipt.numero):
        problems.append("ese número de comprobante ya fue registrado")
    valid = not problems
    await ctx.store.put_json(f"receipt:{ctx.wa_id}", {**receipt.model_dump(), "sha256": sha, "valid": valid,
                                                      "problems": problems}, RECEIPT_TTL)
    return {"valido": valid, "problemas": problems,
            "datos": {"banco": receipt.banco, "numero": receipt.numero, "monto_usd": round(receipt.monto, 2),
                      "fecha": receipt.fecha}}


async def registrar_pago(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    contract = await _own_contract(ctx, args.get("contrato"))
    if isinstance(contract, dict):
        return contract
    receipt = await ctx.store.get_json(f"receipt:{ctx.wa_id}")
    if not receipt:
        return {"error": "SIN_COMPROBANTE_VALIDADO", "hint": "Usa validar_comprobante con la imagen primero"}
    if not receipt["valid"]:
        return {"error": "COMPROBANTE_INVALIDO", "problemas": receipt["problems"], "accion": "deriva a un asesor"}
    amount_cents = round(receipt["monto"] * 100)
    due, _ = await _collectable(ctx, contract.id)
    debt_cents = sum(i.amount_cents for i in due)
    if amount_cents != debt_cents and ctx.control.payment_mismatch == "asesor":
        return {"error": "MONTO_NO_COINCIDE", "monto_usd": usd(amount_cents), "deuda_usd": usd(debt_cents),
                "accion": "deriva a un asesor sin registrar el pago"}
    try:
        payment = await ctx.repo.add_payment(
            contract=contract.id, bank=receipt["banco"], receipt_number=receipt["numero"],
            amount_cents=amount_cents, paid_on=receipt["fecha"], image_sha256=receipt["sha256"], wa_id=ctx.wa_id)
    except DuplicatePayment:
        return {"error": "COMPROBANTE_YA_REGISTRADO"}
    try:
        result = await ctx.isp.nc.apply_payment(contract.id, amount_cents, f"{receipt['banco']}:{receipt['numero']}")
    except Exception as exc:  # noqa: BLE001 - el pago queda en la base para conciliarlo a mano
        await ctx.repo.audit("bot", "payment.apply_failed", contract.id, {"payment_id": payment.id, "error": str(exc)})
        return {"error": "NETWORK_CONTROL_NO_RESPONDE", "accion": "deriva a un asesor; el pago quedó guardado"}
    await ctx.store.pop_json(f"receipt:{ctx.wa_id}")
    await ctx.repo.audit("bot", "payment.registered", contract.id,
                         {"payment_id": payment.id, "amount_cents": amount_cents, "bank": receipt["banco"],
                          "receipt": receipt["numero"]})
    await ctx.store.incr_metrics({"payments": 1, "payments_cents": amount_cents})
    return {"pago_id": payment.id, "monto_usd": usd(amount_cents), "aplicado_a": result.get("aplicado_a", []),
            "saldo_usd": usd(result.get("saldo_cents", 0)), "estado_servicio": result.get("estado_servicio", "")}


async def registrar_prorroga(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    contract = await _own_contract(ctx, args.get("contrato"))
    if isinstance(contract, dict):
        return contract
    try:
        until = datetime.strptime(str(args.get("fecha", "")), "%Y-%m-%d").date()
    except ValueError:
        return {"error": "FECHA_INVALIDA", "hint": "Formato AAAA-MM-DD"}
    days = (until - _today(ctx)).days
    if days <= 0:
        return {"error": "FECHA_INVALIDA", "hint": "La fecha debe ser futura"}
    if days > ctx.control.extension_max_days:
        return {"aprobada": False, "maximo_dias": ctx.control.extension_max_days,
                "accion": "deriva a un asesor para una prórroga mayor"}
    result = await ctx.isp.nc.grant_extension(contract.id, until.isoformat())
    await ctx.repo.audit("bot", "extension.granted", contract.id, {"until": until.isoformat()})
    return {"aprobada": bool(result.get("aprobada")), "hasta": until.isoformat()}


async def estado_ont(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    contract = await _own_contract(ctx, args.get("contrato"))
    if isinstance(contract, dict):
        return contract
    out: dict[str, Any] = {"contrato": contract.id, "estado_servicio": contract.status}
    incident = await ctx.isp.nc.active_incident(contract.zone)
    if incident:
        out["incidente_en_zona"] = {"descripcion": incident.description, "solucion_estimada": incident.eta}
    try:
        status = await ctx.isp.olt.ont_status(contract.id)
    except Exception:  # noqa: BLE001
        out["error"] = "OLT_NO_RESPONDE"
        out["hint"] = "No afirmes que el servicio está bien; ofrece una visita o un asesor"
        return out
    out.update(status)
    power = status.get("potencia_dbm")
    if power is not None and power < ctx.control.low_power_dbm:
        out["alerta"] = f"potencia óptica baja ({power} dBm): probable problema de fibra, requiere visita"
    return out


async def reiniciar_ont(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    contract = await _own_contract(ctx, args.get("contrato"))
    if isinstance(contract, dict):
        return contract
    cooldown = ctx.control.reboot_cooldown_minutes * 60
    if not await ctx.store.allow(f"reboot:{contract.id}", cooldown):
        wait = max(1, (await ctx.store.retry_in(f"reboot:{contract.id}")) // 60)
        return {"error": "REINICIO_RECIENTE", "hint": f"Espera {wait} minuto(s) antes de otro reinicio"}
    try:
        result = await ctx.isp.olt.reboot(contract.id)
    except Exception:  # noqa: BLE001
        return {"error": "OLT_NO_RESPONDE"}
    await ctx.repo.audit("bot", "ont.reboot", contract.id)
    return result


async def crear_ticket_visita(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    contract = await _own_contract(ctx, args.get("contrato"))
    if isinstance(contract, dict):
        return contract
    incident = await ctx.isp.nc.active_incident(contract.zone)
    if incident:
        return {"error": "INCIDENTE_ACTIVO", "descripcion": incident.description,
                "solucion_estimada": incident.eta, "hint": "No agendes visita: informa del incidente"}
    problem = str(args.get("problema", ""))[:300]
    ticket = await ctx.isp.nc.create_ticket(contract.id, problem, contract.address)
    await ctx.repo.audit("bot", "ticket.created", contract.id, {"ticket": ticket, "problem": problem})
    return {"ticket": ticket, "direccion": contract.address}


async def consultar_planes(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return {"planes": [p.model_dump() for p in ctx.control.plans], "moneda": ctx.control.currency}


REGISTRY: dict[str, ToolFn] = {
    "buscar_cliente": buscar_cliente,
    "consultar_deuda": consultar_deuda,
    "validar_comprobante": validar_comprobante,
    "registrar_pago": registrar_pago,
    "registrar_prorroga": registrar_prorroga,
    "estado_ont": estado_ont,
    "reiniciar_ont": reiniciar_ont,
    "crear_ticket_visita": crear_ticket_visita,
    "consultar_planes": consultar_planes,
}


async def call_tool(name: str, args: dict[str, Any], ctx: ToolContext, allowed: set[str] | None = None,
                    registry: dict[str, ToolFn] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """Ejecuta una herramienta aplicando la política: solo las del área, con timeout y sin excepciones."""
    if allowed is not None and name not in allowed:
        result: dict[str, Any] = {"error": "HERRAMIENTA_NO_PERMITIDA", "hint": "No está disponible en esta área"}
    else:
        fn = (registry or REGISTRY).get(name)
        if fn is None:
            result = {"error": "HERRAMIENTA_DESCONOCIDA"}
        else:
            try:
                result = await asyncio.wait_for(fn(args, ctx), timeout=timeout)
            except asyncio.TimeoutError:
                result = {"error": "UPSTREAM_TIMEOUT", "retryable": True}
            except Exception:  # noqa: BLE001 - el modelo debe recibir siempre una respuesta
                log.exception("La herramienta %s falló", name)
                result = {"error": "INTERNAL_ERROR"}
    if "error" in result:
        ctx.errors.append(f"{name}:{result['error']}")
        log.info('{"event": "tool_error", "tool": "%s", "code": "%s"}', name, result["error"])
        await ctx.store.incr_metrics({"tool_errors": 1, f"tool_error:{name}": 1})
    return result
