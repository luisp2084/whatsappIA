"""API del panel web: autenticación, conversaciones, Revisión, Centro de control, métricas,
auditoría y usuarios. Cada ruta exige un permiso de la matriz de `app/auth.py`."""

from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from . import auth, states
from .areas import AREAS
from .control import ControlCenter
from .db import User
from .worker import ActionError, WindowClosedError


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class PasswordBody(BaseModel):
    current: str
    new: str


class AgentMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


class TemplateBody(BaseModel):
    name: str
    params: list[str] = Field(default_factory=list, max_length=10)


class FlagBody(BaseModel):
    reason: str = Field(default="otro", pattern="^(dato_incorrecto|alucino|tono|no_derivo|derivo_sin_motivo|duplicado|otro)$")
    note: str = Field(default="", max_length=1000)


class RuleBody(BaseModel):
    area: str = "todas"
    text: str = Field(min_length=5, max_length=600)


class RuleUpdate(BaseModel):
    active: bool | None = None
    text: str | None = Field(default=None, min_length=5, max_length=600)
    area: str | None = None


class BotSwitch(BaseModel):
    enabled: bool


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(pattern="^(asesor|admin|owner)$")
    password: str


class UserUpdate(BaseModel):
    name: str | None = None
    role: str | None = Field(default=None, pattern="^(asesor|admin|owner)$")
    active: bool | None = None
    password: str | None = None


def _public(u: User) -> dict[str, Any]:
    return {"id": u.id, "email": u.email, "name": u.name, "role": u.role, "active": u.active,
            "permissions": sorted(auth.PERMISSIONS.get(u.role, set()))}


