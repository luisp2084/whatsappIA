import os

from fastapi.testclient import TestClient

from app.demo import build_demo

AUTH = {"Authorization": "Bearer demo"}


def test_demo_runs_full_pipeline_with_rule_based_model(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("DEBOUNCE_SECONDS", "0.01")
    app, _, _ = build_demo()
    with TestClient(app) as c:
        assert c.get("/demo-info").json()["token_hint"] == "demo"
        assert c.get("/", follow_redirects=False).headers["location"] == "/admin/"
        assert c.get("/api/admin/demo").status_code == 401
        assert "reglas" in c.get("/api/admin/demo", headers=AUTH).json()["ai"]

        wa = c.post("/api/admin/demo/incoming", headers=AUTH,
                    json={"text": "¿dónde está mi pedido TX-123456?"}).json()["wa_id"]
        c.portal.call(app.state.queue.drain)
        msgs = c.get(f"/api/admin/conversations/{wa}", headers=AUTH).json()["messages"]
        assert msgs[0]["role"] == "user"
        assert "en reparto" in msgs[-1]["text"] and "asistente virtual" in msgs[-1]["text"]

        c.post("/api/admin/demo/incoming", headers=AUTH, json={"text": "quiero hablar con una persona"})
        c.portal.call(app.state.queue.drain)
        meta = c.get(f"/api/admin/conversations/{wa}", headers=AUTH).json()["meta"]
        assert meta["mode"] == "human"


def test_check_gemini_reports_unavailable_models():
    import asyncio

    from app.agent import Agent
    from app.demo import check_gemini

    class Models:
        async def get(self, *, model):
            if model == "bad":
                raise RuntimeError("404 model not found")
            return object()

    class Client:
        def __init__(self):
            self.models = Models()
            self.aio = self

    assert asyncio.run(check_gemini(Agent(Client(), "good", "bad"))).startswith("modelo de respaldo")
    assert asyncio.run(check_gemini(Agent(Client(), "bad", "bad"))).startswith("ningún modelo")
    assert asyncio.run(check_gemini(Agent(Client(), "good", "good"))) is None
