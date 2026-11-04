"""Orquestador: procesa los mensajes encolados y las acciones de los asesores.

Por cada turno: agrupa la ráfaga, registra los mensajes, aplica comandos y estados, elige el área,
arma el prompt del área, ejecuta el agente con las herramientas permitidas y deja la respuesta en
el outbox, que la envía una sola vez.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import unicodedata
import uuid
from typing import Any, Protocol

import httpx
from google.genai import types

from . import prompts, states
from .agent import Agent, TurnSpec, describe_error, dump_history, load_history
from .areas import Classifier, KeywordClassifier, area as get_area
from .control import ControlCenter
from .isp import ISP
from .outbox import Channel, Outbox
from .parsing import IncomingMessage
from .repo import Repo
from .security import pseudonymize, redact_pii
from .session import SessionStore
from .tools import ToolContext
from .whatsapp import WhatsAppError

log = logging.getLogger(__name__)

GEMINI_MEDIA = ("image/", "audio/", "application/pdf")
CMD_HUMAN = {"AGENTE", "HUMANO", "PERSONA", "ASESOR"}
CMD_OPT_OUT = {"STOP", "BAJA"}
CMD_OPT_IN = {"ALTA"}
CMD_DELETE = {"BORRAR MIS DATOS"}
CMD_DELETE_CONFIRM = "CONFIRMAR BORRADO"
BUTTON_HUMAN = "human"


class HandoffNotifier(Protocol):
    async def notify(self, event: dict[str, Any]) -> None: ...


class WebhookHandoff:
    """Opcional: avisa a otro inbox (Chatwoot, Zendesk, n8n...) además del panel propio."""

    def __init__(self, url: str):
        self.url = url
        self.http = httpx.AsyncClient(timeout=5.0)

    async def notify(self, event: dict[str, Any]) -> None:
        if not self.url:
            return
        try:
            (await self.http.post(self.url, json=event)).raise_for_status()
        except httpx.HTTPError:
            log.exception("No se pudo notificar el handoff")


class WindowClosedError(Exception):
    pass


class ActionError(Exception):
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
    def __init__(self, store: SessionStore, repo: Repo, channel: Channel, agent: Agent, isp: ISP,
                 handoff: HandoffNotifier, *, classifier: Classifier | None = None, debounce_seconds: float,
                 max_history: int, max_media_bytes: int, pepper: str):
        self.store = store
        self.repo = repo
        self.channel = channel
        self.agent = agent
        self.isp = isp
        self.handoff = handoff
        self.classifier = classifier or KeywordClassifier()
        self.outbox = Outbox(repo, channel, store)
        self.debounce = debounce_seconds
        self.max_history = max_history
        self.max_media_bytes = max_media_bytes
        self.pepper = pepper

    # --- entrada desde la cola ------------------------------------------------------

    async def handle_incoming(self, msg: IncomingMessage) -> None:
        if await self.store.seen(msg.message_id):
            return  # reentrega de Meta o de la cola
        await self.store.push(msg.wa_id, msg.to_dict())
        await self.process_user(msg.wa_id)

    async def process_user(self, wa_id: str) -> None:
        """Un único procesador por conversación: garantiza el orden y agrupa ráfagas."""
        while True:
            token = await self.store.try_lock(wa_id)
            if token is None:
                return  # otro proceso atiende a este número y drenará el buffer
            try:
                while True:
                    await asyncio.sleep(self.debounce)
                    batch = await self.store.drain(wa_id)
                    if not batch:
                        break
                    await self._handle_batch(wa_id, [IncomingMessage.from_dict(b) for b in batch])
            finally:
                await self.store.release_lock(wa_id, token)
            if not await self.store.has_pending(wa_id):
                return

    async def recover(self) -> int:
        """Procesa buffers huérfanos (el proceso se reinició a mitad de un turno)."""
        count = 0
        for wa_id in await self.store.users_with_pending():
            if not await self.store.is_locked(wa_id):
                await self.process_user(wa_id)
                count += 1
        return count

    # --- un turno ------------------------------------------------------------------------

    async def _handle_batch(self, wa_id: str, batch: list[IncomingMessage]) -> None:
        started = time.monotonic()
        last = batch[-1]
        turn_id = last.message_id
        await self.channel.mark_read(last.message_id, typing=True)
        name = next((m.profile_name for m in reversed(batch) if m.profile_name), "")
        for m in batch:
            await self.repo.record(wa_id, "user", _describe(m), name=name)
        await self.repo.transition(wa_id, "customer_message")  # cerrada -> bot
        conv = await self.repo.get_conversation(wa_id)
        cc = await self.repo.control()

        if await self._handle_commands(wa_id, conv.state, batch, turn_id, cc):
            return

        if conv.state in states.HUMAN_STATES:
            await self.handoff.notify({"type": "message", "wa_id": wa_id, "messages": [m.to_dict() for m in batch]})
            return

        if any(m.button_id == BUTTON_HUMAN for m in batch):
            await self._escalate(wa_id, "El cliente pulsó 'Hablar con un asesor'.", "media", turn_id, cc)
            return

        if not cc.bot_enabled:  # interruptor de emergencia
            await self._escalate(wa_id, "Bot apagado desde el panel.", "media", turn_id, cc,
                                 message=prompts.BOT_OFF_MESSAGE)
            return

        parts, media = await self._build_parts(batch)
        if not parts:
            await self._say(wa_id, prompts.UNSUPPORTED_MEDIA, turn_id)
            return

        text = "\n".join(m.text for m in batch if m.text)
        classification = await self.classifier.classify(text, conv.area, bool(media))
        area = get_area(classification.area)
        if classification.quiere_humano:
            await self._escalate(wa_id, f"El cliente pidió un asesor: {text[:200]}", "media", turn_id, cc, area.name)
            return
        if not cc.area_enabled(area.name):
            await self._escalate(wa_id, f"Área '{area.name}' desactivada en el Centro de control. Mensaje: {text[:200]}",
                                 "media", turn_id, cc, area.name)
            return

        session = await self.store.load(wa_id)
        rules = await self.repo.active_rules(area.name)
        ident = await self.repo.identity(wa_id)
        system_prompt, version = prompts.build_system_prompt(
            area.name, cc, rules, now=cc.now(),
            customer=f"cédula {ident.customer_id}" if ident and ident.customer_id else "")
        spec = TurnSpec(system_prompt=system_prompt, declarations=area.declarations, allowed=area.allowed,
                        temperature=cc.areas[area.name].temperature)
        ctx = ToolContext(wa_id=wa_id, repo=self.repo, store=self.store, isp=self.isp, control=cc, media=media)

        try:
            result = await self.agent.run(load_history(session.history), parts, ctx, spec)
        except Exception as exc:  # noqa: BLE001
            log.exception("El agente falló (usuario %s)", pseudonymize(wa_id, self.pepper))
            await self.repo.record(wa_id, "system", f"La IA no pudo responder: {describe_error(exc)}")
            await self._say(wa_id, prompts.FALLBACK_ERROR, turn_id)
            self._track("agent_error", wa_id, started, area=area.name)
            await self.store.incr_metrics({"errors": 1})
            return

        prefix = ""
        if not conv.disclosed:
            prefix = prompts.ai_disclosure(cc) + "\n\n"
            await self.repo.update_conversation(wa_id, disclosed=True)
        await self.repo.update_conversation(wa_id, area=area.name)
        common = {"area": area.name, "prompt_version": version, "model": result.model}

        if result.handoff_summary is not None:
            await self._escalate(wa_id, result.handoff_summary, result.handoff_priority, turn_id, cc, area.name,
                                 prefix=prefix, preface=result.text if result.steps_exhausted else "")
        elif result.options:
            options = [{"id": f"opt:{i}", "title": o[:20]} for i, o in enumerate(result.options)]
            shown = prefix + result.text + "\n" + " · ".join(f"[{o}]" for o in result.options)
            await self.outbox.enqueue(wa_id, turn_id, 0, "buttons",
                                      {"body": prefix + result.text, "options": options}, shown, **common)
        else:
            body = prefix + (result.text or prompts.FALLBACK_ERROR)
            await self.outbox.enqueue(wa_id, turn_id, 0, "text", {"text": body}, body, **common)

        session.history = dump_history(result.history, self.max_history)
        await self.store.save(session)
        self._track("turn", wa_id, started, model=result.model, llm_ms=result.latency_ms, tokens=result.tokens,
                    area=area.name, tools=result.tools_used, tool_errors=ctx.errors,
                    handoff=result.handoff_summary is not None, batch_size=len(batch), prompt_version=version)
        await self.store.incr_metrics({"turns": 1, "latency_ms_sum": result.latency_ms, "tokens": result.tokens,
                                       f"area:{area.name}": 1, **{f"tool:{t}": 1 for t in set(result.tools_used)}})

    async def _handle_commands(self, wa_id: str, state: str, batch: list[IncomingMessage], turn_id: str,
                               cc: ControlCenter) -> bool:
        if len(batch) != 1 or batch[0].type != "text":
            return False
        cmd = _normalize(batch[0].text)
        if cmd in CMD_OPT_OUT:
            await self.repo.update_conversation(wa_id, opted_out=True)
            await self._say(wa_id, prompts.OPT_OUT_MESSAGE, turn_id)
        elif cmd in CMD_OPT_IN:
            await self.repo.update_conversation(wa_id, opted_out=False)
            await self._say(wa_id, prompts.OPT_IN_MESSAGE, turn_id)
        elif cmd in CMD_DELETE:
            code = f"{secrets.randbelow(10000):04d}"
            await self.store.put_json(f"delete:{wa_id}", code, 600)
            await self._say(wa_id, prompts.DELETE_ASK.format(code=code), turn_id)
        elif cmd.startswith(CMD_DELETE_CONFIRM):
            expected = await self.store.pop_json(f"delete:{wa_id}")
            if expected and cmd.split()[-1] == expected:
                await self.delete_customer(wa_id, actor="cliente")
                await self.channel.send_text(wa_id, prompts.DELETE_CONFIRMED)  # fuera del outbox: ya no queda rastro
            else:
                await self._say(wa_id, prompts.DELETE_BAD_CODE, turn_id)
        elif cmd in CMD_HUMAN and state not in states.HUMAN_STATES:
            await self._escalate(wa_id, "El cliente pidió hablar con una persona.", "media", turn_id, cc)
        else:
            return False
        return True

    async def _escalate(self, wa_id: str, summary: str, priority: str, turn_id: str, cc: ControlCenter,
                        area: str | None = None, *, prefix: str = "", preface: str = "",
                        message: str | None = None) -> None:
        conv = await self.repo.get_conversation(wa_id)
        if conv.state not in states.HUMAN_STATES:
            await self.repo.transition(wa_id, "escalate", area=area, priority=priority, summary=summary)
        body = prefix + (preface + "\n\n" if preface else "") + (message or prompts.handoff_message(cc))
        await self.outbox.enqueue(wa_id, turn_id, 0, "text", {"text": body}, body, area=area or "")
        await self.repo.record(wa_id, "system", f"Derivado a un asesor (prioridad {priority}): {summary}")
        await self.store.incr_metrics({"handoffs": 1})
        await self.handoff.notify({"type": "handoff", "wa_id": wa_id, "summary": summary, "priority": priority,
                                   "area": area or ""})

    # --- acciones del panel ----------------------------------------------------------------

    async def take(self, wa_id: str, user: Any) -> None:
        await self.repo.transition(wa_id, "take", assigned_to=user.id)
        await self.repo.record(wa_id, "system", f"{user.name} tomó la conversación")
        await self.repo.audit(user.email, "conversation.take", wa_id)

    async def close(self, wa_id: str, user: Any) -> None:
        await self.repo.transition(wa_id, "close")
        await self.repo.record(wa_id, "system", f"{user.name} cerró la conversación")
        await self.repo.audit(user.email, "conversation.close", wa_id)

    async def release(self, wa_id: str, user: Any) -> None:
        await self.repo.transition(wa_id, "release")
        await self.repo.record(wa_id, "system", f"{user.name} devolvió la conversación al bot")
        await self.repo.audit(user.email, "conversation.release", wa_id)

    async def send_agent_message(self, wa_id: str, text: str, user: Any) -> None:
        """Un asesor escribe desde el panel: toma la conversación y el bot se aparta."""
        view = await self.repo.conversation_view(wa_id, limit=1)
        if view is None:
            raise ActionError("Conversación no encontrada")
        if not view["meta"]["window_open"]:
            raise WindowClosedError("La ventana de 24 h está cerrada: solo se puede escribir con una plantilla.")
        await self.repo.transition(wa_id, "agent_message", assigned_to=user.id)
        key = await self.outbox.enqueue(wa_id, f"agent:{uuid.uuid4().hex}", 0, "text", {"text": text}, text,
                                        role="agent", author=user.name)
        item = await self.repo.outbox_get(key) if key else None
        if item is None or item.status != "sent":
            raise ActionError("WhatsApp no aceptó el mensaje; se reintentará automáticamente.")
        # Al historial de la IA, para que tenga contexto si la conversación vuelve al bot.
        session = await self.store.load(wa_id)
        session.history.append(types.Content(role="model", parts=[types.Part.from_text(
            text=f"[Mensaje del asesor {user.name}]: {text}")]).model_dump(mode="json", exclude_none=True))
        await self.store.save(session)
        await self.store.incr_metrics({"agent_messages": 1})
        await self.repo.audit(user.email, "conversation.message", wa_id)

    async def send_template(self, wa_id: str, name: str, params: list[str], user: Any) -> None:
        cc = await self.repo.control()
        template = cc.template(name)
        if template is None:
            raise ActionError("Plantilla desconocida")
        if len(params) != template.params:
            raise ActionError(f"La plantilla necesita {template.params} valor(es)")
        conv = await self.repo.get_conversation(wa_id)
        if conv is None:
            raise ActionError("Conversación no encontrada")
        if conv.opted_out and template.category == "marketing":
            raise ActionError("El cliente pidió no recibir avisos (STOP)")
        text = template.body
        for i, value in enumerate(params, 1):
            text = text.replace(f"{{{{{i}}}}}", value)
        key = await self.outbox.enqueue(
            wa_id, f"template:{uuid.uuid4().hex}", 0, "template",
            {"name": template.name, "language": template.language, "params": params},
            f"[Plantilla {template.name}] {text}", role="agent", author=user.name)
        item = await self.repo.outbox_get(key) if key else None
        if item is None or item.status != "sent":
            raise ActionError("WhatsApp no aceptó la plantilla; se reintentará automáticamente.")
        await self.repo.transition(wa_id, "agent_message", assigned_to=user.id)
        await self.repo.audit(user.email, "conversation.template", wa_id, {"template": name})

    async def delete_customer(self, wa_id: str, actor: str) -> dict[str, int]:
        counts = await self.repo.delete_customer(wa_id)
        await self.store.delete_user(wa_id)
        await self.repo.audit(actor, "data.deleted", pseudonymize(wa_id, self.pepper), counts)
        return counts

    # --- utilidades -------------------------------------------------------------------------

    async def _say(self, wa_id: str, text: str, turn_id: str, seq: int = 0) -> None:
        await self.outbox.enqueue(wa_id, turn_id, seq, "text", {"text": text}, text)

    async def _build_parts(self, batch: list[IncomingMessage]) -> tuple[list[types.Part], list[tuple[bytes, str]]]:
        parts: list[types.Part] = []
        media: list[tuple[bytes, str]] = []
        texts: list[str] = []
        for m in batch:
            if m.text:
                texts.append(m.text)
            if m.media_id and m.mime_type.startswith(GEMINI_MEDIA):
                try:
                    data, mime = await self.channel.download_media(m.media_id, self.max_media_bytes)
                except WhatsAppError:
                    log.warning("No se pudo descargar el media %s", m.media_id)
                    continue
                mime = mime.split(";")[0]
                media.append((data, mime))
                parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        if texts:
            parts.insert(0, types.Part.from_text(text="\n".join(texts)))
        elif media:
            parts.insert(0, types.Part.from_text(text="(El cliente envió un archivo sin texto.)"))
        return parts, media

    def _track(self, event: str, wa_id: str, started: float, **fields: Any) -> None:
        """Evento estructurado (JSON) para Cloud Logging → métricas y alertas. Sin PII."""
        record = {"event": event, "user": pseudonymize(wa_id, self.pepper),
                  "total_ms": int((time.monotonic() - started) * 1000), **fields}
        log.info(redact_pii(json.dumps(record, ensure_ascii=False)))
