"""Procesa los mensajes encolados: debounce, comandos, agente, respuesta y handoff."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from typing import Any, Protocol

import httpx
from google.genai import types

from . import prompts
from .agent import Agent, describe_error, dump_history, load_history
from .parsing import IncomingMessage
from .security import pseudonymize, redact_pii
from .session import Session, SessionStore
from .tools import ToolContext
from .whatsapp import WhatsAppClient, WhatsAppError

log = logging.getLogger(__name__)

GEMINI_MEDIA = ("image/", "audio/", "application/pdf")
CMD_HUMAN = {"AGENTE", "HUMANO", "PERSONA"}
CMD_OPT_OUT = {"STOP", "BAJA"}
CMD_OPT_IN = {"ALTA"}
CMD_DELETE = {"BORRAR MIS DATOS"}
BUTTON_HUMAN = "human"


class HandoffNotifier(Protocol):
    async def notify(self, event: dict[str, Any]) -> None: ...


class WebhookHandoff:
    """Envía el caso a tu inbox humano (Chatwoot, Zendesk, n8n...) vía webhook."""

    def __init__(self, url: str):
        self.url = url
        self.http = httpx.AsyncClient(timeout=5.0)

    async def notify(self, event: dict[str, Any]) -> None:
        if not self.url:
            log.warning("HANDOFF_WEBHOOK_URL vacío; evento de handoff solo registrado en logs")
            return
        try:
            (await self.http.post(self.url, json=event)).raise_for_status()
        except httpx.HTTPError:
            log.exception("No se pudo notificar el handoff")


class WindowClosedError(Exception):
    pass


def _describe(m: IncomingMessage) -> str:
    kinds = {"image": "imagen", "audio": "nota de voz", "document": "documento", "video": "vídeo",
             "sticker": "sticker"}
    if m.type in kinds:
        return f"[{kinds[m.type]}] {m.text}".strip()
    if m.type == "other":
        return "[mensaje no soportado]"
    return m.text


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(text.upper().split())


class Worker:
    def __init__(self, store: SessionStore, wa: WhatsAppClient, agent: Agent, handoff: HandoffNotifier,
                 *, debounce_seconds: float, max_history: int, max_media_bytes: int, pepper: str):
        self.store = store
        self.wa = wa
        self.agent = agent
        self.handoff = handoff
        self.debounce = debounce_seconds
        self.max_history = max_history
        self.max_media_bytes = max_media_bytes
        self.pepper = pepper

    # --- entrada desde la cola -----------------------------------------------

    async def handle_incoming(self, msg: IncomingMessage) -> None:
        if await self.store.seen(msg.message_id):
            return  # reentrega de Meta o de la cola
        await self.store.push(msg.wa_id, msg.to_dict())
        await self.process_user(msg.wa_id)

    async def process_user(self, wa_id: str) -> None:
        """Un único procesador por usuario: garantiza orden y agrupa ráfagas."""
        while True:
            token = await self.store.try_lock(wa_id)
            if token is None:
                return  # otro worker ya atiende a este usuario y drenará el buffer
            try:
                while True:
                    await asyncio.sleep(self.debounce)
                    batch = await self.store.drain(wa_id)
                    if not batch:
                        break
                    await self._handle_batch(wa_id, [IncomingMessage.from_dict(b) for b in batch])
            finally:
                await self.store.release_lock(wa_id, token)
            # Un mensaje pudo llegar entre el último drain y la liberación del lock.
            if not await self.store.has_pending(wa_id):
                return

    # --- un turno ------------------------------------------------------------

    async def _handle_batch(self, wa_id: str, batch: list[IncomingMessage]) -> None:
        started = time.monotonic()
        last = batch[-1]
        await self.wa.mark_read(last.message_id, typing=True)
        session = await self.store.load(wa_id)
        session.profile_name = last.profile_name or session.profile_name
        for m in batch:
            await self.store.record(wa_id, "user", _describe(m), name=session.profile_name)

        if await self._handle_commands(session, batch):
            return

        if session.mode == "human":
            await self.handoff.notify({"type": "message", "wa_id": wa_id,
                                       "messages": [m.to_dict() for m in batch]})
            return

        if any(m.button_id == BUTTON_HUMAN for m in batch):
            await self._escalate(session, "El usuario pulsó 'Hablar con agente'.", "media")
            await self.store.save(session)
            return

        parts = await self._build_parts(batch)
        if not parts:
            await self._say(wa_id, prompts.UNSUPPORTED_MEDIA)
            return

        try:
            result = await self.agent.run(load_history(session.history), parts, ToolContext(wa_id=wa_id))
        except Exception as exc:  # noqa: BLE001
            log.exception("El agente falló (usuario %s)", pseudonymize(wa_id, self.pepper))
            # Visible solo en el panel (el cliente recibe el mensaje genérico): ayuda a diagnosticar.
            await self.store.record(wa_id, "system", f"La IA no pudo responder: {describe_error(exc)}")
            await self._say(wa_id, prompts.FALLBACK_ERROR)
            self._track("agent_error", wa_id, started)
            await self.store.incr_metrics({"errors": 1})
            return

        prefix = ""
        if not session.disclosed:
            prefix = prompts.AI_DISCLOSURE + "\n\n"
            session.disclosed = True

        try:
            if result.handoff_summary is not None:
                await self._escalate(session, result.handoff_summary, result.handoff_priority, prefix)
            elif result.options:
                options = [{"id": f"opt:{i}", "title": o} for i, o in enumerate(result.options)]
                await self.wa.send_buttons(wa_id, prefix + result.text, options)
                await self.store.record(wa_id, "bot", prefix + result.text + "\n" +
                                        " · ".join(f"[{o}]" for o in result.options))
            else:
                await self._say(wa_id, prefix + (result.text or prompts.FALLBACK_ERROR))
        except WhatsAppError:
            # WhatsAppClient ya reintentó con backoff. No se guarda el turno para que el
            # historial no contenga una respuesta que el usuario nunca recibió.
            log.exception("No se pudo enviar la respuesta")
            self._track("send_error", wa_id, started)
            await self.store.incr_metrics({"errors": 1})
            return

        session.history = dump_history(result.history, self.max_history)
        await self.store.save(session)
        self._track("turn", wa_id, started, model=result.model, llm_ms=result.latency_ms,
                    tools=result.tools_used, handoff=result.handoff_summary is not None,
                    batch_size=len(batch), prompt_version=prompts.PROMPT_VERSION)
        await self.store.incr_metrics({"turns": 1, "latency_ms_sum": result.latency_ms,
                                       **{f"tool:{t}": 1 for t in set(result.tools_used)}})

    async def _handle_commands(self, session: Session, batch: list[IncomingMessage]) -> bool:
        if len(batch) != 1 or batch[0].type != "text":
            return False
        cmd = _normalize(batch[0].text)
        wa_id = session.wa_id
        if cmd in CMD_OPT_OUT:
            await self.store.set_opt_out(wa_id, True)
            await self._say(wa_id, prompts.OPT_OUT_MESSAGE)
        elif cmd in CMD_OPT_IN:
            await self.store.set_opt_out(wa_id, False)
            await self._say(wa_id, prompts.OPT_IN_MESSAGE)
        elif cmd in CMD_DELETE:
            # En producción: verifica la identidad y propaga el borrado a CRM, analítica y backups.
            await self.store.delete_user(wa_id)
            await self.wa.send_text(wa_id, prompts.DELETE_CONFIRMED)
            self._track("data_deleted", wa_id, time.monotonic())
        elif cmd in CMD_HUMAN and session.mode != "human":
            await self._escalate(session, "El usuario pidió hablar con una persona.", "media")
            await self.store.save(session)
        else:
            return False
        return True

    async def _escalate(self, session: Session, summary: str, priority: str, prefix: str = "") -> None:
        session.mode = "human"
        await self._say(session.wa_id, prefix + prompts.HANDOFF_MESSAGE)
        await self.store.record(session.wa_id, "system", f"Transferido a humano ({priority}): {summary}")
        await self.store.incr_metrics({"handoffs": 1})
        await self.handoff.notify({
            "type": "handoff",
            "wa_id": session.wa_id,
            "profile_name": session.profile_name,
            "summary": summary,
            "priority": priority,
            "history": session.history[-10:],
        })

    # --- acciones del panel web --------------------------------------------------

    async def send_agent_message(self, wa_id: str, text: str, agent_name: str = "") -> None:
        """Un humano responde desde el panel. Solo dentro de la ventana de 24 h."""
        meta = await self.store.meta(wa_id)
        if not meta["window_open"]:
            raise WindowClosedError("La ventana de 24 h está cerrada: solo se puede escribir con plantilla.")
        session = await self.store.load(wa_id)
        session.mode = "human"  # si un humano escribe, el bot se aparta
        await self.wa.send_text(wa_id, text)
        await self.store.record(wa_id, "agent", text)
        # Se añade al historial del LLM para que el bot tenga contexto cuando recupere la conversación.
        label = f"[Mensaje de {agent_name or 'un agente humano'}]: "
        session.history.append(types.Content(role="model", parts=[types.Part.from_text(text=label + text)])
                               .model_dump(mode="json", exclude_none=True))
        await self.store.save(session)
        await self.store.incr_metrics({"agent_messages": 1})

    async def set_mode(self, wa_id: str, mode: str) -> None:
        session = await self.store.load(wa_id)
        session.mode = mode
        await self.store.save(session)
        event = "Un agente tomó la conversación" if mode == "human" else "Conversación devuelta al bot"
        await self.store.record(wa_id, "system", event)

    async def _say(self, wa_id: str, text: str) -> None:
        await self.wa.send_text(wa_id, text)
        await self.store.record(wa_id, "bot", text)

    async def _build_parts(self, batch: list[IncomingMessage]) -> list[types.Part]:
        parts: list[types.Part] = []
        texts: list[str] = []
        for m in batch:
            if m.text:
                texts.append(m.text)
            if m.media_id and m.mime_type.startswith(GEMINI_MEDIA):
                try:
                    data, mime = await self.wa.download_media(m.media_id, self.max_media_bytes)
                except WhatsAppError:
                    log.warning("No se pudo descargar el media %s", m.media_id)
                    continue
                parts.append(types.Part.from_bytes(data=data, mime_type=mime.split(";")[0]))
        if texts:
            parts.insert(0, types.Part.from_text(text="\n".join(texts)))
        return parts

    def _track(self, event: str, wa_id: str, started: float, **fields: Any) -> None:
        """Evento estructurado (JSON) para Cloud Logging → BigQuery/Mixpanel. Sin PII."""
        record = {"event": event, "user": pseudonymize(wa_id, self.pepper),
                  "total_ms": int((time.monotonic() - started) * 1000), **fields}
        log.info(redact_pii(json.dumps(record, ensure_ascii=False)))
