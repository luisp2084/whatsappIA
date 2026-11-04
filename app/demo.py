"""Modo demo: el sistema completo sin WhatsApp, Redis, PostgreSQL ni sistemas del ISP reales.

    python -m app.demo

- Panel en http://localhost:8080/admin/ con usuarios de ejemplo (la pantalla de acceso los muestra).
- Desde la barra "Modo demo" escribes como si fueras un cliente o envías un comprobante; el mensaje
  recorre el mismo código que en producción (cola, debounce, áreas, herramientas, outbox, estados).
- IA: con GEMINI_API_KEY usa Gemini de verdad; sin clave, un modelo de reglas que imita los flujos.
- Network Control y la OLT son los simulados de `app/isp.py` (clientes, deudas y equipos de ejemplo).
- Todo vive en memoria y se pierde al parar.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import fakeredis
import uvicorn
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from google.genai import types
from pydantic import BaseModel, Field

from . import auth
from .agent import Agent, describe_error
from .areas import KeywordClassifier, LLMClassifier
from .config import DEFAULT_FALLBACK_MODEL, DEFAULT_LITE_MODEL, DEFAULT_MODEL, Settings
from .db import Database
from .isp import ISP, FakeNetworkControl, FakeOLT, FakeReceiptReader, GeminiReceiptReader
from .main import create_app
from .parsing import IncomingMessage
from .repo import Repo
from .revision import RuleSuggester
from .session import SessionStore
from .whatsapp import WhatsAppError

log = logging.getLogger("demo")

DEMO_CUSTOMERS = [
    {"wa_id": "593999000001", "name": "María Fernanda Cedeño", "hint": "2 contratos, debe 2 meses en C-1001"},
    {"wa_id": "593987654321", "name": "Jorge Andrade", "hint": "servicio suspendido, incidente en su zona"},
    {"wa_id": "593998887766", "name": "Luis Pacheco", "hint": "al día, potencia óptica baja"},
    {"wa_id": "593990000099", "name": "Cliente nuevo", "hint": "no es cliente: ventas"},
]
DEMO_USERS = [("owner@demo.ec", "Owner Demo", "owner"), ("admin@demo.ec", "Admin Demo", "admin"),
              ("asesor@demo.ec", "Marta (asesora)", "asesor")]
DEMO_PASSWORD = "demo123456"


# --- WhatsApp simulado ----------------------------------------------------------------------------


class DemoWhatsApp:
    """No envía nada: el panel muestra cada mensaje desde la base de datos. Guarda los adjuntos
    que se suben desde la barra de demo para devolverlos en `download_media`."""

    def __init__(self) -> None:
        self.media: dict[str, tuple[bytes, str]] = {}
        self.fail_next = 0  # para probar reintentos del outbox

    async def _send(self, to: str, what: str) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise WhatsAppError("fallo simulado de WhatsApp")
        log.info("[WhatsApp simulado] a %s: %s", to, what[:100])

    async def send_text(self, to: str, text: str) -> None:
        await self._send(to, text)

    async def send_buttons(self, to: str, body: str, options: list[dict[str, str]]) -> None:
        await self._send(to, f"{body} {[o['title'] for o in options]}")

    async def send_template(self, to: str, name: str, language: str, params: list[str]) -> None:
        await self._send(to, f"plantilla {name} {params}")

    async def mark_read(self, message_id: str, typing: bool = True) -> None:
        return None

    async def download_media(self, media_id: str, max_bytes: int) -> tuple[bytes, str]:
        if media_id not in self.media:
            raise WhatsAppError("media no encontrado")
        return self.media[media_id]


class LogHandoff:
    async def notify(self, event: dict[str, Any]) -> None:
        log.info("[Handoff] %s para %s", event.get("type"), event.get("wa_id"))


# --- Modelo de reglas (sin GEMINI_API_KEY) ----------------------------------------------------------


def _text(text: str) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))])


def _call(name: str, **args: Any) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(candidates=[types.Candidate(
        content=types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]))])


def _usd(v: float) -> str:
    return f"${v:,.2f}"


class RuleBasedModels:
    """Imita las decisiones del modelo con reglas fijas, para probar sin clave de API.
    Lee el turno actual (texto del cliente, adjuntos y respuestas de herramientas) y decide el paso."""

    async def generate_content(self, *, model: str, contents: list[types.Content], config: Any):
        turn_start = max(i for i, c in enumerate(contents)
                         if c.role == "user" and not any(p.function_response for p in c.parts or []))
        user = contents[turn_start]
        text = " ".join(p.text or "" for p in user.parts or [] if p.text)
        has_media = any(p.inline_data for p in user.parts or [])
        results: dict[str, dict[str, Any]] = {}
        for c in contents[turn_start + 1:]:
            for p in c.parts or []:
                if p.function_response:
                    results[p.function_response.name] = dict(p.function_response.response or {})
        previous = " ".join(p.text or "" for c in contents[:turn_start] if c.role == "user"
                            for p in c.parts or [] if p.text and not p.function_response)
        allowed = {d.name for t in (config.tools or []) for d in (t.function_declarations or [])}
        return self._decide(text, previous, has_media, results, allowed)

    def _decide(self, text: str, previous: str, has_media: bool, r: dict[str, dict[str, Any]],
                allowed: set[str]) -> types.GenerateContentResponse:
        t = text.lower()
        contract = re.search(r"c-?\s?(\d{4})", t)
        contract_id = f"C-{contract.group(1)}" if contract else ""
        cedula = re.search(r"\b(\d{10}|\d{13})\b", t)

        def pick_contract(res: dict[str, Any]) -> str | None:
            ids = [c["id"] for c in res.get("contratos", [])]
            if contract_id in ids:
                return contract_id
            prev = re.findall(r"c-?\s?(\d{4})", previous.lower())
            if prev and f"C-{prev[-1]}" in ids:
                return f"C-{prev[-1]}"
            return ids[0] if len(ids) == 1 else None

        def need_customer() -> types.GenerateContentResponse | None:
            if "buscar_cliente" not in r:
                return _call("buscar_cliente", **({"cedula": cedula.group(1)} if cedula else {}))
            res = r["buscar_cliente"]
            if res.get("error") == "NUMERO_NO_VINCULADO":
                return _call("escalate_to_human", summary="Pide datos de una cédula no vinculada a su número; "
                                                         "verificar identidad.", priority="media")
            if res.get("error"):
                return _text("Para ayudarte necesito la *cédula* del titular del servicio.")
            if pick_contract(res) is None:
                ids = [c["id"] for c in res.get("contratos", [])][:3]
                return _call("offer_options", text="Tienes varios contratos. ¿Sobre cuál es tu consulta?",
                             options=[f"Contrato {i}" for i in ids])
            return None

        wants = lambda pattern: re.search(pattern, t) or (re.fullmatch(r"\s*(contrato\s*)?c-?\s?\d{4}\s*|\s*\d{10}\s*", t)
                                                          and re.search(pattern, previous.lower()))
        # Comprobante de pago
        if has_media and "validar_comprobante" in allowed:
            if "validar_comprobante" not in r:
                return _call("validar_comprobante")
            v = r["validar_comprobante"]
            if v.get("error") == "COMPROBANTE_YA_REGISTRADO":
                return _text("Ese comprobante ya fue registrado antes, así que no lo aplico de nuevo.")
            if not v.get("valido"):
                return _call("escalate_to_human", summary="Comprobante con problemas: " + ", ".join(v.get("problemas", [])),
                             priority="media")
            if step := need_customer():
                return step
            if "registrar_pago" not in r:
                return _call("registrar_pago", contrato=pick_contract(r["buscar_cliente"]))
            p = r["registrar_pago"]
            if p.get("error") in ("MONTO_NO_COINCIDE", "COMPROBANTE_INVALIDO", "NETWORK_CONTROL_NO_RESPONDE"):
                detail = (f"Monto {_usd(p['monto_usd'])}, deuda {_usd(p['deuda_usd'])}." if "deuda_usd" in p else p["error"])
                return _call("escalate_to_human", summary=f"Pago no registrado: {detail}", priority="alta")
            if p.get("error"):
                return _text("No pude registrar el pago. Un asesor lo revisará.")
            meses = ", ".join(p.get("aplicado_a", [])) or "tu saldo"
            return _text(f"¡Listo! Registré tu pago de *{_usd(p['monto_usd'])}* aplicado a {meses}. "
                         f"Saldo pendiente: {_usd(p['saldo_usd'])}. Estado del servicio: {p['estado_servicio']}.")
        # Deuda y pagos
        if "consultar_deuda" in allowed and wants(r"deb|deuda|pag|saldo|cuota|factur|cuanto|cuánto"):
            if step := need_customer():
                return step
            if "consultar_deuda" not in r:
                return _call("consultar_deuda", contrato=pick_contract(r["buscar_cliente"]))
            d = r["consultar_deuda"]
            if not d.get("cuotas"):
                return _text(f"Tu contrato {d.get('contrato')} está *al día*. ¡Gracias por tu puntualidad!")
            lines = "\n".join(f"- {c['concepto']} {c['mes']}: {_usd(c['valor_usd'])}" for c in d["cuotas"])
            return _text(f"Tu contrato {d['contrato']} tiene {len(d['cuotas'])} cuota(s) pendiente(s):\n{lines}\n"
                         f"Total: *{_usd(d['total_usd'])}*. Puedes transferir a las cuentas de la empresa y enviarme "
                         "aquí la foto del comprobante.")
        # Prórroga
        if "registrar_prorroga" in allowed and wants(r"prorroga|prórroga|plazo|esperar"):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", t)
            if step := need_customer():
                return step
            if not m:
                return _text("¿Hasta qué fecha necesitas la prórroga? Escríbela como AAAA-MM-DD.")
            if "registrar_prorroga" not in r:
                return _call("registrar_prorroga", contrato=pick_contract(r["buscar_cliente"]), fecha=m.group(1))
            pr = r["registrar_prorroga"]
            if pr.get("aprobada"):
                return _text(f"Prórroga aprobada hasta el *{pr['hasta']}*.")
            return _call("escalate_to_human", summary=f"Pide prórroga hasta {m.group(1)}; supera lo permitido.",
                         priority="baja")
        # Soporte técnico
        if "estado_ont" in allowed:
            if re.search(r"s[ií],?\s*reinici", t):
                if step := need_customer():
                    return step
                if "reiniciar_ont" not in r:
                    return _call("reiniciar_ont", contrato=pick_contract(r["buscar_cliente"]))
                rr = r["reiniciar_ont"]
                if rr.get("error") == "REINICIO_RECIENTE":
                    return _text(f"Ya reinicié tu equipo hace poco. {rr.get('hint', '')}.")
                return _text("Reinicié tu equipo. Espera unos 3 minutos y prueba de nuevo, por favor.")
            if re.search(r"s[ií],?\s*agend|visita", t):
                if step := need_customer():
                    return step
                if "crear_ticket_visita" not in r:
                    return _call("crear_ticket_visita", contrato=pick_contract(r["buscar_cliente"]),
                                 problema=(previous or text)[-200:])
                tk = r["crear_ticket_visita"]
                if tk.get("error") == "INCIDENTE_ACTIVO":
                    return _text(f"Hay un incidente en tu zona: {tk['descripcion']}. Solución estimada: {tk['solucion_estimada']}.")
                return _text(f"Agendé la visita técnica (ticket *{tk.get('ticket')}*) en {tk.get('direccion')}. "
                             "Te contactaremos para confirmar la hora.")
            if step := need_customer():
                return step
            if "estado_ont" not in r:
                return _call("estado_ont", contrato=pick_contract(r["buscar_cliente"]))
            o = r["estado_ont"]
            if o.get("incidente_en_zona"):
                inc = o["incidente_en_zona"]
                return _text(f"Hay un incidente en tu zona: {inc['descripcion']}. Solución estimada: "
                             f"*{inc['solucion_estimada']}*. No hace falta visita; te avisaremos.")
            if o.get("error") == "OLT_NO_RESPONDE":
                return _call("offer_options", text="No pude consultar tu equipo ahora. ¿Quieres que agende una visita?",
                             options=["Sí, agendar", "No, gracias"])
            if o.get("estado_servicio") == "suspendido":
                return _text("Tu servicio está *suspendido* por pagos pendientes. Puedes consultar tu deuda escribiendo "
                             "«cuánto debo».")
            if o.get("alerta") or not o.get("en_linea"):
                return _call("offer_options", text=f"Detecto un problema en la fibra de tu casa ({o.get('alerta') or 'equipo sin conexión'}). "
                                                   "¿Agendo una visita técnica?", options=["Sí, agendar", "No, gracias"])
            return _call("offer_options", text=f"Tu equipo está en línea (señal {o.get('potencia_dbm')} dBm). "
                                               "¿Quieres que lo reinicie?", options=["Sí, reiniciar", "No, gracias"])
        # Ventas
        if "consultar_planes" in allowed and re.search(r"plan|precio|contrat|instal|megas|promo|ofrec", t):
            if "consultar_planes" not in r:
                return _call("consultar_planes")
            planes = "\n".join(f"- *{p['name']}*: {_usd(p['price_usd'])} al mes ({p['description']})"
                               for p in r["consultar_planes"]["planes"])
            return _text(f"Estos son nuestros planes:\n{planes}\n¿Te gustaría contratar alguno? Dime tu dirección "
                         "para revisar la cobertura.")
        if re.search(r"^\s*(hola|buenas|buenos|hey|qué tal|que tal)", t):
            return _call("offer_options", text="¡Hola! ¿En qué te puedo ayudar?",
                         options=["Ver mi deuda", "Sin internet", "Planes"])
        if re.search(r"gracias|perfecto|genial|ok\b|listo", t):
            return _text("¡Con gusto! Si necesitas algo más, escríbeme por aquí.")
        if re.search(r"cuenta|horario|oficina|donde|dónde", t):
            return _text("Nuestro horario de atención es de lunes a viernes de 08:00 a 18:00 y sábados de 09:00 a 13:00.")
        return _text("Puedo ayudarte con pagos, fallas del servicio y planes. ¿Qué necesitas?")


class RuleBasedClient:
    def __init__(self) -> None:
        self.models = RuleBasedModels()
        self.aio = self


# --- Endpoints solo de la demo -------------------------------------------------------------------


class CustomerMessage(BaseModel):
    text: str = Field(default="", max_length=2000)
    wa_id: str = Field(default=DEMO_CUSTOMERS[0]["wa_id"], pattern=r"^\d{6,15}$")
    name: str = Field(default="", max_length=60)
    media_b64: str | None = Field(default=None, max_length=14_000_000)
    mime_type: str = "image/jpeg"
    receipt: dict[str, Any] | None = None  # comprobante simulado (modo reglas)


def demo_router(secret_key: str, ai: dict[str, str], wa: DemoWhatsApp) -> APIRouter:
    async def require_user(request: Request, authorization: str = Header(default="")) -> None:
        uid = auth.read_token(authorization.removeprefix("Bearer ").strip(), secret_key)
        if uid is None or await request.app.state.repo.user_by_id(uid) is None:
            raise HTTPException(status_code=401)

    router = APIRouter()

    @router.get("/demo-info")
    async def demo_info() -> dict[str, Any]:  # sin sesión: solo credenciales de ejemplo
        return {"demo": True, "password": DEMO_PASSWORD,
                "users": [{"email": e, "role": r} for e, _, r in DEMO_USERS]}

    @router.get("/api/admin/demo", dependencies=[Depends(require_user)])
    async def info() -> dict[str, Any]:
        return {"demo": True, "ai": ai["label"], "customers": DEMO_CUSTOMERS}

    @router.post("/api/admin/demo/incoming", dependencies=[Depends(require_user)])
    async def incoming(request: Request, body: CustomerMessage) -> dict[str, str]:
        if not body.text and not body.media_b64 and body.receipt is None:
            raise HTTPException(status_code=400, detail="Escribe un mensaje o adjunta un comprobante")
        name = body.name or next((c["name"] for c in DEMO_CUSTOMERS if c["wa_id"] == body.wa_id), "Cliente")
        msg_id = f"demo.{random.getrandbits(64):x}"
        msg = IncomingMessage(wa_id=body.wa_id, message_id=msg_id, type="text", text=body.text,
                              timestamp=int(time.time()), profile_name=name)
        if body.media_b64 or body.receipt is not None:
            data = (base64.b64decode(body.media_b64) if body.media_b64
                    else json.dumps(body.receipt).encode())
            media_id = f"media.{random.getrandbits(48):x}"
            wa.media[media_id] = (data, body.mime_type)
            msg.type, msg.media_id, msg.mime_type = "image", media_id, body.mime_type
        await request.app.state.queue.publish(msg)  # mismo camino que un webhook real
        return {"wa_id": body.wa_id}

    @router.post("/api/admin/demo/fail-sends", dependencies=[Depends(require_user)])
    async def fail_sends(n: int = 2) -> dict[str, int]:
        wa.fail_next = max(0, min(n, 10))
        return {"fail_next": wa.fail_next}

    return router


# --- Arranque -------------------------------------------------------------------------------------


async def seed(repo: Repo, store: SessionStore) -> None:
    for email, name, role in DEMO_USERS:
        await repo.create_user(email, name, role, auth.hash_password(DEMO_PASSWORD))
    now = time.time()
    convs = [
        ("593987654321", "Jorge Andrade", "pendiente_humano", "soporte", [
            ("user", "Desde ayer no tengo internet y ya pagué"),
            ("bot", "Hay un incidente en tu zona: Corte de fibra troncal en el sector. Solución estimada: *hoy a las 18:00*."),
            ("user", "Eso me dijeron ayer, necesito hablar con alguien"),
            ("system", "Derivado a un asesor (prioridad alta): cliente molesto por corte prolongado en El Batán")], 50),
        ("593998887766", "Luis Pacheco", "bot", "ventas", [
            ("user", "¿Qué planes tienen?"),
            ("bot", "Tenemos Hogar 100 Mbps a $25,00, Hogar 200 Mbps a $35,00 y Gamer 300 Mbps a $45,00.")], 120),
        ("593991112222", "Carla Ruiz", "cerrada", "cobros", [
            ("user", "Ya transferí, les mando el comprobante"),
            ("bot", "¡Listo! Registré tu pago de *$25,00*."),
            ("system", "Marta (asesora) cerró la conversación")], 26 * 60),
    ]
    for wa_id, name, state, area, msgs, age_min in convs:
        t0 = now - age_min * 60
        for i, (role, text) in enumerate(msgs):
            await repo.record(wa_id, role, text, name=name, area=area if role == "bot" else "",
                              prompt_version=f"{area}@v1+v1" if role == "bot" else "", ts=t0 + i * 45)
        await repo.update_conversation(wa_id, state=state, area=area, disclosed=True,
                                       state_since=t0 + len(msgs) * 45, priority="alta" if state != "bot" else "")

    rng = random.Random(7)
    today = datetime.now(timezone.utc).date()
    for i in range(30):
        day = today - timedelta(days=i)
        turns = rng.randint(90, 260) if day.weekday() < 5 else rng.randint(30, 90)
        await store.r.hset(f"metrics:{day:%Y-%m-%d}", mapping={
            "turns": turns, "handoffs": int(turns * rng.uniform(0.05, 0.12)), "errors": rng.randint(0, 3),
            "latency_ms_sum": int(turns * rng.uniform(1400, 2300)), "agent_messages": rng.randint(5, 30),
            "tokens": turns * rng.randint(2500, 4000), "flagged": rng.randint(0, 4),
            "payments": int(turns * 0.08), "payments_cents": int(turns * 0.08) * 2800,
            "area:cobros": int(turns * 0.45), "area:soporte": int(turns * 0.35), "area:ventas": int(turns * 0.1),
            "area:general": int(turns * 0.1), "tool:buscar_cliente": int(turns * 0.6),
            "tool:consultar_deuda": int(turns * 0.35), "tool:estado_ont": int(turns * 0.3),
            "tool:registrar_pago": int(turns * 0.08), "tool:offer_options": int(turns * 0.3),
            "tool:escalate_to_human": int(turns * 0.06)})


def build_demo() -> tuple[Any, SessionStore, Agent, Repo]:
    secret = os.environ.get("SECRET_KEY") or "demo-secret"
    settings = Settings(wa_token="demo", wa_phone_number_id="demo", wa_app_secret="demo", wa_verify_token="demo",
                        secret_key=secret, pseudonym_pepper="demo",
                        database_url=f"sqlite+aiosqlite:///{tempfile.mkdtemp(prefix='whatsappia-demo-')}/demo.db",
                        debounce_seconds=float(os.environ.get("DEBOUNCE_SECONDS", "1.0")))
    store = SessionStore(fakeredis.aioredis.FakeRedis(decode_responses=True), settings.session_ttl_seconds)
    db = Database(settings.database_url)
    wa = DemoWhatsApp()
    nc = FakeNetworkControl(today=date.today())

    if os.environ.get("GEMINI_API_KEY"):
        from google import genai

        client = genai.Client()
        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        agent = Agent(client, model, os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL))
        classifier: Any = LLMClassifier(client, os.environ.get("CLASSIFIER_MODEL", DEFAULT_LITE_MODEL))
        isp = ISP(nc=nc, olt=FakeOLT(nc), receipts=GeminiReceiptReader(client, model))
        suggester = RuleSuggester(client, model)
        ai_label = f"Gemini real ({model})"
    else:
        agent = Agent(RuleBasedClient(), "reglas")  # type: ignore[arg-type]
        classifier = KeywordClassifier()
        isp = ISP(nc=nc, olt=FakeOLT(nc), receipts=FakeReceiptReader())
        suggester = RuleSuggester()
        ai_label = "reglas simples (añade GEMINI_API_KEY para usar Gemini)"

    app = create_app(settings, store=store, db=db, channel=wa, agent=agent, classifier=classifier, isp=isp,
                     handoff=LogHandoff(), suggester=suggester)
    app.state.ai = {"label": ai_label}
    app.include_router(demo_router(secret, app.state.ai, wa))
    app.state.demo_wa = wa
    return app, store, agent, Repo(db)


async def check_gemini(agent: Agent) -> str | None:
    """Comprueba la clave y los modelos antes de abrir el panel. Devuelve un aviso o None."""
    if isinstance(agent.client, RuleBasedClient):
        return None
    problems = []
    models = list(dict.fromkeys(m for m in (agent.model, agent.fallback_model) if m))
    for model in models:
        try:
            await agent.client.aio.models.get(model=model)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{model}: {describe_error(exc)}")
    if len(problems) == len(models):
        return "ningún modelo responde. " + " | ".join(problems)
    return ("modelo de respaldo usado: " + " | ".join(problems)) if problems else None


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app, store, agent, repo = build_demo()
    port = int(os.environ.get("PORT", "8080"))
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning"))

    async def after_start() -> None:
        while not server.started:
            await asyncio.sleep(0.1)
        await seed(app.state.repo, store)
        warning = await check_gemini(agent)
        if warning:
            app.state.ai["label"] += f" · ERROR: {warning}"
            print(f"\n  ATENCIÓN Gemini: {warning}\n  Revisa GEMINI_API_KEY o elige otro modelo con GEMINI_MODEL "
                  "(lista: https://ai.google.dev/gemini-api/docs/models).\n", flush=True)
        print(f"\n  Panel listo: http://localhost:{port}/admin/\n  Usuarios: "
              + ", ".join(e for e, _, _ in DEMO_USERS) + f" · contraseña: {DEMO_PASSWORD}\n", flush=True)

    await asyncio.gather(server.serve(), after_start())


if __name__ == "__main__":
    asyncio.run(main())
