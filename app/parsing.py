"""Convierte el payload del webhook de WhatsApp en objetos simples."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class IncomingMessage:
    wa_id: str
    message_id: str
    type: str  # text | interactive | image | audio | document | other
    timestamp: int = 0
    text: str = ""
    button_id: str = ""
    media_id: str = ""
    mime_type: str = ""
    profile_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IncomingMessage":
        return cls(**data)


@dataclass
class StatusEvent:
    wa_id: str
    message_id: str
    status: str  # sent | delivered | read | failed
    errors: list[dict[str, Any]]


def parse_webhook(body: dict[str, Any]) -> tuple[list[IncomingMessage], list[StatusEvent]]:
    messages: list[IncomingMessage] = []
    statuses: list[StatusEvent] = []
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                messages.append(_parse_message(m, names))
            for s in value.get("statuses", []):
                statuses.append(
                    StatusEvent(
                        wa_id=s.get("recipient_id", ""),
                        message_id=s.get("id", ""),
                        status=s.get("status", ""),
                        errors=s.get("errors", []),
                    )
                )
    return messages, statuses


def _parse_message(m: dict[str, Any], names: dict[str, str]) -> IncomingMessage:
    wa_id = m.get("from", "")
    msg = IncomingMessage(
        wa_id=wa_id,
        message_id=m.get("id", ""),
        type=m.get("type", "other"),
        timestamp=int(m.get("timestamp", 0) or 0),
        profile_name=names.get(wa_id, ""),
    )
    t = msg.type
    if t == "text":
        msg.text = m.get("text", {}).get("body", "")
    elif t == "interactive":
        inter = m.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        msg.button_id = reply.get("id", "")
        msg.text = reply.get("title", "")
    elif t == "button":  # respuesta a un botón de plantilla
        msg.type = "interactive"
        msg.button_id = m.get("button", {}).get("payload", "")
        msg.text = m.get("button", {}).get("text", "")
    elif t in ("image", "audio", "document", "video", "sticker"):
        media = m.get(t, {})
        msg.media_id = media.get("id", "")
        msg.mime_type = media.get("mime_type", "")
        msg.text = media.get("caption", "")
    else:
        msg.type = "other"
    return msg
