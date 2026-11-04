import json

import pytest
from fastapi.testclient import TestClient

from app.agent import Agent
from app.areas import KeywordClassifier
from app.main import create_app
from app.parsing import IncomingMessage
from app.revision import RuleSuggester

from .conftest import (PASSWORD, FakeChannel, FakeClient, FakeHandoff, make_settings, make_user, model_call,
                       model_text, sign, text_payload)

MARIA = "593999000001"


class CaptureQueue:
    def __init__(self):
        self.items = []

    async def publish(self, msg):
        self.items.append(msg)


@pytest.fixture
def make_client(store, db, isp):
    def build(script=(), queue=None, **settings):
        channel = FakeChannel()
        agent = Agent(FakeClient(list(script)), "m")  # type: ignore[arg-type]
        app = create_app(make_settings(**settings), store=store, db=db, channel=channel, agent=agent,
                         classifier=KeywordClassifier(), isp=isp, handoff=FakeHandoff(), suggester=RuleSuggester(),
                         run_background=False, **({"queue": queue} if queue else {}))
        client = TestClient(app)
        client.channel = channel
        return client
    return build


def login(c, email, role):
    c.portal.call(make_user, c.app.state.repo, email, role)
    token = c.post("/api/auth/login", json={"email": email, "password": PASSWORD}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def deliver(c, mid, text, wa=MARIA):
    c.portal.call(c.app.state.worker.handle_incoming,
                  IncomingMessage(wa_id=wa, message_id=mid, type="text", text=text, profile_name="María"))


def test_webhook_verify_signature_and_enqueue(make_client):
    q = CaptureQueue()
    with make_client(queue=q) as c:
        ok = c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
        assert ok.text == "42"
        body = json.dumps(text_payload("346", "m1", "hola")).encode()
        assert c.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=00"}).status_code == 401
        assert c.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)}).status_code == 200
    assert [m.message_id for m in q.items] == ["m1"]


def test_login_errors_and_rate_limit(make_client):
    with make_client() as c:
        c.portal.call(make_user, c.app.state.repo, "a@isp.ec", "asesor")
        assert c.get("/api/admin/conversations").status_code == 401
        for _ in range(5):
            assert c.post("/api/auth/login", json={"email": "a@isp.ec", "password": "mala"}).status_code == 401
        assert c.post("/api/auth/login", json={"email": "a@isp.ec", "password": PASSWORD}).status_code == 429


def test_role_permissions(make_client):
    with make_client() as c:
        asesor = login(c, "asesor@isp.ec", "asesor")
        admin = login(c, "admin@isp.ec", "admin")
        owner = login(c, "owner@isp.ec", "owner")
        assert c.get("/api/admin/conversations", headers=asesor).status_code == 200
        assert c.post("/api/admin/control/bot", headers=asesor, json={"enabled": False}).status_code == 403
        assert c.get("/api/admin/reviews", headers=asesor).status_code == 403
        assert c.post("/api/admin/control/bot", headers=admin, json={"enabled": False}).json() == {"bot_enabled": False}
        assert c.get("/api/admin/users", headers=admin).status_code == 403
        assert len(c.get("/api/admin/users", headers=owner).json()["items"]) == 3
        assert any(a["action"] == "control.bot_switch" for a in c.get("/api/admin/audit", headers=admin).json()["items"])


def test_tabs_take_reply_close(make_client):
    script = [model_call("escalate_to_human", summary="Quiere hablar de su factura", priority="media")]
    with make_client(script) as c:
        h = login(c, "marta@isp.ec", "asesor")
        deliver(c, "m1", "necesito revisar una factura rara")
        tabs = c.get("/api/admin/conversations?tab=pendientes", headers=h).json()
        assert [i["wa_id"] for i in tabs["items"]] == [MARIA] and tabs["counts"]["pendientes"] == 1
        assert c.post(f"/api/admin/conversations/{MARIA}/take", headers=h).json() == {"state": "asignada"}
        mine = c.get("/api/admin/conversations?tab=mias", headers=h).json()
        assert mine["items"][0]["assignee"] == "Marta"
        assert c.post(f"/api/admin/conversations/{MARIA}/messages", headers=h, json={"text": "Hola, te ayudo"}).status_code == 200
        assert c.channel.sent[-1]["text"] == "Hola, te ayudo"
        assert c.post(f"/api/admin/conversations/{MARIA}/close", headers=h).json() == {"state": "cerrada"}
        assert c.get("/api/admin/conversations?tab=cerradas", headers=h).json()["counts"]["cerradas"] == 1
        msgs = c.get(f"/api/admin/conversations/{MARIA}", headers=h).json()["messages"]
        agent_msg = next(m for m in msgs if m["role"] == "agent")
        assert agent_msg["author"] == "Marta"
        assert c.post(f"/api/admin/conversations/{MARIA}/release", headers=h).status_code == 400  # cerrada


