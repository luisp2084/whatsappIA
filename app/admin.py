"""API del panel web (/api/admin/*). Autenticación: Authorization: Bearer <ADMIN_TOKEN>.

En producción pon el panel detrás de SSO (Cloud IAP, Cloudflare Access...) además del token.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .worker import WindowClosedError


class AgentMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    agent_name: str = Field(default="", max_length=60)


def build_router(admin_token: str) -> APIRouter:
    def require_admin(authorization: str = Header(default="")) -> None:
        if not admin_token or not hmac.compare_digest(authorization, f"Bearer {admin_token}"):
            raise HTTPException(status_code=401, detail="Token de administrador inválido")

    router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

    @router.get("/conversations")
    async def conversations(request: Request, limit: int = 100) -> list[dict[str, Any]]:
        return await request.app.state.store.list_conversations(min(limit, 500))

    @router.get("/conversations/{wa_id}")
    async def conversation(request: Request, wa_id: str) -> dict[str, Any]:
        store = request.app.state.store
        if not await store.r.exists(f"meta:{wa_id}"):
            raise HTTPException(status_code=404, detail="Conversación no encontrada o caducada")
        return {"meta": await store.meta(wa_id), "messages": await store.transcript(wa_id)}

    @router.post("/conversations/{wa_id}/messages")
    async def send(request: Request, wa_id: str, body: AgentMessage) -> dict[str, str]:
        try:
            await request.app.state.worker.send_agent_message(wa_id, body.text, body.agent_name)
        except WindowClosedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "sent"}

    @router.post("/conversations/{wa_id}/takeover")
    async def takeover(request: Request, wa_id: str) -> dict[str, str]:
        await request.app.state.worker.set_mode(wa_id, "human")
        return {"mode": "human"}

    @router.post("/conversations/{wa_id}/release")
    async def release(request: Request, wa_id: str) -> dict[str, str]:
        await request.app.state.worker.set_mode(wa_id, "bot")
        return {"mode": "bot"}

    @router.get("/metrics")
    async def metrics(request: Request, days: int = 14) -> dict[str, Any]:
        rows = await request.app.state.store.metrics(max(1, min(days, 90)))
        return {"days": rows}

    return router
