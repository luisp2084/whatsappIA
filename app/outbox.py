"""Outbox idempotente: toda respuesta se guarda antes de enviarse y se envía una sola vez.

- Clave = sha256(wa_id, turn_id, n.º de mensaje del turno). Si el turno se reprocesa (reentrega
  de Pub/Sub, reinicio), la clave coincide y no se envía de nuevo, aunque el LLM redacte distinto.
- Si WhatsApp falla, el mensaje queda `pending` y se reintenta con espera creciente; tras
  MAX_ATTEMPTS pasa a `failed` y el panel lo muestra.
- Guardarraíl: si el bot va a enviar un texto idéntico a uno de los últimos 3 enviados a ese número
  en los últimos 10 minutos, se bloquea (`blocked`) y se avisa en el panel.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Protocol

from .db import OutboxItem
from .repo import Repo
from .session import SessionStore
from .whatsapp import WhatsAppError

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
DUPLICATE_WINDOW_S = 600


class Channel(Protocol):
    async def send_text(self, to: str, text: str) -> None: ...
    async def send_buttons(self, to: str, body: str, options: list[dict[str, str]]) -> None: ...
    async def send_template(self, to: str, name: str, language: str, params: list[str]) -> None: ...
    async def mark_read(self, message_id: str, typing: bool = True) -> None: ...
    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]: ...


def outbox_key(wa_id: str, turn_id: str, seq: int) -> str:
    return hashlib.sha256(f"{wa_id}|{turn_id}|{seq}".encode()).hexdigest()


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


class Outbox:
    def __init__(self, repo: Repo, channel: Channel, store: SessionStore):
        self.repo = repo
        self.channel = channel
        self.store = store

    async def enqueue(self, wa_id: str, turn_id: str, seq: int, kind: str, payload: dict[str, Any], text: str,
                      *, role: str = "bot", author: str = "", area: str = "", prompt_version: str = "",
                      model: str = "", deliver: bool = True) -> str | None:
        """Guarda la respuesta y la envía. Devuelve la clave, o None si ya existía o se bloqueó."""
        key = outbox_key(wa_id, turn_id, seq)
        meta = json.dumps({"role": role, "author": author, "area": area, "prompt_version": prompt_version,
                           "model": model})
        if role == "bot" and kind != "template" and await self._is_repeat(wa_id, text):
            await self.repo.outbox_add(OutboxItem(key=key, wa_id=wa_id, kind=kind, payload=json.dumps(payload),
                                                  text=text, status="blocked", meta=meta))
            await self.repo.record(wa_id, "system", "Respuesta bloqueada: repetía un mensaje enviado hace poco. "
                                                    f"Texto: {text[:150]}")
            await self.store.incr_metrics({"duplicate_blocked": 1})
            log.warning('{"event": "duplicate_blocked", "kind": "%s"}', kind)
            return None
        inserted = await self.repo.outbox_add(OutboxItem(key=key, wa_id=wa_id, kind=kind, payload=json.dumps(payload),
                                                         text=text, meta=meta))
        if not inserted:
            return None  # mismo turno reprocesado: ya estaba en el outbox
        if deliver:
            await self.deliver(key)
        return key

    async def _is_repeat(self, wa_id: str, text: str) -> bool:
        recent = await self.repo.last_sent_texts(wa_id, 3, since=time.time() - DUPLICATE_WINDOW_S)
        return _norm(text) in {_norm(t) for t in recent}

    async def deliver(self, key: str) -> bool:
        if not await self.repo.outbox_claim(key):
            return False  # otro proceso lo está enviando o ya se envió
        item = await self.repo.outbox_get(key)
        payload = json.loads(item.payload)
        meta = json.loads(item.meta or "{}")
        try:
            if item.kind == "text":
                await self.channel.send_text(item.wa_id, payload["text"])
            elif item.kind == "buttons":
                await self.channel.send_buttons(item.wa_id, payload["body"], payload["options"])
            elif item.kind == "template":
                await self.channel.send_template(item.wa_id, payload["name"], payload["language"], payload["params"])
            else:
                raise WhatsAppError(f"tipo desconocido: {item.kind}")
        except WhatsAppError as exc:
            attempts = item.attempts + 1
            if attempts >= MAX_ATTEMPTS:
                await self.repo.outbox_update(key, status="failed", attempts=attempts, last_error=str(exc)[:500])
                await self.repo.record(item.wa_id, "system", f"No se pudo entregar un mensaje tras {attempts} "
                                                             f"intentos: {str(exc)[:200]}")
                await self.store.incr_metrics({"send_failed": 1})
            else:
                delay = min(15 * 2 ** (attempts - 1), 900)  # 15 s, 30 s, 1 min... hasta 15 min
                await self.repo.outbox_update(key, status="pending", attempts=attempts, last_error=str(exc)[:500],
                                              next_attempt_at=time.time() + delay)
                await self.store.incr_metrics({"send_retries": 1})
            log.warning('{"event": "send_error", "attempt": %d, "error": %s}', attempts, json.dumps(str(exc)[:200]))
            return False
        await self.repo.outbox_update(key, status="sent", attempts=item.attempts + 1, sent_at=time.time())
        await self.repo.record(item.wa_id, meta.get("role", "bot"), item.text, author=meta.get("author", ""),
                               area=meta.get("area", ""), prompt_version=meta.get("prompt_version", ""),
                               model=meta.get("model", ""))
        return True

    async def flush(self) -> int:
        sent = 0
        for item in await self.repo.outbox_due():
            sent += await self.deliver(item.key)
        return sent

    async def run_forever(self, interval: float = 10.0) -> None:
        """Bucle del servicio `wa-sender` (o de la app completa): reintenta los pendientes."""
        while True:
            try:
                await self.flush()
            except Exception:  # noqa: BLE001
                log.exception("Fallo en el ciclo del outbox")
            await asyncio.sleep(interval)
