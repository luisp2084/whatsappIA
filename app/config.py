"""Configuración leída de variables de entorno (ver .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Falta la variable de entorno obligatoria {name}")
    return value or ""


# Modelos por defecto. Google retira modelos con frecuencia: revisa
# https://ai.google.dev/gemini-api/docs/deprecations y cámbialos con GEMINI_MODEL / GEMINI_FALLBACK_MODEL.
DEFAULT_MODEL = "gemini-3.5-flash"
DEFAULT_FALLBACK_MODEL = "gemini-flash-latest"  # alias de Google: siempre el Flash más reciente
DEFAULT_LITE_MODEL = "gemini-flash-lite-latest"  # clasificador de áreas: barato y rápido
ROLES = ("all", "webhook", "worker", "sender", "console")


@dataclass(frozen=True)
class Settings:
    # WhatsApp Cloud API
    wa_token: str
    wa_phone_number_id: str
    wa_app_secret: str
    wa_verify_token: str
    graph_version: str = "v23.0"

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = DEFAULT_MODEL
    gemini_fallback_model: str = DEFAULT_FALLBACK_MODEL
    thinking_budget: int | None = None  # solo modelos 2.5: 0 = sin "thinking"; None = valor por defecto
    classifier_model: str = DEFAULT_LITE_MODEL  # vacío = clasificador por palabras clave

    # Estado y colas
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "sqlite+aiosqlite:///./whatsappia.db"  # producción: postgresql+asyncpg://...
    queue_mode: str = "local"  # "local" (asyncio) | "pubsub"
    pubsub_topic: str = ""  # projects/<id>/topics/<topic>
    pubsub_push_token: str = ""  # secreto compartido en la URL de push

    # Comportamiento
    debounce_seconds: float = 4.0
    session_ttl_seconds: int = 7 * 24 * 3600
    max_history_contents: int = 30
    max_media_bytes: int = 10 * 1024 * 1024

    # Operación
    handoff_webhook_url: str = ""  # tu inbox humano (Chatwoot, Zendesk, n8n...)
    secret_key: str = "cambia-esto"  # firma las sesiones del panel
    owner_email: str = ""  # crea el primer owner si no hay usuarios
    owner_password: str = ""
    role: str = "all"  # all | webhook | worker | sender | console (ver README)
    pseudonym_pepper: str = "cambia-esto"

    @classmethod
    def from_env(cls) -> "Settings":
        tb = _env("THINKING_BUDGET", "")
        return cls(
            wa_token=_env("WA_TOKEN", required=True),
            wa_phone_number_id=_env("WA_PHONE_NUMBER_ID", required=True),
            wa_app_secret=_env("WA_APP_SECRET", required=True),
            wa_verify_token=_env("WA_VERIFY_TOKEN", required=True),
            graph_version=_env("GRAPH_VERSION", "v23.0"),
            gemini_api_key=_env("GEMINI_API_KEY"),
            gemini_model=_env("GEMINI_MODEL", DEFAULT_MODEL),
            gemini_fallback_model=_env("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL),
            thinking_budget=None if tb == "" else int(tb),
            classifier_model=_env("CLASSIFIER_MODEL", DEFAULT_LITE_MODEL),
            redis_url=_env("REDIS_URL", "redis://localhost:6379/0"),
            database_url=_env("DATABASE_URL", "sqlite+aiosqlite:///./whatsappia.db"),
            queue_mode=_env("QUEUE_MODE", "local"),
            pubsub_topic=_env("PUBSUB_TOPIC"),
            pubsub_push_token=_env("PUBSUB_PUSH_TOKEN"),
            debounce_seconds=float(_env("DEBOUNCE_SECONDS", "4.0")),
            session_ttl_seconds=int(_env("SESSION_TTL_SECONDS", str(7 * 24 * 3600))),
            max_history_contents=int(_env("MAX_HISTORY_CONTENTS", "30")),
            handoff_webhook_url=_env("HANDOFF_WEBHOOK_URL"),
            secret_key=_env("SECRET_KEY", required=True),
            owner_email=_env("OWNER_EMAIL"),
            owner_password=_env("OWNER_PASSWORD"),
            role=_env("ROLE", "all"),
            pseudonym_pepper=_env("PSEUDONYM_PEPPER", required=True),
        )
