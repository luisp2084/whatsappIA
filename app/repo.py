"""Acceso a la base de datos: conversaciones, mensajes, outbox, pagos, usuarios, Revisión y auditoría."""

from __future__ import annotations

import json
import time
from typing import Any, Iterable

from sqlalchemy import delete, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from . import states
from .control import ControlCenter
from .db import (AuditLog, ChannelIdentity, Conversation, Database, Message, OutboxItem, Payment,
                 Review, Rule, Setting, User)

WINDOW_SECONDS = 24 * 3600  # ventana de servicio de WhatsApp
CONTROL_KEY = "control_center"


class DuplicatePayment(Exception):
    pass


class Repo:
    def __init__(self, db: Database):
        self.db = db
        self._control: ControlCenter | None = None
        self._control_at = 0.0

    # --- conversaciones y mensajes ------------------------------------------------

    async def get_conversation(self, wa_id: str) -> Conversation | None:
        async with self.db.session() as s:
            return await s.get(Conversation, wa_id)

    async def ensure_conversation(self, wa_id: str, name: str = "") -> Conversation:
        async with self.db.session() as s:
            conv = await s.get(Conversation, wa_id)
            if conv is None:
                conv = Conversation(wa_id=wa_id, name=name)
                s.add(conv)
                try:
                    await s.commit()
                except IntegrityError:  # la creó otro proceso a la vez
                    await s.rollback()
                    conv = await s.get(Conversation, wa_id)
            elif name and conv.name != name:
                conv.name = name
                await s.commit()
            return conv

    async def record(self, wa_id: str, role: str, text: str, *, name: str = "", author: str = "",
                     area: str = "", prompt_version: str = "", model: str = "", ts: float | None = None) -> int:
        ts = ts or time.time()
        await self.ensure_conversation(wa_id, name)
        async with self.db.session() as s:
            msg = Message(wa_id=wa_id, ts=ts, role=role, text=text, author=author, area=area,
                          prompt_version=prompt_version, model=model)
            s.add(msg)
            values: dict[str, Any] = {"last_ts": ts, "last_role": role, "last_text": text[:200]}
            if role == "user":
                values["last_user_ts"] = ts
            await s.execute(update(Conversation).where(Conversation.wa_id == wa_id).values(**values))
            await s.commit()
            return msg.id

    async def transition(self, wa_id: str, event: str, *, assigned_to: int | None = None,
                         area: str | None = None, priority: str | None = None,
                         summary: str | None = None) -> tuple[str, str]:
        """Aplica un evento de la máquina de estados. Devuelve (estado_anterior, estado_nuevo)."""
        await self.ensure_conversation(wa_id)
        async with self.db.session() as s:
            conv = await s.get(Conversation, wa_id, with_for_update=True)
            old = conv.state
            new = states.next_state(old, event)
            conv.state = new
            if new != old:
                conv.state_since = time.time()
            if new == states.ASSIGNED and assigned_to is not None:
                conv.assigned_to = assigned_to
            if new in (states.BOT, states.CLOSED, states.PENDING) and event != "take":
                conv.assigned_to = None
            if area is not None:
                conv.area = area
            if priority is not None:
                conv.priority = priority
            if summary is not None:
                conv.handoff_summary = summary
            await s.commit()
            return old, new

    async def update_conversation(self, wa_id: str, **values: Any) -> None:
        await self.ensure_conversation(wa_id)
        async with self.db.session() as s:
            await s.execute(update(Conversation).where(Conversation.wa_id == wa_id).values(**values))
            await s.commit()

    async def list_conversations(self, tab: str = "todas", user_id: int | None = None,
                                 limit: int = 200) -> list[dict[str, Any]]:
        wanted = states.TABS.get(tab, states.TABS["todas"])
        async with self.db.session() as s:
            q = select(Conversation, User.name).outerjoin(User, User.id == Conversation.assigned_to) \
                .where(Conversation.state.in_(wanted)).order_by(desc(Conversation.last_ts)).limit(limit)
            if tab == "mias":
                q = q.where(Conversation.assigned_to == user_id)
            rows = (await s.execute(q)).all()
        return [self._meta(c, assignee) for c, assignee in rows]

    async def tab_counts(self, user_id: int | None) -> dict[str, int]:
        async with self.db.session() as s:
            rows = dict((await s.execute(
                select(Conversation.state, func.count()).group_by(Conversation.state))).all())
            mine = (await s.execute(select(func.count()).where(
                Conversation.state == states.ASSIGNED, Conversation.assigned_to == user_id))).scalar_one()
        return {"pendientes": rows.get(states.PENDING, 0), "mias": mine,
                "todas": sum(rows.get(x, 0) for x in states.TABS["todas"]),
                "cerradas": rows.get(states.CLOSED, 0)}

    async def conversation_view(self, wa_id: str, limit: int = 300) -> dict[str, Any] | None:
        async with self.db.session() as s:
            row = (await s.execute(select(Conversation, User.name).outerjoin(
                User, User.id == Conversation.assigned_to).where(Conversation.wa_id == wa_id))).first()
            if row is None:
                return None
            msgs = (await s.execute(select(Message).where(Message.wa_id == wa_id)
                                    .order_by(desc(Message.ts), desc(Message.id)).limit(limit))).scalars().all()
        return {"meta": self._meta(*row), "messages": [
            {"id": m.id, "ts": m.ts, "role": m.role, "text": m.text, "author": m.author, "area": m.area,
             "prompt_version": m.prompt_version, "flagged": m.flagged} for m in reversed(msgs)]}

    def _meta(self, c: Conversation, assignee: str | None = None) -> dict[str, Any]:
        open_ = time.time() - c.last_user_ts < WINDOW_SECONDS
        return {"wa_id": c.wa_id, "name": c.name, "state": c.state, "area": c.area,
                "assigned_to": c.assigned_to, "assignee": assignee or "", "priority": c.priority,
                "handoff_summary": c.handoff_summary, "last_ts": c.last_ts, "last_role": c.last_role,
                "last_text": c.last_text, "state_since": c.state_since, "window_open": open_,
                "window_closes_at": c.last_user_ts + WINDOW_SECONDS if c.last_user_ts else None,
                "opted_out": c.opted_out}

    async def last_sent_texts(self, wa_id: str, n: int = 3, since: float = 0.0) -> list[str]:
        async with self.db.session() as s:
            rows = (await s.execute(select(OutboxItem.text).where(
                OutboxItem.wa_id == wa_id, OutboxItem.status.in_(("sent", "pending", "sending")),
                OutboxItem.created_at >= since)
                .order_by(desc(OutboxItem.created_at)).limit(n))).scalars().all()
        return list(rows)

    async def stale_pending(self, older_than_s: float) -> list[dict[str, Any]]:
        """Conversaciones esperando a un humano desde hace más de `older_than_s` (para alertas)."""
        cutoff = time.time() - older_than_s
        async with self.db.session() as s:
            rows = (await s.execute(select(Conversation).where(
                Conversation.state == states.PENDING, Conversation.state_since < cutoff))).scalars().all()
        return [self._meta(c) for c in rows]

    # --- outbox --------------------------------------------------------------------

    async def outbox_add(self, item: OutboxItem) -> bool:
        async with self.db.session() as s:
            s.add(item)
            try:
                await s.commit()
                return True
            except IntegrityError:
                await s.rollback()
                return False

    async def outbox_get(self, key: str) -> OutboxItem | None:
        async with self.db.session() as s:
            return await s.get(OutboxItem, key)

    async def outbox_due(self, limit: int = 50) -> list[OutboxItem]:
        async with self.db.session() as s:
            return list((await s.execute(select(OutboxItem).where(
                OutboxItem.status == "pending", OutboxItem.next_attempt_at <= time.time())
                .order_by(OutboxItem.created_at).limit(limit))).scalars().all())

    async def outbox_update(self, key: str, **values: Any) -> None:
        async with self.db.session() as s:
            await s.execute(update(OutboxItem).where(OutboxItem.key == key).values(**values))
            await s.commit()

    async def outbox_claim(self, key: str) -> bool:
        """Marca un envío como en curso; solo un proceso lo consigue."""
        async with self.db.session() as s:
            res = await s.execute(update(OutboxItem).where(
                OutboxItem.key == key, OutboxItem.status == "pending").values(status="sending"))
            await s.commit()
            return res.rowcount == 1

    # --- pagos -----------------------------------------------------------------------

    async def payment_by_image(self, sha256: str) -> Payment | None:
        async with self.db.session() as s:
            return (await s.execute(select(Payment).where(Payment.image_sha256 == sha256))).scalar_one_or_none()

    async def payment_by_receipt(self, bank: str, number: str) -> Payment | None:
        async with self.db.session() as s:
            return (await s.execute(select(Payment).where(
                Payment.bank == bank, Payment.receipt_number == number))).scalar_one_or_none()

    async def add_payment(self, **values: Any) -> Payment:
        async with self.db.session() as s:
            p = Payment(**values)
            s.add(p)
            try:
                await s.commit()
            except IntegrityError as exc:
                await s.rollback()
                raise DuplicatePayment(str(exc.orig)) from exc
            return p

    async def payments_since(self, ts: float) -> tuple[int, int]:
        async with self.db.session() as s:
            n, total = (await s.execute(select(func.count(), func.coalesce(func.sum(Payment.amount_cents), 0))
                                        .where(Payment.created_at >= ts))).one()
        return int(n), int(total)

    # --- identidades -------------------------------------------------------------------

    async def identity(self, wa_id: str) -> ChannelIdentity | None:
        async with self.db.session() as s:
            return await s.get(ChannelIdentity, wa_id)

    async def link_customer(self, wa_id: str, customer_id: str) -> None:
        async with self.db.session() as s:
            ident = await s.get(ChannelIdentity, wa_id)
            if ident is None:
                ident = ChannelIdentity(wa_id=wa_id, phone=wa_id)
                s.add(ident)
            ident.customer_id = customer_id
            ident.verified_at = time.time()
            await s.commit()

    # --- usuarios ----------------------------------------------------------------------

    async def user_by_email(self, email: str) -> User | None:
        async with self.db.session() as s:
            return (await s.execute(select(User).where(func.lower(User.email) == email.lower()))).scalar_one_or_none()

    async def user_by_id(self, user_id: int) -> User | None:
        async with self.db.session() as s:
            return await s.get(User, user_id)

    async def list_users(self) -> list[User]:
        async with self.db.session() as s:
            return list((await s.execute(select(User).order_by(User.created_at))).scalars().all())

    async def count_users(self) -> int:
        async with self.db.session() as s:
            return (await s.execute(select(func.count()).select_from(User))).scalar_one()

    async def create_user(self, email: str, name: str, role: str, password_hash: str) -> User:
        async with self.db.session() as s:
            u = User(email=email.lower(), name=name, role=role, password_hash=password_hash)
            s.add(u)
            await s.commit()
            return u

    async def update_user(self, user_id: int, **values: Any) -> None:
        async with self.db.session() as s:
            await s.execute(update(User).where(User.id == user_id).values(**values))
            await s.commit()

    # --- Revisión y reglas ----------------------------------------------------------------

    async def message(self, message_id: int) -> Message | None:
        async with self.db.session() as s:
            return await s.get(Message, message_id)

    async def flag_message(self, message_id: int, reporter: str, reason: str, note: str) -> Review:
        async with self.db.session() as s:
            msg = await s.get(Message, message_id)
            msg.flagged = True
            review = Review(message_id=message_id, wa_id=msg.wa_id, reported_by=reporter, reason=reason, note=note)
            s.add(review)
            await s.commit()
            return review

    async def list_reviews(self, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            q = select(Review, Message).join(Message, Message.id == Review.message_id) \
                .order_by(desc(Review.created_at)).limit(limit)
            if status:
                q = q.where(Review.status == status)
            rows = (await s.execute(q)).all()
            out = []
            for r, m in rows:
                prev = (await s.execute(select(Message.text).where(
                    Message.wa_id == m.wa_id, Message.role == "user", Message.ts <= m.ts)
                    .order_by(desc(Message.ts)).limit(1))).scalar_one_or_none()
                out.append({"id": r.id, "status": r.status, "reason": r.reason, "note": r.note,
                            "reported_by": r.reported_by, "created_at": r.created_at, "rule_id": r.rule_id,
                            "wa_id": m.wa_id, "message_id": m.id, "bot_text": m.text, "area": m.area,
                            "prompt_version": m.prompt_version, "customer_text": prev or ""})
        return out

    async def review(self, review_id: int) -> Review | None:
        async with self.db.session() as s:
            return await s.get(Review, review_id)

    async def update_review(self, review_id: int, **values: Any) -> None:
        async with self.db.session() as s:
            await s.execute(update(Review).where(Review.id == review_id).values(**values))
            await s.commit()

    async def create_rule(self, area: str, text: str, created_by: str) -> Rule:
        async with self.db.session() as s:
            rule = Rule(area=area, text=text, created_by=created_by)
            s.add(rule)
            await s.commit()
            return rule

    async def list_rules(self) -> list[Rule]:
        async with self.db.session() as s:
            return list((await s.execute(select(Rule).order_by(desc(Rule.created_at)))).scalars().all())

    async def set_rule(self, rule_id: int, **values: Any) -> None:
        async with self.db.session() as s:
            await s.execute(update(Rule).where(Rule.id == rule_id).values(**values))
            await s.commit()

    async def active_rules(self, area: str) -> list[str]:
        async with self.db.session() as s:
            rows = (await s.execute(select(Rule.text).where(
                Rule.active.is_(True), or_(Rule.area == area, Rule.area == "todas"))
                .order_by(Rule.created_at))).scalars().all()
        return list(rows)

    # --- auditoría ------------------------------------------------------------------------

    async def audit(self, actor: str, action: str, target: str = "", detail: dict[str, Any] | None = None) -> None:
        async with self.db.session() as s:
            s.add(AuditLog(actor=actor, action=action, target=target,
                           detail=json.dumps(detail or {}, ensure_ascii=False, default=str)))
            await s.commit()

    async def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        async with self.db.session() as s:
            rows = (await s.execute(select(AuditLog).order_by(desc(AuditLog.ts)).limit(limit))).scalars().all()
        return [{"ts": a.ts, "actor": a.actor, "action": a.action, "target": a.target,
                 "detail": json.loads(a.detail or "{}")} for a in rows]

    # --- Centro de control --------------------------------------------------------------------

    async def control(self, max_age_s: float = 5.0) -> ControlCenter:
        """Configuración vigente, con una caché corta para no leer la tabla en cada mensaje."""
        if self._control is not None and time.time() - self._control_at < max_age_s:
            return self._control
        async with self.db.session() as s:
            row = await s.get(Setting, CONTROL_KEY)
        self._control = ControlCenter.model_validate_json(row.value) if row else ControlCenter()
        self._control_at = time.time()
        return self._control

    async def set_control(self, cc: ControlCenter, by: str) -> None:
        async with self.db.session() as s:
            row = await s.get(Setting, CONTROL_KEY)
            if row is None:
                s.add(Setting(key=CONTROL_KEY, value=cc.model_dump_json(), updated_by=by))
            else:
                row.value, row.updated_by, row.updated_at = cc.model_dump_json(), by, time.time()
            await s.commit()
        self._control, self._control_at = cc, time.time()

    # --- derechos del titular -------------------------------------------------------------------

    async def delete_customer(self, wa_id: str) -> dict[str, int]:
        """Borra conversación, mensajes, outbox, reportes e identidad. Los pagos se conservan por
        obligación contable, pero se desvinculan del número."""
        counts: dict[str, int] = {}
        async with self.db.session() as s:
            for model, col in ((Review, Review.wa_id), (Message, Message.wa_id), (OutboxItem, OutboxItem.wa_id),
                               (ChannelIdentity, ChannelIdentity.wa_id), (Conversation, Conversation.wa_id)):
                res = await s.execute(delete(model).where(col == wa_id))
                counts[model.__tablename__] = res.rowcount
            res = await s.execute(update(Payment).where(Payment.wa_id == wa_id).values(wa_id=None))
            counts["payments_unlinked"] = res.rowcount
            await s.commit()
        return counts


def as_dicts(items: Iterable[Any], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{f: getattr(i, f) for f in fields} for i in items]
