"""Evaluaciones del agente sobre el conjunto dorado (`evals/casos/*.yaml`).

    python -m evals.run                    # todos los casos
    python -m evals.run --filtro cobros    # solo los ids que contienen "cobros"

Cada caso corre aislado con el Worker real (cola, áreas, prompts, herramientas, outbox, estados),
Network Control y OLT simulados con datos congelados, y:
- Gemini real si existe GEMINI_API_KEY (lo que hay que correr antes de liberar un cambio de
  prompt, modelo, regla o herramienta), o
- el modelo de reglas de la demo si no hay clave (solo comprueba que el arnés funciona).

Escribe un informe en Markdown (por defecto `evals/report.md`) y termina con código 1 si algún
caso falla: todos los criterios son bloqueantes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fakeredis
import yaml

from app.agent import Agent
from app.areas import KeywordClassifier, LLMClassifier
from app.config import DEFAULT_FALLBACK_MODEL, DEFAULT_LITE_MODEL, DEFAULT_MODEL
from app.control import ControlCenter
from app.db import Database
from app.demo import DemoWhatsApp, RuleBasedClient
from app.isp import ISP, FakeNetworkControl, FakeOLT, FakeReceiptReader, GeminiReceiptReader
from app.parsing import IncomingMessage
from app.repo import Repo
from app.session import SessionStore
from app.tools import REGISTRY
from app.worker import Worker

CUSTOMERS = {"maria": ("593999000001", "María Fernanda Cedeño"), "jorge": ("593987654321", "Jorge Andrade"),
             "luis": ("593998887766", "Luis Pacheco"), "nuevo": ("593990000099", "Cliente nuevo")}


@dataclass
class Outcome:
    case: dict[str, Any]
    failures: list[str] = field(default_factory=list)
    bot_texts: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.failures and not self.error


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def ai_backend() -> tuple[Any, str, Any, Any]:
    if os.environ.get("GEMINI_API_KEY"):
        from google import genai

        client = genai.Client()
        model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        return (Agent(client, model, os.environ.get("GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)),
                f"Gemini ({model})", LLMClassifier(client, os.environ.get("CLASSIFIER_MODEL", DEFAULT_LITE_MODEL)),
                GeminiReceiptReader(client, model))
    return Agent(RuleBasedClient(), "reglas"), "modelo de reglas (sin GEMINI_API_KEY)", KeywordClassifier(), FakeReceiptReader()


async def run_case(case: dict[str, Any], workdir: Path) -> Outcome:
    out = Outcome(case=case)
    started = time.monotonic()
    wa_id, name = CUSTOMERS[case.get("cliente", "maria")]
    db = Database(f"sqlite+aiosqlite:///{workdir}/{case['id']}.db")
    await db.create_all()
    repo = Repo(db)
    cc = ControlCenter.model_validate(deep_merge(ControlCenter().model_dump(), case.get("control", {})))
    await repo.set_control(cc, "evals")
    store = SessionStore(fakeredis.aioredis.FakeRedis(decode_responses=True), 3600)
    nc = FakeNetworkControl()
    agent, _, classifier, reader = ai_backend()
    channel = DemoWhatsApp()

    simulated = case.get("herramientas_simuladas", {}) or {}
    registry = {}
    for tool_name, fn in REGISTRY.items():
        async def wrapped(args, ctx, _n=tool_name, _fn=fn):
            out.tools.append(_n)
            if _n in simulated:
                return dict(simulated[_n])
            return await _fn(args, ctx)
        registry[tool_name] = wrapped
    agent.registry = registry

    worker = Worker(store, repo, channel, agent, ISP(nc=nc, olt=FakeOLT(nc), receipts=reader), _NoHandoff(),
                    classifier=classifier, debounce_seconds=0.0, max_history=30, max_media_bytes=10_000_000,
                    pepper="evals")
    try:
        for i, turn in enumerate(case["turnos"]):
            msg = IncomingMessage(wa_id=wa_id, message_id=f"{case['id']}.{i}", type="text",
                                  text=str(turn.get("cliente", "")), profile_name=name)
            if "comprobante" in turn:
                data = {"es_comprobante": True, "banco": "Banco Pichincha", "numero": f"{case['id']}-{i}",
                        "fecha": cc.now().date().isoformat(), "cuenta_destino": "2200123456", "confianza": 0.95,
                        **turn["comprobante"]}
                channel.media[f"m{i}"] = (json.dumps(data).encode(), "image/jpeg")
                msg.type, msg.media_id, msg.mime_type = "image", f"m{i}", "image/jpeg"
            await worker.handle_incoming(msg)
    except Exception as exc:  # noqa: BLE001
        out.error = f"{type(exc).__name__}: {exc}"
    view = await repo.conversation_view(wa_id) or {"meta": {}, "messages": []}
    out.bot_texts = [m["text"] for m in view["messages"] if m["role"] == "bot"]
    out.seconds = time.monotonic() - started
    check(out, case.get("espera", {}), view["meta"], len(nc.payments))
    await db.dispose()
    return out


class _NoHandoff:
    async def notify(self, event: dict[str, Any]) -> None:
        return None


def check(out: Outcome, exp: dict[str, Any], meta: dict[str, Any], payments: int) -> None:
    text = "\n".join(out.bot_texts).lower()
    f = out.failures
    if "area" in exp and meta.get("area") != exp["area"]:
        f.append(f"área {meta.get('area')!r}, se esperaba {exp['area']!r}")
    if "deriva" in exp:
        derived = meta.get("state") in ("pendiente_humano", "asignada")
        if derived != exp["deriva"]:
            f.append("derivó a un asesor" if derived else "no derivó a un asesor")
    if "estado" in exp and meta.get("state") != exp["estado"]:
        f.append(f"estado {meta.get('state')!r}, se esperaba {exp['estado']!r}")
    if "mensajes_enviados" in exp and len(out.bot_texts) != exp["mensajes_enviados"]:
        f.append(f"{len(out.bot_texts)} mensajes del bot, se esperaban {exp['mensajes_enviados']}")
    for t in exp.get("herramientas", []):
        if t not in out.tools:
            f.append(f"no usó {t}")
    for t in exp.get("herramientas_prohibidas", []):
        if t in out.tools:
            f.append(f"usó {t}, que estaba prohibida")
    for s in exp.get("contiene", []):
        if str(s).lower() not in text:
            f.append(f"no contiene «{s}»")
    for s in exp.get("no_contiene", []):
        if str(s).lower() in text:
            f.append(f"contiene «{s}»")
    if "pagos" in exp and payments != exp["pagos"]:
        f.append(f"{payments} pago(s) registrados, se esperaban {exp['pagos']}")


def report(outcomes: list[Outcome], backend: str) -> str:
    passed = sum(o.ok for o in outcomes)
    lines = [f"# Evaluaciones del agente", "",
             f"**{passed} de {len(outcomes)} casos pasan** · IA: {backend} · {time.strftime('%Y-%m-%d %H:%M')}", "",
             "| Resultado | Caso | Detalle | Herramientas | s |", "| --- | --- | --- | --- | --- |"]
    for o in outcomes:
        detail = o.error or "; ".join(o.failures) or o.case.get("descripcion", "")
        lines.append(f"| {'✅' if o.ok else '❌'} | `{o.case['id']}` | {detail.replace('|', '/')} | "
                     f"{', '.join(dict.fromkeys(o.tools)) or '—'} | {o.seconds:.1f} |")
    failed = [o for o in outcomes if not o.ok]
    if failed:
        lines += ["", "## Respuestas de los casos fallidos", ""]
        for o in failed:
            lines += [f"### `{o.case['id']}`", ""] + [f"> {t.replace(chr(10), ' ')}" for t in o.bot_texts] + [""]
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Evalúa el agente sobre el conjunto dorado.")
    parser.add_argument("--casos", default=str(Path(__file__).parent / "casos"))
    parser.add_argument("--filtro", default="")
    parser.add_argument("--informe", default=str(Path(__file__).parent / "report.md"))
    args = parser.parse_args()

    cases: list[dict[str, Any]] = []
    for path in sorted(Path(args.casos).glob("*.yaml")):
        cases += [c for c in yaml.safe_load(path.read_text(encoding="utf-8")) or [] if args.filtro in c["id"]]
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        print("Hay ids de caso repetidos", file=sys.stderr)
        return 2
    _, backend, _, _ = ai_backend()
    with tempfile.TemporaryDirectory(prefix="evals-") as tmp:
        outcomes = [await run_case(c, Path(tmp)) for c in cases]
    text = report(outcomes, backend)
    Path(args.informe).write_text(text, encoding="utf-8")
    print(text)
    return 0 if all(o.ok for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
