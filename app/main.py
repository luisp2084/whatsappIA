"""App FastAPI: webhook de WhatsApp, endpoint de la cola, outbox, API y panel web.

La misma imagen sirve para todos los servicios; ROLE elige qué monta cada uno:
- all: todo en un proceso (desarrollo, demo o un solo servicio de Cloud Run).
- webhook: GET/POST /webhook (público). Valida la firma y publica en la cola.
- worker: POST /tasks/pubsub (privado). Orquestador y herramientas.
- sender: bucle del outbox que reintenta los envíos pendientes.
- console: panel /admin y API /api (detrás de IAP).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth
from .admin import build_router
from .config import Settings
from .db import Database
from .parsing import IncomingMessage, parse_webhook
from .queue import LocalQueue, PubSubQueue, Queue
from .repo import Repo
from .security import pseudonymize, verify_signature
from .worker import Worker

WEB_DIR = Path(__file__).parent / "web"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")


def _default_components(settings: Settings, needed: set[str]) -> dict[str, Any]:
    """Construye solo lo que no llegó en `overrides` (los tests inyectan dobles)."""
    out: dict[str, Any] = {}
    client = None
    if needed & {"agent", "classifier", "isp", "suggester"}:
        from google import genai

        client = genai.Client(api_key=settings.gemini_api_key or None)
    if "store" in needed:
        from redis.asyncio import Redis

        from .session import SessionStore

        out["store"] = SessionStore(Redis.from_url(settings.redis_url, decode_responses=True),
                                    settings.session_ttl_seconds)
    if "db" in needed:
        out["db"] = Database(settings.database_url)
    if "channel" in needed:
        from .whatsapp import WhatsAppClient

        out["channel"] = WhatsAppClient(settings.wa_token, settings.wa_phone_number_id, settings.graph_version)
    if "agent" in needed:
        from .agent import Agent

        out["agent"] = Agent(client, settings.gemini_model, settings.gemini_fallback_model, settings.thinking_budget)
    if "classifier" in needed:
        from .areas import KeywordClassifier, LLMClassifier

        out["classifier"] = (LLMClassifier(client, settings.classifier_model) if settings.classifier_model
                             else KeywordClassifier())
    if "isp" in needed:
        from .isp import build_isp

        out["isp"] = build_isp(client, settings.gemini_model)
    if "handoff" in needed:
        from .worker import WebhookHandoff

        out["handoff"] = WebhookHandoff(settings.handoff_webhook_url)
    if "suggester" in needed:
        from .revision import RuleSuggester

        out["suggester"] = RuleSuggester(client, settings.gemini_model)
    return out


async def bootstrap_owner(repo: Repo, settings: Settings) -> None:
    """Crea el primer owner desde OWNER_EMAIL / OWNER_PASSWORD si aún no hay usuarios."""
    if await repo.count_users() > 0 or not (settings.owner_email and settings.owner_password):
        return
    if problem := auth.validate_password(settings.owner_password):
        raise RuntimeError(f"OWNER_PASSWORD no es válida: {problem}")
    await repo.create_user(settings.owner_email, "Owner", "owner", auth.hash_password(settings.owner_password))
    await repo.audit("sistema", "user.bootstrap", settings.owner_email)
    log.info("Usuario owner inicial creado: %s", settings.owner_email)


def create_app(settings: Settings, **overrides: Any) -> FastAPI:
    """`overrides` acepta store, db, channel, agent, classifier, isp, handoff, suggester y queue."""
    role = settings.role
    serves = (lambda *r: role == "all" or role in r)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        names = {"store", "db", "channel", "agent", "classifier", "isp", "handoff", "suggester"}
        parts = {**_default_components(settings, names - overrides.keys()), **overrides}
        db: Database = parts["db"]
        await db.create_all()
        repo = Repo(db)
        await bootstrap_owner(repo, settings)
        worker = Worker(parts["store"], repo, parts["channel"], parts["agent"], parts["isp"], parts["handoff"],
                        classifier=parts["classifier"], debounce_seconds=settings.debounce_seconds,
                        max_history=settings.max_history_contents, max_media_bytes=settings.max_media_bytes,
                        pepper=settings.pseudonym_pepper)
        queue: Queue = parts.get("queue") or (
            PubSubQueue(settings.pubsub_topic) if settings.queue_mode == "pubsub" else LocalQueue(worker.handle_incoming))
        app.state.worker, app.state.queue, app.state.store, app.state.repo = worker, queue, parts["store"], repo
        app.state.suggester = parts["suggester"]
        tasks: list[asyncio.Task] = []
        if serves("sender") and overrides.get("run_background", True):
            tasks.append(asyncio.create_task(worker.outbox.run_forever()))
        if serves("worker") and overrides.get("run_background", True):
            tasks.append(asyncio.create_task(worker.recover()))
        yield
        for t in tasks:
            t.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await t
        if isinstance(queue, LocalQueue):
            await queue.drain()
        if "db" not in overrides:
            await db.dispose()

    app = FastAPI(title="Agente WhatsApp", lifespan=lifespan)

    @app.middleware("http")
    async def no_stale_panel(request: Request, call_next):
        # El panel cambia con cada versión: que el navegador revalide siempre en lugar de mezclar
        # un app.js viejo con un index.html nuevo (pantalla en blanco).
        response = await call_next(request)
        if request.url.path.startswith("/admin"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "role": role}

    if serves("webhook"):
        @app.get("/webhook")
        async def verify(request: Request) -> Response:
            q = request.query_params
            if q.get("hub.mode") == "subscribe" and hmac.compare_digest(
                    q.get("hub.verify_token", ""), settings.wa_verify_token):
                return PlainTextResponse(q.get("hub.challenge", ""))
            raise HTTPException(status_code=403)

        @app.post("/webhook")
        async def receive(request: Request, x_hub_signature_256: str | None = Header(default=None)) -> Response:
            raw = await request.body()
            if not verify_signature(raw, x_hub_signature_256, settings.wa_app_secret):
                raise HTTPException(status_code=401)
            messages, statuses = parse_webhook(json.loads(raw))
            for msg in messages:
                await request.app.state.queue.publish(msg)  # nada de LLM aquí: responder en < 1 s
            metrics: dict[str, int] = {}
            for st in statuses:
                if st.status == "failed":
                    metrics["wa_status_failed"] = metrics.get("wa_status_failed", 0) + 1
                    log.warning(json.dumps({"event": "wa_status_failed", "errors": st.errors,
                                            "user": pseudonymize(st.wa_id, settings.pseudonym_pepper)}))
                if st.pricing.get("billable"):  # costo real de Meta por categoría
                    k = f"wa_billable:{st.pricing.get('category', 'otro')}"
                    metrics[k] = metrics.get(k, 0) + 1
            if metrics:
                await request.app.state.store.incr_metrics(metrics)
            return Response(status_code=200)

    if serves("worker"):
        @app.post("/tasks/pubsub")
        async def pubsub_push(request: Request, token: str = "") -> Response:
            if settings.pubsub_push_token and not hmac.compare_digest(token, settings.pubsub_push_token):
                raise HTTPException(status_code=403)
            envelope = await request.json()
            data = json.loads(base64.b64decode(envelope["message"]["data"]))
            await request.app.state.worker.handle_incoming(IncomingMessage.from_dict(data))
            return Response(status_code=204)  # 2xx = ack; un error hace que Pub/Sub reintente

    if serves("console"):
        app.include_router(build_router(settings.secret_key))

        @app.get("/", include_in_schema=False)
        async def root() -> Response:
            return RedirectResponse("/admin/")

        app.mount("/admin", StaticFiles(directory=WEB_DIR, html=True), name="admin")

    return app


def build() -> FastAPI:  # punto de entrada para uvicorn: `uvicorn app.main:build --factory`
    return create_app(Settings.from_env())
