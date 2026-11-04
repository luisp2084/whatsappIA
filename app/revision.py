"""Propuesta de reglas a partir de una respuesta marcada como mala ("Analizar con IA")."""

from __future__ import annotations

import logging
from typing import Any

from google.genai import types

log = logging.getLogger(__name__)

PROMPT = """Un asesor marcó como incorrecta la respuesta de un asistente de WhatsApp de un proveedor de internet.
Escribe UNA regla breve, concreta y verificable para el prompt del asistente que evite el error.
Empieza con un verbo en imperativo, incluye un ejemplo entre comillas si ayuda, máximo 2 frases.

Mensaje del cliente: {customer}
Respuesta del asistente: {bot}
Motivo: {reason}
Lo que debió hacer, según el asesor: {note}

Regla:"""


class RuleSuggester:
    def __init__(self, client: Any = None, model: str = ""):
        self.client = client
        self.model = model

    async def suggest(self, customer: str, bot: str, reason: str, note: str) -> str:
        if self.client is not None and self.model:
            try:
                resp = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=[types.Content(role="user", parts=[types.Part.from_text(text=PROMPT.format(
                        customer=customer[:500], bot=bot[:800], reason=reason, note=note[:500]))])],
                    config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=200))
                text = (resp.text or "").strip()
                if text:
                    return text
            except Exception:  # noqa: BLE001
                log.warning("No se pudo proponer la regla con IA", exc_info=True)
        if note:
            note = note.strip().rstrip(".")
            return f"Cuando el cliente escriba algo como «{customer[:80]}», {note[:1].lower() + note[1:]}."
        return f"No vuelvas a responder «{bot[:80]}» en esta situación; deriva a un asesor si no tienes el dato."
