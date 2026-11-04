from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date
from typing import Any

import fakeredis
import pytest
import pytest_asyncio
from google.genai import types

from app import auth
from app.agent import Agent
from app.areas import KeywordClassifier
from app.config import Settings
from app.db import Database
from app.isp import ISP, FakeNetworkControl, FakeOLT, FakeReceiptReader
from app.repo import Repo
from app.session import SessionStore
from app.worker import Worker

APP_SECRET = "test-secret"
SECRET_KEY = "test-secret-key"
PASSWORD = "contraseña-segura"
MARIA = "593999000001"  # 2 contratos (C-1001 debe 2 meses de 25 USD, C-1005 debe 1 de 45)
JORGE = "593987654321"  # C-1002 suspendido, incidente en su zona
LUIS = "593998887766"  # C-1003 al día, potencia baja


def make_settings(**kw: Any) -> Settings:
    base = dict(wa_token="t", wa_phone_number_id="123", wa_app_secret=APP_SECRET, wa_verify_token="verify-me",
                debounce_seconds=0.01, pseudonym_pepper="pepper", secret_key=SECRET_KEY,
                database_url="sqlite+aiosqlite:///unused.db")
    base.update(kw)
    return Settings(**base)


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def text_payload(wa_id: str, msg_id: str, text: str) -> dict[str, Any]:
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"field": "messages", "value": {
        "contacts": [{"wa_id": wa_id, "profile": {"name": "Ana"}}],
        "messages": [{"from": wa_id, "id": msg_id, "timestamp": "1762160000", "type": "text",
                      "text": {"body": text}}]}}]}]}


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.read: list[str] = []
        self.media: dict[str, tuple[bytes, str]] = {}
        self.fail_next = 0

    async def _ok(self) -> None:
        if self.fail_next:
            self.fail_next -= 1
            from app.whatsapp import WhatsAppError
            raise WhatsAppError("falla simulada")

    async def send_text(self, to: str, text: str) -> None:
        await self._ok()
        self.sent.append({"to": to, "type": "text", "text": text})

    async def send_buttons(self, to: str, body: str, options: list[dict[str, str]]) -> None:
        await self._ok()
        self.sent.append({"to": to, "type": "buttons", "text": body, "options": options})

    async def send_template(self, to: str, name: str, language: str, params: list[str]) -> None:
        await self._ok()
        self.sent.append({"to": to, "type": "template", "name": name, "params": params})

    async def mark_read(self, message_id: str, typing: bool = True) -> None:
        self.read.append(message_id)

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        return self.media[media_id]


class FakeModels:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: list[types.Content], config: Any):
        self.calls.append({"model": model, "contents": list(contents), "config": config})
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


def receipt(**kw: Any) -> bytes:
    from app.control import ControlCenter

    data = {"es_comprobante": True, "banco": "Banco Pichincha", "numero": "889911", "monto": 50.0,
            "fecha": ControlCenter().now().date().isoformat(), "cuenta_destino": "2200123456", "confianza": 0.95}
    data.update(kw)
    return json.dumps(data).encode()


@pytest.fixture
def store():
    return SessionStore(fakeredis.aioredis.FakeRedis(decode_responses=True), ttl_seconds=3600)


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
def repo(db):
    return Repo(db)


@pytest.fixture
def isp():
    nc = FakeNetworkControl(today=date.today())
    return ISP(nc=nc, olt=FakeOLT(nc), receipts=FakeReceiptReader())


@pytest.fixture
def channel():
    return FakeChannel()


@pytest.fixture
def handoff():
    return FakeHandoff()


def make_worker(store, repo, channel, isp, handoff, script, fallback: str | None = None):
    client = FakeClient(script)
    agent = Agent(client, "primary-model", fallback)  # type: ignore[arg-type]
    worker = Worker(store, repo, channel, agent, isp, handoff, classifier=KeywordClassifier(),
                    debounce_seconds=0.01, max_history=30, max_media_bytes=1_000_000, pepper="pepper")
    return worker, client


async def make_user(repo: Repo, email: str, role: str):
    return await repo.create_user(email, email.split("@")[0].title(), role, auth.hash_password(PASSWORD))
