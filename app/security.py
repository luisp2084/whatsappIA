"""Firma de webhooks, seudonimización y redacción de PII para logs."""

from __future__ import annotations

import hashlib
import hmac
import re


def verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Valida X-Hub-Signature-256 (HMAC-SHA256 del cuerpo crudo con el App Secret)."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def pseudonymize(wa_id: str, pepper: str) -> str:
    """ID estable y no reversible sin el pepper, para logs y analítica."""
    return hmac.new(pepper.encode(), wa_id.encode(), hashlib.sha256).hexdigest()[:16]


_PII_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<EMAIL>"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b"), "<IBAN>"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "<CARD>"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "<PHONE>"),
]


def redact_pii(text: str) -> str:
    """Redacción básica por regex. En producción complementa con Cloud DLP o Presidio."""
    for pattern, token in _PII_PATTERNS:
        text = pattern.sub(token, text)
    return text
