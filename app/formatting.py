"""Adapta el Markdown que suele generar el LLM al formato de WhatsApp."""

from __future__ import annotations

import re

MAX_TEXT = 4096  # límite de la Cloud API para mensajes de texto


def to_whatsapp(text: str) -> str:
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+)$", r"*\1*", text, flags=re.M)  # títulos -> negrita
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)  # **negrita** -> *negrita*
    text = re.sub(r"__(.+?)__", r"_\1_", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1: \2", text)  # enlaces
    text = re.sub(r"^\s*[*+]\s+", "- ", text, flags=re.M)  # viñetas
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def split_message(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Trocea respetando párrafos para no superar el límite de la API."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(para) > limit:
            chunks.append(para[:limit])
            para = para[limit:]
        current = para
    if current:
        chunks.append(current)
    return chunks
