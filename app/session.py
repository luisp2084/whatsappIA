"""Estado en Redis: sesiones con TTL, deduplicación, buffer de ráfagas y locks por usuario."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import asdict, dataclass, field
from typing import Any

from redis.asyncio import Redis

DEDUPE_TTL = 24 * 3600
TRANSCRIPT_MAX = 300
METRICS_TTL = 90 * 24 * 3600
WINDOW_SECONDS = 24 * 3600  # ventana de servicio de WhatsApp


@dataclass
class Session:
    wa_id: str
    history: list[dict[str, Any]] = field(default_factory=list)  # types.Content serializados
    mode: str = "bot"  # "bot" | "human"
    disclosed: bool = False  # ya se informó de que es una IA
    profile_name: str = ""

    @property
    def is_new(self) -> bool:
        return not self.history and not self.disclosed


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int):
        self.r = redis
        self.ttl = ttl_seconds

    # --- idempotencia --------------------------------------------------------

    async def seen(self, message_id: str) -> bool:
        """True si el mensaje ya se procesó (Meta puede reenviar el mismo evento)."""
        created = await self.r.set(f"dedupe:{message_id}", 1, nx=True, ex=DEDUPE_TTL)
        return not created

    # --- buffer de ráfagas ---------------------------------------------------

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

    # --- lock por usuario (un solo worker por conversación) -------------------

    async def try_lock(self, wa_id: str, ttl: int = 120) -> str | None:
        token = uuid.uuid4().hex
        ok = await self.r.set(f"lock:{wa_id}", token, nx=True, ex=ttl)
        return token if ok else None

    async def release_lock(self, wa_id: str, token: str) -> None:
        # Borra solo si el lock sigue siendo nuestro.
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
        await self.r.eval(script, 1, f"lock:{wa_id}", token)

    # --- sesión --------------------------------------------------------------

    async def load(self, wa_id: str) -> Session:
        raw = await self.r.get(f"session:{wa_id}")
        if not raw:
            return Session(wa_id=wa_id)
        return Session(**json.loads(raw))

    async def save(self, session: Session) -> None:
        await self.r.set(f"session:{session.wa_id}", json.dumps(asdict(session)), ex=self.ttl)

    # --- opt-out (sin TTL: debe persistir aunque caduque la sesión) ----------

    async def set_opt_out(self, wa_id: str, value: bool) -> None:
        if value:
            await self.r.set(f"optout:{wa_id}", 1)
        else:
            await self.r.delete(f"optout:{wa_id}")

    async def is_opted_out(self, wa_id: str) -> bool:
        return bool(await self.r.exists(f"optout:{wa_id}"))

    async def delete_user(self, wa_id: str) -> None:
        """Derecho de supresión: elimina todo lo que guardamos de este usuario."""
        await self.r.delete(f"session:{wa_id}", f"buf:{wa_id}", f"transcript:{wa_id}", f"meta:{wa_id}")
        await self.r.zrem("convs", wa_id)

    # --- transcripción e índice para el panel web ------------------------------

    async def record(self, wa_id: str, role: str, text: str, name: str = "") -> None:
        """role: user | bot | agent | system. Alimenta el panel; el LLM usa `history`."""
        now = time.time()
        item = json.dumps({"ts": now, "role": role, "text": text}, ensure_ascii=False)
        meta = {"last_ts": now, "last_role": role, "last_text": text[:200]}
        if role == "user":
            meta["last_user_ts"] = now
        if name:
            meta["name"] = name
        async with self.r.pipeline(transaction=False) as pipe:
            pipe.rpush(f"transcript:{wa_id}", item)
            pipe.ltrim(f"transcript:{wa_id}", -TRANSCRIPT_MAX, -1)
            pipe.expire(f"transcript:{wa_id}", self.ttl)
            pipe.hset(f"meta:{wa_id}", mapping=meta)
            pipe.expire(f"meta:{wa_id}", self.ttl)
            pipe.zadd("convs", {wa_id: now})
            await pipe.execute()

    async def transcript(self, wa_id: str) -> list[dict[str, Any]]:
        return [json.loads(i) for i in await self.r.lrange(f"transcript:{wa_id}", 0, -1)]

    async def meta(self, wa_id: str) -> dict[str, Any]:
        raw = await self.r.hgetall(f"meta:{wa_id}")
        last_user = float(raw.get("last_user_ts", 0) or 0)
        session = await self.load(wa_id)
        return {
            "wa_id": wa_id,
            "name": raw.get("name", "") or session.profile_name,
            "mode": session.mode,
            "last_ts": float(raw.get("last_ts", 0) or 0),
            "last_role": raw.get("last_role", ""),
            "last_text": raw.get("last_text", ""),
            "window_open": time.time() - last_user < WINDOW_SECONDS,
            "window_closes_at": last_user + WINDOW_SECONDS if last_user else None,
            "opted_out": await self.is_opted_out(wa_id),
        }

    async def list_conversations(self, limit: int = 100) -> list[dict[str, Any]]:
        out = []
        for wa_id in await self.r.zrevrange("convs", 0, limit - 1):
            if not await self.r.exists(f"meta:{wa_id}"):
                await self.r.zrem("convs", wa_id)  # caducó por TTL
                continue
            out.append(await self.meta(wa_id))
        return out

    # --- métricas diarias (agregadas, sin PII) -----------------------------------

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
