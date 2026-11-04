"""Usuarios del panel, roles y sesiones.

Roles y permisos (de menos a más):
- asesor: ver y atender conversaciones, escribir, enviar plantillas, marcar respuestas como malas.
- admin: lo anterior + Revisión y reglas, Centro de control, interruptor del bot, métricas y auditoría.
- owner: lo anterior + gestionar usuarios.

Las contraseñas se guardan con scrypt. La sesión es un token firmado con HMAC (SECRET_KEY) que
caduca en 12 h. En producción, pon además el panel detrás de Cloud IAP o similar.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

ROLES = ("asesor", "admin", "owner")
PERMISSIONS: dict[str, set[str]] = {
    "asesor": {"conversations.view", "conversations.act", "reviews.flag", "metrics.view"},
    "admin": {"conversations.view", "conversations.act", "reviews.flag", "metrics.view", "reviews.manage",
              "control.manage", "audit.view"},
    "owner": {"conversations.view", "conversations.act", "reviews.flag", "metrics.view", "reviews.manage",
              "control.manage", "audit.view", "users.manage"},
}
TOKEN_TTL = 12 * 3600


def can(role: str, permission: str) -> bool:
    return permission in PERMISSIONS.get(role, set())


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=2**14, r=8, p=1,
                                dklen=len(expected))
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


def issue_token(user_id: int, secret: str, ttl: int = TOKEN_TTL) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"uid": user_id, "exp": int(time.time()) + ttl}).encode()).decode()
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(token: str, secret: str) -> int | None:
    try:
        body, sig = token.split(".")
    except ValueError:
        return None
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data: dict[str, Any] = json.loads(base64.urlsafe_b64decode(body.encode()))
    except ValueError:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return int(data["uid"])


def validate_password(password: str) -> str | None:
    if len(password) < 10:
        return "La contraseña debe tener al menos 10 caracteres."
    return None