def build_router(secret_key: str) -> APIRouter:
    async def current_user(request: Request, authorization: str = Header(default="")) -> User:
        token = authorization.removeprefix("Bearer ").strip()
        uid = auth.read_token(token, secret_key) if token else None
        user = await request.app.state.repo.user_by_id(uid) if uid else None
        if user is None or not user.active:
            raise HTTPException(status_code=401, detail="Sesión caducada o no válida. Vuelve a entrar.")
        return user

    def require(permission: str) -> Callable[..., Any]:
        async def dep(user: User = Depends(current_user)) -> User:
            if not auth.can(user.role, permission):
                raise HTTPException(status_code=403, detail="Tu rol no tiene permiso para esta acción.")
            return user
        return dep

    router = APIRouter()

    # --- autenticación -------------------------------------------------------------------

    @router.post("/api/auth/login")
    async def login(request: Request, body: LoginBody) -> dict[str, Any]:
        repo = request.app.state.repo
        store = request.app.state.store
        key = f"login:{body.email.lower()}"
        attempts = await store.get_json(key) or 0
        if attempts >= 5:
            raise HTTPException(status_code=429, detail="Demasiados intentos. Espera 10 minutos.")
        user = await repo.user_by_email(body.email)
        if user is None or not user.active or not auth.verify_password(body.password, user.password_hash):
            await store.put_json(key, attempts + 1, 600)
            raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")
        await store.pop_json(key)
        await repo.audit(user.email, "auth.login")
        return {"token": auth.issue_token(user.id, secret_key), "user": _public(user)}

    @router.get("/api/auth/me")
    async def me(user: User = Depends(current_user)) -> dict[str, Any]:
        return _public(user)

    @router.post("/api/auth/password")
    async def change_password(request: Request, body: PasswordBody, user: User = Depends(current_user)) -> dict[str, str]:
        if not auth.verify_password(body.current, user.password_hash):
            raise HTTPException(status_code=400, detail="La contraseña actual no es correcta.")
        if problem := auth.validate_password(body.new):
            raise HTTPException(status_code=400, detail=problem)
        await request.app.state.repo.update_user(user.id, password_hash=auth.hash_password(body.new))
        await request.app.state.repo.audit(user.email, "auth.password_changed")
        return {"status": "ok"}

    # --- conversaciones -------------------------------------------------------------------

    @router.get("/api/admin/conversations")
    async def conversations(request: Request, tab: str = "todas", limit: int = 200,
                            user: User = Depends(require("conversations.view"))) -> dict[str, Any]:
        repo = request.app.state.repo
        if tab not in states.TABS:
            raise HTTPException(status_code=400, detail="Pestaña desconocida")
        return {"items": await repo.list_conversations(tab, user.id, min(limit, 500)),
                "counts": await repo.tab_counts(user.id)}

    @router.get("/api/admin/conversations/{wa_id}")
    async def conversation(request: Request, wa_id: str,
                           user: User = Depends(require("conversations.view"))) -> dict[str, Any]:
        view = await request.app.state.repo.conversation_view(wa_id)
        if view is None:
            raise HTTPException(status_code=404, detail="Conversación no encontrada")
        return view

    async def _act(fn: Any, *args: Any) -> None:
        try:
            await fn(*args)
        except WindowClosedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ActionError, states.InvalidTransition) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/api/admin/conversations/{wa_id}/messages")
    async def send(request: Request, wa_id: str, body: AgentMessage,
                   user: User = Depends(require("conversations.act"))) -> dict[str, str]:
        await _act(request.app.state.worker.send_agent_message, wa_id, body.text, user)
        return {"status": "sent"}

    @router.post("/api/admin/conversations/{wa_id}/template")
    async def template(request: Request, wa_id: str, body: TemplateBody,
                       user: User = Depends(require("conversations.act"))) -> dict[str, str]:
        await _act(request.app.state.worker.send_template, wa_id, body.name, body.params, user)
        return {"status": "sent"}

    @router.post("/api/admin/conversations/{wa_id}/{action}")
    async def act(request: Request, wa_id: str, action: str,
                  user: User = Depends(require("conversations.act"))) -> dict[str, str]:
        worker = request.app.state.worker
        fn = {"take": worker.take, "close": worker.close, "release": worker.release}.get(action)
        if fn is None:
            raise HTTPException(status_code=404, detail="Acción desconocida")
        if await request.app.state.repo.get_conversation(wa_id) is None:
            raise HTTPException(status_code=404, detail="Conversación no encontrada")
        await _act(fn, wa_id, user)
        conv = await request.app.state.repo.get_conversation(wa_id)
        return {"state": conv.state}

    # --- Revisión y reglas -----------------------------------------------------------------

    @router.post("/api/admin/messages/{message_id}/flag")
    async def flag(request: Request, message_id: int, body: FlagBody,
                   user: User = Depends(require("reviews.flag"))) -> dict[str, Any]:
        repo = request.app.state.repo
        msg = await repo.message(message_id)
        if msg is None or msg.role != "bot":
            raise HTTPException(status_code=400, detail="Solo se pueden marcar respuestas del bot.")
        review = await repo.flag_message(message_id, user.email, body.reason, body.note)
        await request.app.state.store.incr_metrics({"flagged": 1})
        await repo.audit(user.email, "review.flagged", str(message_id), {"reason": body.reason})
        return {"review_id": review.id}

    @router.get("/api/admin/reviews")
    async def reviews(request: Request, status: str = "abierto",
                      user: User = Depends(require("reviews.manage"))) -> dict[str, Any]:
        repo = request.app.state.repo
        return {"items": await repo.list_reviews(status or None), "areas": ["todas", *AREAS]}

    @router.post("/api/admin/reviews/{review_id}/suggest")
    async def suggest(request: Request, review_id: int,
                      user: User = Depends(require("reviews.manage"))) -> dict[str, str]:
        repo = request.app.state.repo
        item = next((r for r in await repo.list_reviews(None) if r["id"] == review_id), None)
        if item is None:
            raise HTTPException(status_code=404, detail="Reporte no encontrado")
        text = await request.app.state.suggester.suggest(item["customer_text"], item["bot_text"],
                                                         item["reason"], item["note"])
        return {"text": text, "area": item["area"] or "todas"}

    @router.post("/api/admin/reviews/{review_id}/rule")
    async def review_rule(request: Request, review_id: int, body: RuleBody,
                          user: User = Depends(require("reviews.manage"))) -> dict[str, Any]:
        repo = request.app.state.repo
        if await repo.review(review_id) is None:
            raise HTTPException(status_code=404, detail="Reporte no encontrado")
        if body.area not in ("todas", *AREAS):
            raise HTTPException(status_code=400, detail="Área desconocida")
        rule = await repo.create_rule(body.area, body.text.strip(), user.email)
        await repo.update_review(review_id, status="resuelto", rule_id=rule.id)
        await repo.audit(user.email, "rule.created", str(rule.id), {"area": body.area, "review": review_id})
        return {"rule_id": rule.id}

    @router.post("/api/admin/reviews/{review_id}/dismiss")
    async def dismiss(request: Request, review_id: int,
                      user: User = Depends(require("reviews.manage"))) -> dict[str, str]:
        await request.app.state.repo.update_review(review_id, status="descartado")
        await request.app.state.repo.audit(user.email, "review.dismissed", str(review_id))
        return {"status": "descartado"}

    @router.get("/api/admin/rules")
    async def rules(request: Request, user: User = Depends(require("reviews.manage"))) -> dict[str, Any]:
        items = await request.app.state.repo.list_rules()
        return {"items": [{"id": r.id, "area": r.area, "text": r.text, "active": r.active,
                           "created_by": r.created_by, "created_at": r.created_at} for r in items],
                "areas": ["todas", *AREAS]}

    @router.post("/api/admin/rules")
    async def create_rule(request: Request, body: RuleBody,
                          user: User = Depends(require("reviews.manage"))) -> dict[str, Any]:
        if body.area not in ("todas", *AREAS):
            raise HTTPException(status_code=400, detail="Área desconocida")
        rule = await request.app.state.repo.create_rule(body.area, body.text.strip(), user.email)
        await request.app.state.repo.audit(user.email, "rule.created", str(rule.id), {"area": body.area})
        return {"rule_id": rule.id}

    @router.patch("/api/admin/rules/{rule_id}")
    async def update_rule(request: Request, rule_id: int, body: RuleUpdate,
                          user: User = Depends(require("reviews.manage"))) -> dict[str, str]:
        values = body.model_dump(exclude_none=True)
        if "area" in values and values["area"] not in ("todas", *AREAS):
            raise HTTPException(status_code=400, detail="Área desconocida")
        await request.app.state.repo.set_rule(rule_id, **values)
        await request.app.state.repo.audit(user.email, "rule.updated", str(rule_id), values)
        return {"status": "ok"}

    # --- Centro de control ---------------------------------------------------------------------

    @router.get("/api/admin/control")
    async def get_control(request: Request, user: User = Depends(require("conversations.view"))) -> dict[str, Any]:
        cc = await request.app.state.repo.control(max_age_s=0)
        return {"control": cc.model_dump(), "areas": list(AREAS), "open_now": cc.is_open()}

    @router.put("/api/admin/control")
    async def put_control(request: Request, user: User = Depends(require("control.manage"))) -> dict[str, str]:
        repo = request.app.state.repo
        try:
            cc = ControlCenter.model_validate(await request.json())
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(x) for x in first.get("loc", ()))
            raise HTTPException(status_code=400, detail=f"Valor no válido en {where}: {first.get('msg')}") from exc
        before = (await repo.control(max_age_s=0)).model_dump()
        after = cc.model_dump()
        changed = sorted(k for k in after if after[k] != before.get(k))
        await repo.set_control(cc, user.email)
        await repo.audit(user.email, "control.updated", "", {"changed": changed})
        return {"status": "ok"}

    @router.post("/api/admin/control/bot")
    async def bot_switch(request: Request, body: BotSwitch,
                         user: User = Depends(require("control.manage"))) -> dict[str, bool]:
        repo = request.app.state.repo
        cc = (await repo.control(max_age_s=0)).model_copy(update={"bot_enabled": body.enabled})
        await repo.set_control(cc, user.email)
        await repo.audit(user.email, "control.bot_switch", "", {"enabled": body.enabled})
        return {"bot_enabled": body.enabled}

    # --- métricas y auditoría -------------------------------------------------------------------

    @router.get("/api/admin/metrics")
    async def metrics(request: Request, days: int = 14,
                      user: User = Depends(require("metrics.view"))) -> dict[str, Any]:
        days = max(1, min(days, 90))
        repo = request.app.state.repo
        n, cents = await repo.payments_since(time.time() - days * 86400)
        stale = await repo.stale_pending(30 * 60)
        return {"days": await request.app.state.store.metrics(days),
                "payments": {"count": n, "usd": round(cents / 100, 2)},
                "pending_over_30min": len(stale)}

    @router.get("/api/admin/audit")
    async def audit_log(request: Request, limit: int = 200,
                        user: User = Depends(require("audit.view"))) -> dict[str, Any]:
        return {"items": await request.app.state.repo.list_audit(min(limit, 1000))}

    # --- usuarios ------------------------------------------------------------------------------

    @router.get("/api/admin/users")
    async def users(request: Request, user: User = Depends(require("users.manage"))) -> dict[str, Any]:
        return {"items": [_public(u) for u in await request.app.state.repo.list_users()]}

    @router.post("/api/admin/users")
    async def create_user(request: Request, body: UserCreate,
                          user: User = Depends(require("users.manage"))) -> dict[str, Any]:
        repo = request.app.state.repo
        if problem := auth.validate_password(body.password):
            raise HTTPException(status_code=400, detail=problem)
        if await repo.user_by_email(body.email):
            raise HTTPException(status_code=400, detail="Ya existe un usuario con ese correo.")
        new = await repo.create_user(body.email, body.name, body.role, auth.hash_password(body.password))
        await repo.audit(user.email, "user.created", new.email, {"role": body.role})
        return _public(new)

    @router.patch("/api/admin/users/{user_id}")
    async def update_user(request: Request, user_id: int, body: UserUpdate,
                          user: User = Depends(require("users.manage"))) -> dict[str, Any]:
        repo = request.app.state.repo
        target = await repo.user_by_id(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        values = body.model_dump(exclude_none=True)
        losing_owner = target.role == "owner" and (values.get("role", "owner") != "owner" or values.get("active") is False)
        if losing_owner and sum(1 for u in await repo.list_users() if u.role == "owner" and u.active) <= 1:
            raise HTTPException(status_code=400, detail="Debe quedar al menos un owner activo.")
        if "password" in values:
            if problem := auth.validate_password(values["password"]):
                raise HTTPException(status_code=400, detail=problem)
            values["password_hash"] = auth.hash_password(values.pop("password"))
        await repo.update_user(user_id, **values)
        await repo.audit(user.email, "user.updated", target.email,
                         {k: v for k, v in values.items() if k != "password_hash"})
        return _public(await repo.user_by_id(user_id))

    return router