def test_window_closed_blocks_free_text(make_client):
    with make_client() as c:
        h = login(c, "marta@isp.ec", "asesor")
        c.portal.call(lambda: c.app.state.repo.record(MARIA, "user", "hola", ts=1.0))
        r = c.post(f"/api/admin/conversations/{MARIA}/messages", headers=h, json={"text": "¿sigues ahí?"})
        assert r.status_code == 409
        r = c.post(f"/api/admin/conversations/{MARIA}/template", headers=h,
                   json={"name": "retomar_conversacion", "params": ["María"]})
        assert r.status_code == 200 and c.channel.sent[-1]["type"] == "template"


def test_flag_review_rule_reaches_prompt(make_client):
    script = [model_text("Un cobrador pasará por tu domicilio."), model_text("Entendido, no hay deuda pendiente.")]
    with make_client(script) as c:
        asesor = login(c, "marta@isp.ec", "asesor")
        admin = login(c, "admin@isp.ec", "admin")
        deliver(c, "m1", "ya el dinero lo vinieron a retirar, cuánto debo")
        bot_msg = next(m for m in c.get(f"/api/admin/conversations/{MARIA}", headers=asesor).json()["messages"]
                       if m["role"] == "bot")
        user_msg_id = bot_msg["id"] - 1
        assert c.post(f"/api/admin/messages/{user_msg_id}/flag", headers=asesor, json={}).status_code == 400
        r = c.post(f"/api/admin/messages/{bot_msg['id']}/flag", headers=asesor,
                   json={"reason": "alucino", "note": "no existen cobradores a domicilio"})
        review_id = r.json()["review_id"]
        items = c.get("/api/admin/reviews", headers=admin).json()["items"]
        assert items[0]["customer_text"].startswith("ya el dinero") and items[0]["area"] == "cobros"
        suggestion = c.post(f"/api/admin/reviews/{review_id}/suggest", headers=admin).json()
        assert "cobradores" in suggestion["text"]
        rule_id = c.post(f"/api/admin/reviews/{review_id}/rule", headers=admin,
                         json={"area": "cobros", "text": "No menciones cobradores a domicilio: no existen."}).json()["rule_id"]
        assert c.get("/api/admin/reviews", headers=admin).json()["items"] == []  # ya resuelto
        assert c.patch(f"/api/admin/rules/{rule_id}", headers=admin, json={"active": True}).status_code == 200
        deliver(c, "m2", "y cuánto debo entonces")
        prompt = c.app.state.worker.agent.client.models.calls[-1]["config"].system_instruction
        assert "1. No menciones cobradores a domicilio" in prompt


def test_control_center_validation_and_audit(make_client):
    with make_client() as c:
        admin = login(c, "admin@isp.ec", "admin")
        cc = c.get("/api/admin/control", headers=admin).json()["control"]
        cc["months_collectable"] = 2
        assert c.put("/api/admin/control", headers=admin, json=cc).status_code == 200
        cc["hours"]["lunes"] = {"open": "25:00", "close": "18:00"}
        r = c.put("/api/admin/control", headers=admin, json=cc)
        assert r.status_code == 400 and "hours" in r.json()["detail"]
        audit = c.get("/api/admin/audit", headers=admin).json()["items"]
        assert audit[0]["action"] == "control.updated" and audit[0]["detail"]["changed"] == ["months_collectable"]


def test_users_management_keeps_an_owner(make_client):
    with make_client() as c:
        owner = login(c, "owner@isp.ec", "owner")
        r = c.post("/api/admin/users", headers=owner, json={"email": "nuevo@isp.ec", "name": "Nuevo", "role": "asesor",
                                                             "password": "corta"})
        assert r.status_code == 400
        new = c.post("/api/admin/users", headers=owner, json={"email": "nuevo@isp.ec", "name": "Nuevo",
                                                               "role": "asesor", "password": "una-clave-larga"}).json()
        assert new["role"] == "asesor"
        me = c.get("/api/auth/me", headers=owner).json()
        assert c.patch(f"/api/admin/users/{me['id']}", headers=owner, json={"role": "admin"}).status_code == 400
        assert c.patch(f"/api/admin/users/{new['id']}", headers=owner, json={"active": False}).json()["active"] is False
        assert c.post("/api/auth/login", json={"email": "nuevo@isp.ec", "password": "una-clave-larga"}).status_code == 401


def test_owner_bootstrap_from_env(make_client):
    with make_client(owner_email="jefe@isp.ec", owner_password="clave-inicial-segura") as c:
        r = c.post("/api/auth/login", json={"email": "jefe@isp.ec", "password": "clave-inicial-segura"})
        assert r.json()["user"]["role"] == "owner"


def test_metrics_include_payments_and_static_panel(make_client):
    with make_client() as c:
        admin = login(c, "admin@isp.ec", "admin")
        m = c.get("/api/admin/metrics?days=7", headers=admin).json()
        assert m["payments"] == {"count": 0, "usd": 0.0} and len(m["days"]) == 7
        assert "Panel del agente" in c.get("/admin/").text


def test_roles_mount_only_their_routes(make_client):
    with make_client(role="webhook") as c:
        assert c.get("/healthz").json()["role"] == "webhook"
        assert c.get("/api/auth/me").status_code == 404
        assert c.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                         "hub.challenge": "1"}).status_code == 200
