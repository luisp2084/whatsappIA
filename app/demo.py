"""Modo demo: ejecuta el sistema completo sin WhatsApp ni Redis reales.

    python -m app.demo

- Panel en http://localhost:8080/admin/ (token: `demo`, o el valor de ADMIN_TOKEN).
- Escribes como si fueras un cliente desde la barra "Modo demo" del panel; el mensaje recorre
  el mismo código que en producción (cola, debounce, worker, herramientas, handoff).
- IA: si existe GEMINI_API_KEY se usa Gemini de verdad; si no, un modelo de reglas simple.
- WhatsApp y Redis se simulan en memoria: no se envía nada fuera y todo se pierde al parar.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import fakeredis
import uvicorn
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from google.genai import types
from pydantic import BaseModel, Field

from .agent import Agent, describe_error
from .config import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL, Settings
from .main import create_app
from .parsing import IncomingMessage
from .session import SessionStore
from .whatsapp import WhatsAppError

log = logging.getLogger("demo")

DEMO_CUSTOMER = ("34600000001", "Cliente de prueba")


# --- Dobles de WhatsApp y del inbox humano -----------------------------------------


class DemoWhatsApp:
    """No envía nada: el panel ya muestra cada mensaje a partir de la transcripción."""

    async def send_text(self, to: str, text: str) -> None:
        log.info("[WhatsApp simulado] a %s: %s", to, text[:80])

    async def send_buttons(self, to: str, body: str, options: list[dict[str, str]]) -> None:
        log.info("[WhatsApp simulado] a %s: %s %s", to, body[:80], [o["title"] for o in options])

    async def mark_read(self, message_id: str, typing: bool = True) -> None:
        return None

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        raise WhatsAppError("sin media en modo demo")


class LogHandoff:
    async def notify(self, event: dict[str, Any]) -> None:
        log.info("[Handoff] %s para %s", event.get("type"), event.get("wa_id"))


# --- Modelo de reglas (cuando no hay GEMINI_API_KEY) ---------------------------------


def _text(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))])


def _call(name: str, **args: Any) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(name=name, args=args))]))])


class RuleBasedModels:
    """Imita las decisiones del modelo con reglas fijas para poder probar sin clave de API."""

    async def generate_content(self, *, model: str, contents: list[types.Content], config: Any):
        last = contents[-1]
        responses = [p.function_response for p in (last.parts or []) if p.function_response]
        if responses:
            return _text(self._after_tool(responses[0].name, responses[0].response or {}))

        original = " ".join(p.text for p in (last.parts or []) if p.text)
        t = original.lower()
        order = re.search(r"tx-?\s?(\d{6})", t)
        if re.search(r"persona|humano|agente|asesor|queja|reclam", t):
            return _call("escalate_to_human", summary=f"El cliente escribió: {original[:150]}", priority="media")
        if order:
            return _call("get_order_status", order_id=f"TX-{order.group(1)}")
        if re.search(r"devol|cambiar|roto|dañad", t):
            return _call("offer_options", text="¿Quieres iniciar una devolución? Necesitaré el número de pedido.",
                         options=["Sí, devolver", "No, gracias"])
        if re.search(r"pedido|envío|envio|llega|paquete", t):
            return _text("¿Me das el número de pedido? Tiene el formato *TX-123456*.")
        if re.search(r"horario|abr[eií]|domingo|tienda", t):
            return _text("Nuestras tiendas abren de lunes a sábado de *10:00 a 21:00* y los domingos de *11:00 a 20:00*.")
        if re.search(r"^\s*(hola|buenas|buenos|hey|qué tal|que tal)", t):
            return _call("offer_options", text="¡Hola! ¿En qué te puedo ayudar?",
                         options=["Mi pedido", "Devoluciones", "Hablar con agente"])
        if re.search(r"gracias|perfecto|genial|ok\b", t):
            return _text("¡A ti! Si necesitas algo más, escríbeme por aquí.")
        return _text("Puedo ayudarte con pedidos, envíos, devoluciones y horarios de Tienda X. ¿Qué necesitas?")

    @staticmethod
    def _after_tool(name: str, r: dict[str, Any]) -> str:
        if name != "get_order_status":
            return "Hecho."
        if r.get("error") == "ORDER_NOT_FOUND":
            return "No encuentro ese pedido. ¿Puedes revisar el número? (Prueba con TX-123456 o TX-654321.)"
        if r.get("error"):
            return "Ese número no parece válido. El formato es *TX-123456*."
        if r.get("status") == "en_reparto":
            return f"Tu pedido {r['order_id']} está *en reparto* con {r.get('carrier', 'el transportista')} y llega el {r.get('eta')}."
        if r.get("status") == "entregado":
            return f"Tu pedido {r['order_id']} se entregó el {r.get('delivered_at')}. ¿Hay algún problema con él?"
        return f"Tu pedido {r.get('order_id')} está en estado: {r.get('status')}."


class RuleBasedClient:
    def __init__(self) -> None:
        self.models = RuleBasedModels()
        self.aio = self


# --- Endpoints solo de la demo ------------------------------------------------------------


class CustomerMessage(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    wa_id: str = Field(default=DEMO_CUSTOMER[0], pattern=r"^\d{6,15}$")
    name: str = Field(default=DEMO_CUSTOMER[1], max_length=60)


def demo_router(admin_token: str, ai: dict[str, str]) -> APIRouter:
    def require_admin(authorization: str = Header(default="")) -> None:
        if not hmac.compare_digest(authorization, f"Bearer {admin_token}"):
            raise HTTPException(status_code=401)

    router = APIRouter()

    @router.get("/demo-info")
    async def demo_info() -> dict[str, Any]:  # sin token: solo dice que es una demo
        return {"demo": True, "token_hint": admin_token if admin_token == "demo" else None}

    @router.get("/api/admin/demo", dependencies=[Depends(require_admin)])
    async def info() -> dict[str, Any]:
        return {"demo": True, "ai": ai["label"], "customer": {"wa_id": DEMO_CUSTOMER[0], "name": DEMO_CUSTOMER[1]}}

    @router.post("/api/admin/demo/incoming", dependencies=[Depends(require_admin)])
    async def incoming(request: Request, body: CustomerMessage) -> dict[str, str]:
        msg = IncomingMessage(
            wa_id=body.wa_id, message_id=f"demo.{random.getrandbits(64):x}", type="text", text=body.text,
            timestamp=int(datetime.now(timezone.utc).timestamp()), profile_name=body.name)
        await request.app.state.queue.publish(msg)  # mismo camino que un webhook real
        return {"wa_id": body.wa_id}

    return router


# --- Arranque ------------------------------------------------------------------------------


async def seed(store: SessionStore) -> None:
    """Conversaciones y 30 días de métricas de ejemplo para que el panel no arranque vacío."""
    examples = [
        ("34611222333", "Ana López", "human", [
            ("user", "Hola, mi pedido TX-123456 no ha llegado"),
            ("bot", "Tu pedido TX-123456 está *en reparto* con DHL y llega el 2026-11-05."),
            ("user", "Me dijeron que llegaba ayer, es la tercera vez"),
            ("bot", "Te paso con una persona del equipo."),
            ("system", "Transferido a humano (alta): retraso reiterado del pedido TX-123456"),
            ("user", "¿Hay alguien?")]),
        ("34622333444", "Carlos", "bot", [
            ("user", "¿Abrís el domingo?"),
            ("bot", "Sí, los domingos abrimos de *11:00 a 20:00*.")]),
    ]
    for wa_id, name, mode, msgs in examples:
        for role, text in msgs:
            await store.record(wa_id, role, text, name=name)
        session = await store.load(wa_id)
        session.mode, session.profile_name, session.disclosed = mode, name, True
        await store.save(session)

    rng = random.Random(7)
    today = datetime.now(timezone.utc).date()
    for i in range(30):
        day = today - timedelta(days=i)
        turns = rng.randint(90, 260) if day.weekday() < 5 else rng.randint(30, 90)
        await store.r.hset(f"metrics:{day:%Y-%m-%d}", mapping={
            "turns": turns, "handoffs": int(turns * rng.uniform(0.05, 0.12)), "errors": rng.randint(0, 3),
            "latency_ms_sum": int(turns * rng.uniform(1400, 2300)), "agent_messages": rng.randint(5, 30),
            "tool:get_order_status": int(turns * 0.4), "tool:offer_options": int(turns * 0.3),
            "tool:create_return": int(turns * 0.05), "tool:escalate_to_human": int(turns * 0.06)})


def build_demo() -> tuple[Any, SessionStore]:
    admin_token = os.environ.get("ADMIN_TOKEN") or "demo"
    settings = Settings(wa_token="demo", wa_phone_number_id="demo", wa_app_secret="demo",
                        wa_verify_token="demo", admin_token=admin_token, pseudonym_pepper="demo",
                        debounce_seconds=float(os.environ.get("DEBOUNCE_SECONDS", "1.0")))
    store = SessionStore(fakeredis.aioredis.FakeRedis(decode_responses=True), settings.session_ttl_seconds)

    if os.environ.get("GEMINI_API_KEY"):
        from google import genai

        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        agent = Agent(genai.Client(), model, os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL))
        ai_label = f"Gemini real ({model})"
    else:
        agent = Agent(RuleBasedClient(), "reglas")  # type: ignore[arg-type]
        ai_label = "reglas simples (añade GEMINI_API_KEY para usar Gemini)"

    app = create_app(settings, store=store, wa=DemoWhatsApp(), agent=agent, handoff=LogHandoff())
    app.state.ai = {"label": ai_label}
    app.include_router(demo_router(admin_token, app.state.ai))
    return app, store, agent


async def check_gemini(agent: Agent) -> str | None:
    """Comprueba la clave y los modelos antes de abrir el panel. Devuelve un aviso o None."""
    if isinstance(agent.client, RuleBasedClient):
        return None
    problems = []
    models = list(dict.fromkeys(m for m in (agent.model, agent.fallback_model) if m))
    for model in models:
        try:
            await agent.client.aio.models.get(model=model)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{model}: {describe_error(exc)}")
    if len(problems) == len(models):
        return "ningún modelo responde. " + " | ".join(problems)
    return ("modelo de respaldo usado: " + " | ".join(problems)) if problems else None


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app, store, agent = build_demo()
    await seed(store)
    warning = await check_gemini(agent)
    if warning:
        app.state.ai["label"] += f" · ERROR: {warning}"
        print(f"\n  ATENCIÓN Gemini: {warning}\n"
              "  Revisa GEMINI_API_KEY o elige otro modelo con GEMINI_MODEL "
              "(lista: https://ai.google.dev/gemini-api/docs/models).\n", flush=True)
    port = int(os.environ.get("PORT", "8080"))
    print(f"\n  Panel listo: http://localhost:{port}/admin/  (token: {os.environ.get('ADMIN_TOKEN') or 'demo'})\n",
          flush=True)
    await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")).serve()


if __name__ == "__main__":
    asyncio.run(main())
