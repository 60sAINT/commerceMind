import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from core.intent import IntentCategory, IntentResult, IntentSource
from memory.service import ConversationMemory
from memory.store import RedisMemoryStore


@pytest_asyncio.fixture
async def memory():
    client = FakeRedis(decode_responses=True)
    service = ConversationMemory(RedisMemoryStore(client, ttl_s=120))
    try:
        yield service, client
    finally:
        await client.aclose()


def result(intent, confidence, source):
    return IntentResult(
        intent=intent,
        confidence=confidence,
        source=source,
        source_scores={"llm": confidence},
        entities={"order_id": ["A-123"]},
        reasoning="当前轮识别结果",
    )


@pytest.mark.asyncio
async def test_persist_intent_updates_latest_and_preserves_other_state(memory):
    service, _ = memory
    await service.store.set_state("u", "s", {"stage": "refund_pending", "custom": 3})
    await service.persist_intent("u", "s", result(IntentCategory.REFUND, 0.91, IntentSource.LLM))
    first = await service.store.get_state("u", "s")
    assert first["stage"] == "refund_pending" and first["custom"] == 3
    assert first["intent"]["intent"] == "refund"
    assert first["intent"]["confidence"] == 0.91

    await service.persist_intent("u", "s", result(IntentCategory.LOGISTICS, 0.84, IntentSource.EMBEDDING))
    state = await service.store.get_state("u", "s")
    assert state["intent"]["intent"] == "logistics"
    assert state["intent"]["source"] == "embedding"


@pytest.mark.asyncio
async def test_intent_state_is_isolated_by_user_and_session(memory):
    service, _ = memory
    await service.persist_intent("u", "s1", result(IntentCategory.REFUND, 0.9, IntentSource.LLM))
    await service.persist_intent("u", "s2", result(IntentCategory.INVOICE, 0.8, IntentSource.PATTERN))
    await service.persist_intent("other", "s1", result(IntentCategory.GREETING, 0.7, IntentSource.EMBEDDING))
    assert (await service.store.get_state("u", "s1"))["intent"]["intent"] == "refund"
    assert (await service.store.get_state("u", "s2"))["intent"]["intent"] == "invoice"
    assert (await service.store.get_state("other", "s1"))["intent"]["intent"] == "greeting"


@pytest.mark.asyncio
async def test_intent_state_write_refreshes_ttl_and_expiration_clears_it(memory):
    service, client = memory
    key = service.store.state_key("u", "s")
    await service.persist_intent("u", "s", result(IntentCategory.REFUND, 0.9, IntentSource.LLM))
    assert 0 < await client.ttl(key) <= 120
    await client.expire(key, 5)
    await service.persist_intent("u", "s", result(IntentCategory.INVOICE, 0.8, IntentSource.PATTERN))
    assert await client.ttl(key) > 5
    await client.delete(key)
    assert await service.store.get_state("u", "s") == {}


@pytest.mark.asyncio
async def test_persist_intent_rejects_wrong_result_type(memory):
    service, _ = memory
    with pytest.raises(TypeError):
        await service.persist_intent("u", "s", {"intent": "refund"})
