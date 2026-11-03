"""Herramientas (function calling): declaraciones para Gemini + implementaciones.

Las implementaciones de pedidos son MOCKS: sustitúyelas por llamadas a tu OMS/CRM.
Cada herramienta devuelve un dict serializable; los errores se devuelven como
{"error": "..."} para que el modelo los explique, en lugar de lanzar excepciones.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from google.genai import types

log = logging.getLogger(__name__)

# Herramientas "terminales": el worker las convierte en acciones de UI y cierra el turno.
OFFER_OPTIONS = "offer_options"
ESCALATE = "escalate_to_human"

DECLARATIONS = [
    types.FunctionDeclaration(
        name="get_order_status",
        description="Obtiene el estado y la fecha estimada de entrega de un pedido. "
                    "Úsala siempre que el usuario pregunte por su pedido.",
        parameters_json_schema={
            "type": "object",
            "properties": {"order_id": {"type": "string", "description": "ID del pedido, formato TX-123456"}},
            "required": ["order_id"],
        },
    ),
    types.FunctionDeclaration(
        name="create_return",
        description="Inicia una devolución. Solo tras confirmación explícita del usuario.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "reason": {"type": "string", "enum": ["defectuoso", "talla", "no_deseado", "otro"]},
            },
            "required": ["order_id", "reason"],
        },
    ),
    types.FunctionDeclaration(
        name=OFFER_OPTIONS,
        description="Envía al usuario un mensaje con 2 o 3 botones de respuesta rápida. "
                    "Úsala para preguntas cerradas. Termina tu turno.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Texto del mensaje (máx. 3 frases)"},
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 3,
                    "items": {"type": "string", "description": "Etiqueta del botón, máx. 20 caracteres"},
                },
            },
            "required": ["text", "options"],
        },
    ),
    types.FunctionDeclaration(
        name=ESCALATE,
        description="Transfiere la conversación a un agente humano. Termina tu turno.",
        parameters_json_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Resumen del caso para el agente humano"},
                "priority": {"type": "string", "enum": ["baja", "media", "alta"]},
            },
            "required": ["summary"],
        },
    ),
]

TOOLS = [types.Tool(function_declarations=DECLARATIONS)]


@dataclass
class ToolContext:
    wa_id: str


ToolFn = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

# ---------------------------------------------------------------------------
# Implementaciones (mock)
# ---------------------------------------------------------------------------

_ORDER_RE = re.compile(r"^TX-\d{6}$")
_FAKE_ORDERS = {
    "TX-123456": {"status": "en_reparto", "eta": "2026-11-05", "carrier": "DHL"},
    "TX-654321": {"status": "entregado", "delivered_at": "2026-10-28"},
}


async def get_order_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    order_id = str(args.get("order_id", "")).strip().upper()
    if not _ORDER_RE.match(order_id):
        return {"error": "INVALID_ORDER_ID", "hint": "El formato es TX-123456"}
    # En producción: comprueba que el pedido pertenece a ctx.wa_id antes de devolver datos.
    order = _FAKE_ORDERS.get(order_id)
    return {"order_id": order_id, **order} if order else {"error": "ORDER_NOT_FOUND"}


async def create_return(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    order_id = str(args.get("order_id", "")).strip().upper()
    if order_id not in _FAKE_ORDERS:
        return {"error": "ORDER_NOT_FOUND"}
    return {"return_id": "RT-0001", "label_url": "https://example.com/etiqueta/RT-0001"}


REGISTRY: dict[str, ToolFn] = {
    "get_order_status": get_order_status,
    "create_return": create_return,
}


async def call_tool(name: str, args: dict[str, Any], ctx: ToolContext,
                    registry: dict[str, ToolFn] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    fn = (registry or REGISTRY).get(name)
    if fn is None:
        return {"error": "UNKNOWN_TOOL"}
    try:
        return await asyncio.wait_for(fn(args, ctx), timeout=timeout)
    except asyncio.TimeoutError:
        return {"error": "UPSTREAM_TIMEOUT", "retryable": True}
    except Exception:  # noqa: BLE001 - el modelo debe recibir siempre una respuesta
        log.exception("La herramienta %s falló", name)
        return {"error": "INTERNAL_ERROR"}
