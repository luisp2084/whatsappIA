import time

from fastapi.testclient import TestClient

from app.agent import Agent
from app.main import create_app
from app.parsing import IncomingMessage

from .conftest import FakeClient, FakeHandoff, FakeWA, make_settings, model_call, model_text

AUTH = {"Authorization": "Bearer admin"}
WA = "34600000000"


def make_client(store, script):
    wa = FakeWA()
    agent = Agent(FakeClient(script), "m")  # type: ignore[arg-type]
    app = create_app(make_settings(), store=store, wa=wa, agent=agent, handoff=FakeHandoff(),
                     queue=object())
    return TestClient(app), wa


def deliver(client, mid, text):
    worker = client.app.state.worker
    client.portal.call(worker.handle_incoming,
                       IncomingMessage(wa_id=WA, message_id=mid, type="text", text=text, profile_name="Ana"))


def test_requires_token(store):
    c, _ = make_client(store, [])
    with c:
        assert c.get("/api/admin/conversations").status_code == 401
        assert c.get("/api/admin/conversations", headers={"Authorization": "Bearer x"}).status_code == 401


def test_conversation_flow_takeover_reply_release(store):
    c, wa = make_client(store, [model_text("¡Hola Ana! ¿En qué te ayudo?")])
    with c:
        deliver(c, "m1", "hola")

        convs = c.get("/api/admin/conversations", headers=AUTH).json()
        assert convs[0]["wa_id"] == WA and convs[0]["name"] == "Ana" and convs[0]["mode"] == "bot"
        assert convs[0]["window_open"] is True

        detail = c.get(f"/api/admin/conversations/{WA}", headers=AUTH).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "bot"]

        r = c.post(f"/api/admin/conversations/{WA}/messages", headers=AUTH,
                   json={"text": "Soy Marta, te ayudo yo", "agent_name": "Marta"})
        assert r.status_code == 200
        assert wa.sent[-1]["text"] == "Soy Marta, te ayudo yo"
        meta = c.get(f"/api/admin/conversations/{WA}", headers=AUTH).json()["meta"]
        assert meta["mode"] == "human"  # escribir como agente toma el control

        # Con modo humano el bot no responde (el script del modelo está vacío: fallaría si se llamara).
        deliver(c, "m2", "gracias")
        assert len(wa.sent) == 2

        assert c.post(f"/api/admin/conversations/{WA}/release", headers=AUTH).json() == {"mode": "bot"}
        roles = [m["role"] for m in c.get(f"/api/admin/conversations/{WA}", headers=AUTH).json()["messages"]]
        assert roles == ["user", "bot", "agent", "user", "system"]

        m = c.get("/api/admin/metrics?days=7", headers=AUTH).json()["days"][-1]
        assert m["turns"] == 1 and m["agent_messages"] == 1


def test_reply_blocked_when_window_closed(store):
    c, _ = make_client(store, [model_text("hola")])
    with c:
        deliver(c, "m1", "hola")
        c.portal.call(store.r.hset, f"meta:{WA}", "last_user_ts", time.time() - 25 * 3600)
        r = c.post(f"/api/admin/conversations/{WA}/messages", headers=AUTH, json={"text": "¿sigues ahí?"})
        assert r.status_code == 409


def test_escalation_counts_and_static_panel(store):
    c, _ = make_client(store, [model_call("escalate_to_human", summary="queja", priority="alta")])
    with c:
        deliver(c, "m1", "quiero quejarme")
        assert c.get("/api/admin/metrics", headers=AUTH).json()["days"][-1]["handoffs"] == 1
        page = c.get("/admin/")
        assert page.status_code == 200 and "Panel del agente" in page.text
        assert c.get("/admin/app.js").status_code == 200


def test_delete_removes_from_panel(store):
    c, _ = make_client(store, [model_text("hola")])
    with c:
        deliver(c, "m1", "hola")
        deliver(c, "m2", "borrar mis datos")
        assert c.get("/api/admin/conversations", headers=AUTH).json() == []
