"""App FastAPI: webhook de WhatsApp, endpoint de la cola, API y panel web de administración."""

from __future__ import annotations

import base64
import hmac
import json
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .admin import build_router

from .config import Settings
from .parsing import IncomingMessage, parse_webhook
from .queue import LocalQueue, PubSubQueue, Queue
from .security import pseudonymize, verify_signature
from .worker import Worker

WEB_DIR = Path(__file__).parent / "web"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")


def _default_components(settings: Settings) -> dict[str, Any]:
    from google import genai
    from redis.asyncio import Redis

    from .agent import Agent
    from .session import SessionStore
    from .whatsapp import WhatsAppClient
    from .worker import WebhookHandoff

    return {
        "store": SessionStore(Redis.from_url(settings.redis_url, decode_responses=True),
                              settings.session_ttl_seconds),
        "wa": WhatsAppClient(settings.wa_token, settings.wa_phone_number_id, settings.graph_version),
        "agent": Agent(genai.Client(api_key=settings.gemini_api_key or None), settings.gemini_model,
                       settings.gemini_fallback_model, settings.thinking_budget),
        "handoff": WebhookHandoff(settings.handoff_webhook_url),
    }


def create_app(settings: Settings, **overrides: Any) -> FastAPI:
    """`overrides` permite inyectar store/wa/agent/handoff/queue (tests)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        parts = {**(_default_components(settings) if not {"store", "wa", "agent", "handoff"} <= overrides.keys()
                    else {}), **overrides}
        worker = Worker(parts["store"], parts["wa"], parts["agent"], parts["handoff"],
                        debounce_seconds=settings.debounce_seconds,
                        max_history=settings.max_history_contents,
                        max_media_bytes=settings.max_media_bytes,
                        pepper=settings.pseudonym_pepper)
        queue: Queue = parts.get("queue") or (
            PubSubQueue(settings.pubsub_topic) if settings.queue_mode == "pubsub"
            else LocalQueue(worker.handle_incoming)
        )
        app.state.worker, app.state.queue, app.state.store = worker, queue, parts["store"]
        yield
        if isinstance(queue, LocalQueue):
            await queue.drain()

    app = FastAPI(title="Agente WhatsApp", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # --- Webhook de Meta ---------------------------------------------------------

    @app.get("/webhook")
    async def verify(request: Request) -> Response:
        q = request.query_params
        if q.get("hub.mode") == "subscribe" and hmac.compare_digest(
                q.get("hub.verify_token", ""), settings.wa_verify_token):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(status_code=403)

    @app.post("/webhook")
    async def receive(request: Request,
                      x_hub_signature_256: str | None = Header(default=None)) -> Response:
        raw = await request.body()
        if not verify_signature(raw, x_hub_signature_256, settings.wa_app_secret):
            raise HTTPException(status_code=401)
        messages, statuses = parse_webhook(json.loads(raw))
        for msg in messages:
            await request.app.state.queue.publish(msg)  # nada de LLM aquí: responder en < 1 s
        for st in statuses:
            if st.status == "failed":
                log.warning(json.dumps({"event": "wa_status_failed",
                                        "user": pseudonymize(st.wa_id, settings.pseudonym_pepper),
                                        "errors": st.errors}))
        return Response(status_code=200)

    # --- Suscripción push de Pub/Sub ------------------------------------------

    @app.post("/tasks/pubsub")
    async def pubsub_push(request: Request, token: str = "") -> Response:
        if settings.pubsub_push_token and not hmac.compare_digest(token, settings.pubsub_push_token):
            raise HTTPException(status_code=403)
        envelope = await request.json()
        data = json.loads(base64.b64decode(envelope["message"]["data"]))
        await request.app.state.worker.handle_incoming(IncomingMessage.from_dict(data))
        return Response(status_code=204)  # 2xx = ack; un error hace que Pub/Sub reintente

    # --- Administración: devolver la conversación al bot ------------------------

    @app.post("/handoff/release")
    async def release(payload: dict[str, str], authorization: str = Header(default="")) -> dict[str, str]:
        if not settings.admin_token or not hmac.compare_digest(authorization, f"Bearer {settings.admin_token}"):
            raise HTTPException(status_code=401)
        await app.state.worker.set_mode(payload["wa_id"], "bot")
        return {"status": "bot"}

    # --- Panel web ---------------------------------------------------------------

    app.include_router(build_router(settings.admin_token))

    app.mount("/admin", StaticFiles(directory=WEB_DIR, html=True), name="admin")

    return app


def build() -> FastAPI:  # punto de entrada para uvicorn: `uvicorn app.main:build --factory`
    return create_app(Settings.from_env())
