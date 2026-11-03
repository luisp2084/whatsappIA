"""Bucle del agente: Gemini + function calling manual, con modelo de respaldo."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from google import genai
from google.genai import types

from .prompts import SYSTEM_PROMPT
from .tools import ESCALATE, OFFER_OPTIONS, TOOLS, ToolContext, ToolFn, call_tool

log = logging.getLogger(__name__)

MAX_STEPS = 5  # llamadas al modelo por turno (evita bucles de herramientas)


@dataclass
class AgentResult:
    text: str = ""
    options: list[str] = field(default_factory=list)
    handoff_summary: str | None = None
    handoff_priority: str = "media"
    history: list[types.Content] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    model: str = ""
    latency_ms: int = 0


class Agent:
    def __init__(self, client: genai.Client, model: str, fallback_model: str | None = None,
                 thinking_budget: int | None = 0, registry: dict[str, ToolFn] | None = None,
                 temperature: float = 0.3):
        self.client = client
        self.model = model
        self.fallback_model = fallback_model
        self.registry = registry
        thinking = types.ThinkingConfig(thinking_budget=thinking_budget) if thinking_budget is not None else None
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=TOOLS,
            temperature=temperature,
            max_output_tokens=1024,
            thinking_config=thinking,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def run(self, history: list[types.Content], user_parts: list[types.Part],
                  ctx: ToolContext) -> AgentResult:
        started = time.monotonic()
        result = AgentResult()
        contents = [*history, types.Content(role="user", parts=user_parts)]

        for _ in range(MAX_STEPS):
            response, result.model = await self._generate(contents)
            candidate = response.candidates[0] if response.candidates else None
            if candidate is None or candidate.content is None or not candidate.content.parts:
                break  # respuesta vacía o bloqueada por seguridad
            contents.append(candidate.content)

            calls = [p.function_call for p in candidate.content.parts if p.function_call]
            if not calls:
                result.text = "".join(p.text or "" for p in candidate.content.parts
                                      if p.text and not p.thought).strip()
                break

            responses: list[types.Part] = []
            terminal = False
            for call in calls:
                args = dict(call.args or {})
                result.tools_used.append(call.name)
                if call.name == OFFER_OPTIONS:
                    result.text = str(args.get("text", ""))
                    result.options = [str(o) for o in (args.get("options") or [])][:3]
                    output: dict[str, Any] = {"sent": True}
                    terminal = True
                elif call.name == ESCALATE:
                    result.handoff_summary = str(args.get("summary", ""))
                    result.handoff_priority = str(args.get("priority", "media"))
                    output = {"transferred": True}
                    terminal = True
                else:
                    output = await call_tool(call.name, args, ctx, self.registry)
                responses.append(types.Part.from_function_response(name=call.name, response=output))
            contents.append(types.Content(role="user", parts=responses))
            if terminal:
                break

        result.history = contents
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    async def _generate(self, contents: list[types.Content]) -> tuple[types.GenerateContentResponse, str]:
        try:
            return await self.client.aio.models.generate_content(
                model=self.model, contents=contents, config=self.config), self.model
        except Exception:
            if not self.fallback_model:
                raise
            log.warning("Fallo en %s; usando %s", self.model, self.fallback_model, exc_info=True)
            return await self.client.aio.models.generate_content(
                model=self.fallback_model, contents=contents, config=self.config), self.fallback_model


# ---------------------------------------------------------------------------
# Historial: serialización, recorte y limpieza de binarios
# ---------------------------------------------------------------------------

def load_history(raw: list[dict[str, Any]]) -> list[types.Content]:
    return [types.Content.model_validate(c) for c in raw]


def dump_history(contents: list[types.Content], max_contents: int) -> list[dict[str, Any]]:
    """Quita binarios (fotos/audio no se guardan), recorta y serializa.

    Se conservan los thought_signature de las partes del modelo: Gemini los
    necesita para continuar correctamente los turnos con function calling.
    """
    cleaned = [_strip_media(c) for c in contents]
    trimmed = trim_history(cleaned, max_contents)
    return [c.model_dump(mode="json", exclude_none=True) for c in trimmed]


def trim_history(contents: list[types.Content], max_contents: int) -> list[types.Content]:
    if len(contents) <= max_contents:
        return contents
    tail = contents[-max_contents:]
    # El historial debe empezar en un mensaje real del usuario, no a mitad de una llamada a herramienta.
    for i, c in enumerate(tail):
        if c.role == "user" and not any(p.function_response for p in (c.parts or [])):
            return tail[i:]
    return []


def _strip_media(content: types.Content) -> types.Content:
    if not any(p.inline_data for p in (content.parts or [])):
        return content
    parts = []
    for p in content.parts or []:
        if p.inline_data:
            kind = (p.inline_data.mime_type or "archivo").split("/")[0]
            parts.append(types.Part.from_text(text=f"[El usuario envió un {kind}]"))
        else:
            parts.append(p)
    return types.Content(role=content.role, parts=parts)
