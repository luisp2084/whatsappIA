import json

from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import FakeHandoff, FakeWA, make_settings, sign, text_payload


class CaptureQueue:
    def __init__(self):
        self.items = []

    async def publish(self, msg):
        self.items.append(msg)


def client_with(store, queue):
    app = create_app(make_settings(), store=store, wa=FakeWA(), agent=object(),
                     handoff=FakeHandoff(), queue=queue)
    return TestClient(app)


def test_verify_handshake(store):
    with client_with(store, CaptureQueue()) as c:
        ok = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                       "hub.challenge": "42"})
        assert ok.status_code == 200 and ok.text == "42"
        assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x"}).status_code == 403


def test_webhook_rejects_bad_signature_and_enqueues_valid(store):
    queue = CaptureQueue()
    body = json.dumps(text_payload("346", "m1", "hola")).encode()
    with client_with(store, queue) as c:
        bad = c.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=00"})
        assert bad.status_code == 401
        good = c.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body),
                                                        "Content-Type": "application/json"})
        assert good.status_code == 200
    assert [m.message_id for m in queue.items] == ["m1"]


def test_release_requires_admin(store):
    with client_with(store, CaptureQueue()) as c:
        assert c.post("/handoff/release", json={"wa_id": "346"}).status_code == 401
        r = c.post("/handoff/release", json={"wa_id": "346"}, headers={"Authorization": "Bearer admin"})
        assert r.json() == {"status": "bot"}
