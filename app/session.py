"""Estado efímero en Redis: historial del LLM, deduplicación, buffer de ráfagas, locks,
límites de frecuencia, datos temporales y contadores de métricas.

Lo que no puede caducar (conversaciones, mensajes, pagos, auditoría) vive en la base de datos
(`app/repo.py`).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from redis.asyncio import Redis

DEDUPE_TTL = 24 * 3600
METRICS_TTL = 90 * 24 * 3600


@dataclass
class Session:
    """Memoria de corto plazo de la IA para un número."""

    wa_id: str
    history: list[dict[str, Any]] = field(default_factory=list)  # types.Content serializados
    summary: str = ""  # resumen de lo recortado del historial


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self.r = redis
        self.ttl = ttl_seconds

    # --- idempotencia ---------------------------------------------------------------

    async def seen(self, message_id: str) -> bool:
        """True si el mensaje ya llegó (Meta y Pub/Sub pueden reentregar el mismo evento)."""
        created = await self.r.set(f"dedupe:{message_id}", 1, nx=True, ex=DEDUPE_TTL)
        return not created

    # --- buffer de ráfagas ----------------------------------------------------------

    async def push(self, wa_id: str, item: dict[str, Any]) -> None:
        key = f"buf:{wa_id}"
        await self.r.rpush(key, json.dumps(item))
        await self.r.expire(key, 3600)

    async def drain(self, wa_id: str) -> list[dict[str, Any]]:
        key = f"buf:{wa_id}"
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            items, _ = await pipe.execute()
        return [json.loads(i) for i in items]

    async def has_pending(self, wa_id: str) -> bool:
        return bool(await self.r.llen(f"buf:{wa_id}"))

    async def users_with_pending(self) -> list[str]:
        """Buffers que quedaron sin procesar (p. ej. el proceso se reinició a mitad de un turno)."""
        return [k.split(":", 1)[1] async for k in self.r.scan_iter(match="buf:*", count=200)]

    # --- lock por conversación ------------------------------------------------------

    async def try_lock(self, wa_id: str, ttl: int = 120) -> str | None:
        token = uuid.uuid4().hex
        ok = await self.r.set(f"lock:{wa_id}", token, nx=True, ex=ttl)
        return token if ok else None

    async def release_lock(self, wa_id: str, token: str) -> None:
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
        await self.r.eval(script, 1, f"lock:{wa_id}", token)

    async def is_locked(self, wa_id: str) -> bool:
        return bool(await self.r.exists(f"lock:{wa_id}"))

    # --- límites de frecuencia y datos temporales ------------------------------------

    async def allow(self, key: str, every_seconds: int) -> bool:
        """True como mucho una vez cada `every_seconds` para esa clave (p. ej. reiniciar una ONT)."""
        return bool(await self.r.set(f"rate:{key}", 1, nx=True, ex=every_seconds))

    async def retry_in(self, key: str) -> int:
        return max(0, int(await self.r.ttl(f"rate:{key}")))

    async def put_json(self, key: str, value: Any, ttl: int) -> None:
        await self.r.set(f"tmp:{key}", json.dumps(value, ensure_ascii=False), ex=ttl)

    async def get_json(self, key: str) -> Any:
        raw = await self.r.get(f"tmp:{key}")
        return json.loads(raw) if raw else None

    async def pop_json(self, key: str) -> Any:
        raw = await self.r.getdel(f"tmp:{key}")
        return json.loads(raw) if raw else None

    # --- sesión del LLM ----------------------------------------------------------------

    async def load(self, wa_id: str) -> Session:
        raw = await self.r.get(f"session:{wa_id}")
        if not raw:
            return Session(wa_id=wa_id)
        data = json.loads(raw)
        return Session(wa_id=wa_id, history=data.get("history", []), summary=data.get("summary", ""))

    async def save(self, session: Session) -> None:
        await self.r.set(f"session:{session.wa_id}", json.dumps(asdict(session)), ex=self.ttl)

    async def delete_user(self, wa_id: str) -> None:
        await self.r.delete(f"session:{wa_id}", f"buf:{wa_id}")
        async for key in self.r.scan_iter(match=f"tmp:*{wa_id}*", count=200):
            await self.r.delete(key)

    # --- métricas diarias (agregadas, sin PII) -------------------------------------------

    async def incr_metrics(self, values: dict[str, int]) -> None:
        key = f"metrics:{datetime.now(timezone.utc):%Y-%m-%d}"
        async with self.r.pipeline(transaction=False) as pipe:
            for field_name, n in values.items():
                pipe.hincrby(key, field_name, n)
            pipe.expire(key, METRICS_TTL)
            await pipe.execute()

    async def metrics(self, days: int = 7) -> list[dict[str, Any]]:
        today = datetime.now(timezone.utc).date()
        rows = []
        for i in range(days - 1, -1, -1):
            day = today - timedelta(days=i)
            raw = await self.r.hgetall(f"metrics:{day:%Y-%m-%d}")
            rows.append({"date": f"{day:%Y-%m-%d}", **{k: int(v) for k, v in raw.items()}})
        return rows
