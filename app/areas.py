"""Áreas de atención: qué herramientas tiene cada una y cómo se elige el área de un mensaje."""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from google.genai import types
from pydantic import BaseModel

from .prompts import classifier_prompt
from .tools import DECLARATIONS, ESCALATE, OFFER_OPTIONS

log = logging.getLogger(__name__)

AreaName = Literal["cobros", "soporte", "ventas", "general"]
COMMON_TOOLS = (OFFER_OPTIONS, ESCALATE)

AREA_TOOLS: dict[str, tuple[str, ...]] = {
    "cobros": ("buscar_cliente", "consultar_deuda", "validar_comprobante", "registrar_pago", "registrar_prorroga"),
    "soporte": ("buscar_cliente", "estado_ont", "reiniciar_ont", "crear_ticket_visita"),
    "ventas": ("consultar_planes",),
    "general": ("consultar_planes",),
}
AREAS = tuple(AREA_TOOLS)


@dataclass(frozen=True)
class Area:
    name: str
    tools: tuple[str, ...]

    @property
    def allowed(self) -> set[str]:
        return {*self.tools, *COMMON_TOOLS}

    @property
    def declarations(self) -> list[types.FunctionDeclaration]:
        return [DECLARATIONS[t] for t in (*self.tools, *COMMON_TOOLS)]


def area(name: str) -> Area:
    return Area(name, AREA_TOOLS.get(name, AREA_TOOLS["general"]))


class Classification(BaseModel):
    area: AreaName
    quiere_humano: bool = False


class Classifier(Protocol):
    async def classify(self, text: str, previous_area: str, has_media: bool) -> Classification: ...


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()


KEYWORDS: list[tuple[str, str]] = [
    ("soporte", r"internet|lent[oa]|wifi|router|modem|\bonu\b|\bont\b|no (me )?(funciona|hay|tengo)|"
                r"sin (servicio|senal)|se cae|caid|\bcorte|desconect|visita|tecnico|reinici|luz roja"),
    ("cobros", r"pag|deuda|debo|saldo|factur|comprobante|transferi|deposit|prorroga|plazo|cobr|cuota|"
               r"reconex|suspend|valor a pagar"),
    ("ventas", r"plan|precio|contrat|instal|cobertura|megas|mbps|velocidad mayor|cambiar de plan|promo"),
]
SELECTION_RE = (r"(el |mi )?(contrato\s*)?c-?\s?\d{3,6}|\d{10}|\d{13}|\d{4}-\d{2}-\d{2}|"
                r"(si|no|ok|dale|claro|vale)\b.{0,40}")
HUMAN_RE = r"\b(persona|humano|agente|asesor(a)?|operador(a)?|alguien real)\b"


class KeywordClassifier:
    """Reglas simples; también es el respaldo si el clasificador con IA falla."""

    async def classify(self, text: str, previous_area: str, has_media: bool) -> Classification:
        t = _plain(text)
        wants_human = bool(re.search(HUMAN_RE, t))
        # Respuestas de selección (un contrato, una cédula, sí/no, una fecha) siguen el tema anterior.
        if previous_area in AREAS and not has_media and re.fullmatch(SELECTION_RE, t.strip()):
            return Classification(area=previous_area, quiere_humano=wants_human)  # type: ignore[arg-type]
        for name, pattern in KEYWORDS:
            if re.search(pattern, t):
                return Classification(area=name, quiere_humano=wants_human)
        if has_media:
            return Classification(area="cobros", quiere_humano=wants_human)  # casi siempre, un comprobante
        if previous_area in AREAS:
            return Classification(area=previous_area, quiere_humano=wants_human)  # type: ignore[arg-type]
        return Classification(area="general", quiere_humano=wants_human)


class LLMClassifier:
    """Una llamada barata al modelo ligero con salida JSON. Si falla, usa las palabras clave."""

    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model
        self.fallback = KeywordClassifier()

    async def classify(self, text: str, previous_area: str, has_media: bool) -> Classification:
        if not text.strip():
            return await self.fallback.classify(text, previous_area, has_media)
        try:
            resp = await self.client.aio.models.generate_content(
                model=self.model,
                contents=[types.Content(role="user", parts=[types.Part.from_text(
                    text=f"{classifier_prompt(previous_area)}\n\nMensaje: {text[:1000]}"
                         + ("\n(El cliente adjuntó una imagen o PDF.)" if has_media else ""))])],
                config=types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json",
                                                   response_schema=Classification, max_output_tokens=100))
            parsed = resp.parsed
            if isinstance(parsed, Classification):
                return parsed
            return Classification.model_validate_json(resp.text or "")
        except Exception:  # noqa: BLE001
            log.warning("Clasificador con IA falló; uso palabras clave", exc_info=True)
            return await self.fallback.classify(text, previous_area, has_media)
