"""Cliente mínimo de la WhatsApp Cloud API (Graph API) con reintentos."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from .formatting import split_message, to_whatsapp

log = logging.getLogger(__name__)

RETRYABLE = {429, 500, 502, 503, 504}


class WhatsAppError(Exception):
    pass


class WhatsAppClient:
    def __init__(self, token: str, phone_number_id: str, graph_version: str = "v23.0",
                 http: httpx.AsyncClient | None = None, max_retries: int = 4):
        self._base = f"https://graph.facebook.com/{graph_version}"
        self._phone_id = phone_number_id
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._max_retries = max_retries

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- envío ---------------------------------------------------------------

    async def send_text(self, to: str, text: str) -> None:
        for chunk in split_message(to_whatsapp(text)):
            await self._post_message({"to": to, "type": "text",
                                      "text": {"body": chunk, "preview_url": False}})

    async def send_buttons(self, to: str, body: str, options: list[dict[str, str]]) -> None:
        """Hasta 3 botones de respuesta rápida; títulos de máximo 20 caracteres."""
        buttons = [
            {"type": "reply", "reply": {"id": o["id"][:256], "title": o["title"][:20]}}
            for o in options[:3]
        ]
        await self._post_message({
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": to_whatsapp(body)[:1024]},
                "action": {"buttons": buttons},
            },
        })

    async def mark_read(self, message_id: str, typing: bool = True) -> None:
        """Marca como leído y muestra 'escribiendo…' (dura hasta la respuesta o ~25 s)."""
        payload: dict[str, Any] = {"messaging_product": "whatsapp", "status": "read",
                                   "message_id": message_id}
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        try:
            await self._request("POST", f"{self._base}/{self._phone_id}/messages", json=payload)
        except WhatsAppError:
            log.warning("mark_read falló; se continúa")  # no es crítico

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        meta = await self._request("GET", f"{self._base}/{media_id}")
        info = meta.json()
        if int(info.get("file_size", 0) or 0) > max_bytes:
            raise WhatsAppError("media demasiado grande")
        resp = await self._request("GET", info["url"])
        if len(resp.content) > max_bytes:
            raise WhatsAppError("media demasiado grande")
        return resp.content, info.get("mime_type", "application/octet-stream")

    # --- interno -------------------------------------------------------------

    async def _post_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        resp = await self._request("POST", f"{self._base}/{self._phone_id}/messages", json=payload)
        return resp.json()

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.request(method, url, headers=self._headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise WhatsAppError(f"error de red: {exc}") from exc
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in RETRYABLE or attempt == self._max_retries:
                    raise WhatsAppError(f"Graph API {resp.status_code}: {resp.text[:500]}")
            await asyncio.sleep(min(2 ** attempt, 16) * (0.5 + random.random() / 2))
        raise WhatsAppError("reintentos agotados")  # inalcanzable
