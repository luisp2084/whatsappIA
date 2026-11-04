import time

import pytest

from app.outbox import MAX_ATTEMPTS, Outbox

pytestmark = pytest.mark.asyncio

WA = "593999000001"


async def test_same_turn_is_sent_once(repo, store, channel):
    ob = Outbox(repo, channel, store)
    assert await ob.enqueue(WA, "turn-1", 0, "text", {"text": "hola"}, "hola")
    assert await ob.enqueue(WA, "turn-1", 0, "text", {"text": "hola, otra redacción"}, "hola, otra") is None
    assert len(channel.sent) == 1
    view = await repo.conversation_view(WA)
    assert [m["role"] for m in view["messages"]] == ["bot"]


async def test_guardrail_blocks_recent_repeated_text(repo, store, channel):
    ob = Outbox(repo, channel, store)
    await ob.enqueue(WA, "t1", 0, "text", {"text": "Tu saldo es $50"}, "Tu saldo es $50")
    assert await ob.enqueue(WA, "t2", 0, "text", {"text": "Tu saldo es  $50"}, "Tu  saldo es $50") is None
    assert len(channel.sent) == 1
    msgs = (await repo.conversation_view(WA))["messages"]
    assert msgs[-1]["role"] == "system" and "bloqueada" in msgs[-1]["text"]
    assert (await store.metrics(1))[-1]["duplicate_blocked"] == 1
    # Un asesor sí puede repetir texto.
    assert await ob.enqueue(WA, "t3", 0, "text", {"text": "Tu saldo es $50"}, "Tu saldo es $50", role="agent")


async def test_failed_send_is_retried_and_then_marked_failed(repo, store, channel):
    ob = Outbox(repo, channel, store)
    channel.fail_next = 1
    key = await ob.enqueue(WA, "t1", 0, "text", {"text": "hola"}, "hola")
    item = await repo.outbox_get(key)
    assert item.status == "pending" and item.attempts == 1 and channel.sent == []
    await repo.outbox_update(key, next_attempt_at=time.time() - 1)
    assert await ob.flush() == 1 and channel.sent[0]["text"] == "hola"

    channel.fail_next = MAX_ATTEMPTS
    key = await ob.enqueue(WA, "t2", 0, "text", {"text": "chao"}, "chao")
    for _ in range(MAX_ATTEMPTS):
        await repo.outbox_update(key, next_attempt_at=time.time() - 1)
        await ob.flush()
    assert (await repo.outbox_get(key)).status == "failed"
    assert "No se pudo entregar" in (await repo.conversation_view(WA))["messages"][-1]["text"]
