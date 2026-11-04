import asyncio

from fastapi.testclient import TestClient

from app.control import ControlCenter
from app.demo import DEMO_PASSWORD, build_demo, check_gemini, seed

MARIA = "593999000001"


def run_demo(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("DEBOUNCE_SECONDS", "0.01")
    return build_demo()


def test_demo_pipeline_with_rule_based_model(monkeypatch):
    app, store, _, _ = run_demo(monkeypatch)
    with TestClient(app) as c:
        c.portal.call(seed, app.state.repo, store)
        info = c.get("/demo-info").json()
        assert info["password"] == DEMO_PASSWORD and len(info["users"]) == 3
        token = c.post("/api/auth/login", json={"email": "asesor@demo.ec", "password": DEMO_PASSWORD}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        assert "reglas" in c.get("/api/admin/demo", headers=h).json()["ai"]

        def say(**body):
            assert c.post("/api/admin/demo/incoming", headers=h, json=body).status_code == 200
            c.portal.call(app.state.queue.drain)

        say(text="cuánto debo", wa_id=MARIA)
        say(text="Contrato C-1001", wa_id=MARIA)
        today = ControlCenter().now().date().isoformat()
        say(wa_id=MARIA, receipt={"es_comprobante": True, "banco": "Banco Pichincha", "numero": "123", "monto": 50,
                                  "fecha": today, "cuenta_destino": "2200123456", "confianza": 0.9})
        say(text="C-1001", wa_id=MARIA)
        texts = [m["text"] for m in c.get(f"/api/admin/conversations/{MARIA}", headers=h).json()["messages"]
                 if m["role"] == "bot"]
        assert "Total: *$50.00*" in texts[1] and "Registré tu pago de *$50.00*" in texts[2]
        assert "al día" in texts[3]


def test_check_gemini_reports_unavailable_models():
    from app.agent import Agent

    class Models:
        async def get(self, *, model):
            if model == "bad":
                raise RuntimeError("404 model not found")

    class Client:
        def __init__(self):
            self.models = Models()
            self.aio = self

    assert asyncio.run(check_gemini(Agent(Client(), "good", "bad"))).startswith("modelo de respaldo")
    assert asyncio.run(check_gemini(Agent(Client(), "bad", "bad"))).startswith("ningún modelo")
    assert asyncio.run(check_gemini(Agent(Client(), "good", "good"))) is None
