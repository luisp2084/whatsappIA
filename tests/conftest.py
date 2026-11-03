from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import fakeredis
import pytest
from google.genai import types

from app.agent import Agent
from app.config import Settings
from app.session import SessionStore
from app.worker import Worker

APP_SECRET = "test-secret"


def make_settings(**kw: Any) -> Settings:
    base = dict(wa_token="t", wa_phone_number_id="123", wa_app_secret=APP_SECRET,
                wa_verify_token="verify-me", debounce_seconds=0.05, pseudonym_pepper="pepper",
                admin_token="admin")
    base.update(kw)
    return Settings(**base)


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def text_payload(wa_id: str, msg_id: str, text: str) -> dict[str, Any]:
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": wa_id, "profile": {"name": "Ana"}}],
        "messages": [{"from": wa_id, "id": msg_id, "timestamp": "1762160000", "type": "text",
                      "text": {"body": text}}]}}]}]}


class FakeWA:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.read: list[str] = []

    async def send_text(self, to: str, text: str) -> None:
        self.sent.append({"to": to, "type": "text", "text": text})

    async def send_buttons(self, to: str, body: str, options: list[dict[str, str]]) -> None:
        self.sent.append({"to": to, "type": "buttons", "text": body, "options": options})

    async def mark_read(self, message_id: str, typing: bool = True) -> None:
        self.read.append(message_id)

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        return b"\xff\xd8fakejpeg", "image/jpeg"


class FakeModels:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: list[types.Content], config: Any):
        self.calls.append({"model": model, "contents": list(contents)})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script: list[Any]) -> None:
        self.models = FakeModels(script)
        self.aio = self


def model_text(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))])


def model_call(name: str, **args: Any) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(
            function_call=types.FunctionCall(name=name, args=args), thought_signature=b"sig")]))])


class FakeHandoff:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def notify(self, event: dict[str, Any]) -> None:
        self.events.append(event)


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def store(redis):
    return SessionStore(redis, ttl_seconds=3600)


@pytest.fixture
def wa():
    return FakeWA()


@pytest.fixture
def handoff():
    return FakeHandoff()


def make_worker(store, wa, handoff, script, fallback: str | None = None):
    client = FakeClient(script)
    agent = Agent(client, "primary-model", fallback)  # type: ignore[arg-type]
    worker = Worker(store, wa, agent, handoff, debounce_seconds=0.05, max_history=30,
                    max_media_bytes=1_000_000, pepper="pepper")
    return worker, client


def dumps(obj: Any) -> bytes:
    return json.dumps(obj).encode()
