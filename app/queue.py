"""Desacople webhook → worker.

- LocalQueue: asyncio en el mismo proceso (desarrollo, o una sola instancia).
- PubSubQueue: Google Pub/Sub con ordering key = wa_id. Una suscripción push
  entrega cada mensaje en POST /tasks/pubsub (ver main.py y README).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Protocol

from .parsing import IncomingMessage

log = logging.getLogger(__name__)


class Queue(Protocol):
    async def publish(self, msg: IncomingMessage) -> None: ...


class LocalQueue:
    def __init__(self, handler: Callable[[IncomingMessage], Awaitable[None]]):
        self.handler = handler
        self._tasks: set[asyncio.Task] = set()

    async def publish(self, msg: IncomingMessage) -> None:
        task = asyncio.create_task(self._run(msg))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, msg: IncomingMessage) -> None:
        try:
            await self.handler(msg)
        except Exception:  # noqa: BLE001
            log.exception("Fallo procesando el mensaje %s", msg.message_id)

    async def drain(self) -> None:
        """Espera a que terminen las tareas en curso (tests y apagado ordenado)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


class PubSubQueue:
    def __init__(self, topic: str):
        from google.cloud import pubsub_v1  # import diferido: dependencia solo en producción

        self.topic = topic
        self.publisher = pubsub_v1.PublisherClient(
            publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
        )

    async def publish(self, msg: IncomingMessage) -> None:
        data = json.dumps(msg.to_dict()).encode()
        future = self.publisher.publish(self.topic, data, ordering_key=msg.wa_id)
        await asyncio.wrap_future(future)
