"""Base de datos durable (SQLAlchemy async).

PostgreSQL en producción (`postgresql+asyncpg://...`) y SQLite en desarrollo, tests y demo
(`sqlite+aiosqlite://...`). Redis sigue guardando lo efímero: colas, bloqueos, deduplicación,
historial del LLM y contadores de métricas.

Las tablas se crean al arrancar con `create_all`. Cuando el esquema empiece a cambiar en
producción, pasa a migraciones con Alembic.
"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import (Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
                        event)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> float:
    return time.time()


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    """Una conversación por número de WhatsApp. `state` sigue la máquina de `app/states.py`."""

    __tablename__ = "conversations"
    wa_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    state: Mapped[str] = mapped_column(String(24), default="bot", index=True)
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    area: Mapped[str] = mapped_column(String(32), default="")
    priority: Mapped[str] = mapped_column(String(8), default="")
    handoff_summary: Mapped[str] = mapped_column(Text, default="")
    last_ts: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    last_role: Mapped[str] = mapped_column(String(16), default="")
    last_text: Mapped[str] = mapped_column(String(200), default="")
    last_user_ts: Mapped[float] = mapped_column(Float, default=0.0)
    state_since: Mapped[float] = mapped_column(Float, default=now)
    opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    disclosed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wa_id: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[float] = mapped_column(Float, default=now)
    role: Mapped[str] = mapped_column(String(16))  # user | bot | agent | system
    text: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(120), default="")  # asesor que escribió
    area: Mapped[str] = mapped_column(String(32), default="")
    prompt_version: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)


class OutboxItem(Base):
    """Respuesta pendiente de envío. La clave impide enviar dos veces lo mismo."""

    __tablename__ = "outbox"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    wa_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # text | buttons | template
    payload: Mapped[str] = mapped_column(Text)  # JSON
    text: Mapped[str] = mapped_column(Text, default="")  # lo que verá el panel
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    next_attempt_at: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[str] = mapped_column(Text, default="{}")  # JSON: área, versión de prompt, modelo
    created_at: Mapped[float] = mapped_column(Float, default=now)
    sent_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("bank", "receipt_number", name="uq_payment_receipt"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract: Mapped[str] = mapped_column(String(32), index=True)
    bank: Mapped[str] = mapped_column(String(80))
    receipt_number: Mapped[str] = mapped_column(String(64))
    amount_cents: Mapped[int] = mapped_column(Integer)
    paid_on: Mapped[str] = mapped_column(String(10))  # AAAA-MM-DD
    image_sha256: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    wa_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    applied_to: Mapped[str] = mapped_column(Text, default="")  # meses aplicados
    created_at: Mapped[float] = mapped_column(Float, default=now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float, default=now, index=True)
    actor: Mapped[str] = mapped_column(String(120))  # email del asesor, "bot" o "sistema"
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(Text, default="{}")


class ChannelIdentity(Base):
    """Vínculo entre la identidad de WhatsApp y el cliente del ISP (cédula)."""

    __tablename__ = "channel_identities"
    wa_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    bsuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    verified_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(16))  # owner | admin | asesor
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Review(Base):
    """Respuesta del bot marcada como mala por un asesor (entrada de Revisión)."""

    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    wa_id: Mapped[str] = mapped_column(String(32), index=True)
    reported_by: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(32), default="otro")
    note: Mapped[str] = mapped_column(Text, default="")  # qué debió responder
    status: Mapped[str] = mapped_column(String(12), default="abierto", index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("rules.id"), nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Rule(Base):
    """Regla aprendida en Revisión; las activas se añaden al prompt del área."""

    __tablename__ = "rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    area: Mapped[str] = mapped_column(String(32))  # un área o "todas"
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[float] = mapped_column(Float, default=now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON
    updated_at: Mapped[float] = mapped_column(Float, default=now)
    updated_by: Mapped[str] = mapped_column(String(120), default="")


Index("ix_messages_wa_ts", Message.wa_id, Message.ts)


class Database:
    def __init__(self, url: str):
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite"):
            if ":memory:" in url or url.rstrip("/").endswith("sqlite+aiosqlite:"):
                # En memoria todas las sesiones compartirían una conexión y se pisarían las
                # transacciones: usa siempre un archivo (la demo y los tests usan uno temporal).
                raise ValueError("SQLite en memoria no está soportado; usa un archivo, p. ej. sqlite+aiosqlite:///./whatsappia.db")
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            kwargs.update(pool_size=5, max_overflow=10, pool_pre_ping=True)
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA foreign_keys=ON")  # SQLite no aplica las FK por defecto
                cur.execute("PRAGMA journal_mode=WAL")  # lectores y un escritor a la vez
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
