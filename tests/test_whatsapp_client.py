import json

import httpx
import pytest

from app import whatsapp
from app.whatsapp import WhatsAppClient, WhatsAppError

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def _sleep(_):
        return None
    monkeypatch.setattr(whatsapp.asyncio, "sleep", _sleep)


def client_for(handler):
    return WhatsAppClient("TOKEN", "PHONE", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_send_buttons_payload_and_retry_on_429():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    wa = client_for(handler)
    await wa.send_buttons("346", "**Elige**", [{"id": "a", "title": "Una opción con título largo"},
                                               {"id": "b", "title": "B"}])
    body = json.loads(calls[-1].content)
    assert len(calls) == 2
    assert calls[-1].url.path == "/v23.0/PHONE/messages"
    assert calls[-1].headers["Authorization"] == "Bearer TOKEN"
    assert body["interactive"]["body"]["text"] == "*Elige*"
    assert body["interactive"]["action"]["buttons"][0]["reply"]["title"] == "Una opción con títul"


async def test_client_error_is_not_retried():
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(400, json={"error": {"code": 131047}})

    with pytest.raises(WhatsAppError):
        await client_for(handler).send_text("346", "hola")
    assert len(calls) == 1


async def test_download_media_two_steps():
    def handler(req: httpx.Request):
        if req.url.path.endswith("/MID"):
            return httpx.Response(200, json={"url": "https://lookaside.fbsbx.com/f", "mime_type": "audio/ogg",
                                             "file_size": 3})
        return httpx.Response(200, content=b"ogg")

    data, mime = await client_for(handler).download_media("MID", 1000)
    assert data == b"ogg" and mime == "audio/ogg"
