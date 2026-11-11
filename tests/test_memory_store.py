"""Redis 短期记忆测试：使用 fakeredis，不依赖真实 Redis。"""
import asyncio

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from memory.locks import redis_lease
from memory.store import RedisMemoryStore


@pytest_asyncio.fixture
async def redis_client():
    client = FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def store(redis_client):
    return RedisMemoryStore(redis_client, ttl_s=120)


async def add_turn(store, index, **kwargs):
    return await store.commit_turn(
        user_id="user-1",
        session_id="session-1",
        request_id=f"req-{index}",
        user_message=f"user-{index}",
        assistant_message=f"assistant-{index}",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_turns_are_one_item_per_round_and_ordered(store, redis_client):
    for index in range(3):
        assert await add_turn(store, index) is not None

    key = store.working_key("user-1", "session-1")
    raw = await redis_client.lrange(key, 0, -1)
    # 一轮一个 item，不是 user/assistant 两条消息。
    assert len(raw) == 3
    assert raw[0].find('"user_message":"user-2"') > 0

    turns = await store.get_turns("user-1", "session-1")
    assert [turn.user_message for turn in turns] == ["user-0", "user-1", "user-2"]
    assert [turn.assistant_message for turn in turns] == [
        "assistant-0", "assistant-1", "assistant-2",
    ]
    assert turns[0].created_at.tzinfo is not None

    snapshot = await store.get_snapshot("user-1", "session-1")
    assert snapshot.to_messages()[0] == {"role": "user", "content": "user-0"}
    assert len(snapshot.to_messages()) == 6


@pytest.mark.asyncio
async def test_users_and_sessions_are_isolated(store):
    await add_turn(store, 0)
    await store.commit_turn(
        "user-2", "session-1",
        request_id="other", user_message="u", assistant_message="a",
    )
    assert store.working_key("user-1", "session-1") != store.working_key("user-2", "session-1")
    assert store.working_key("a:b", "c") != store.working_key("a", "b:c")
    assert len(await store.get_turns("user-1", "session-1")) == 1
    assert len(await store.get_turns("user-2", "session-1")) == 1


@pytest.mark.asyncio
async def test_ttl_is_applied_and_refreshed(store, redis_client):
    await add_turn(store, 0, state={"stage": "created"}, summary="s0")
    keys = [
        store.working_key("user-1", "session-1"),
        store.state_key("user-1", "session-1"),
        store.summary_key("user-1", "session-1"),
    ]
    for key in keys:
        assert 0 < await redis_client.ttl(key) <= 120

    for key in keys:
        await redis_client.expire(key, 5)
    await add_turn(store, 1)
    for key in keys:
        assert await redis_client.ttl(key) > 5


@pytest.mark.asyncio
async def test_concurrent_appends_keep_every_turn(store):
    await asyncio.gather(*[add_turn(store, index) for index in range(20)])
    turns = await store.get_turns("user-1", "session-1")
    assert len(turns) == 20
    assert {turn.request_id for turn in turns} == {f"req-{index}" for index in range(20)}


@pytest.mark.asyncio
async def test_failed_turn_writes_nothing(store, redis_client):
    result = await store.commit_turn(
        "user-1", "session-1",
        request_id="req-failed",
        user_message="订单查询",
        assistant_message="上游失败",
        succeeded=False,
        state={"stage": "failed"},
        summary="不应保存",
    )
    assert result is None
    assert await redis_client.exists(
        store.working_key("user-1", "session-1"),
        store.state_key("user-1", "session-1"),
        store.summary_key("user-1", "session-1"),
    ) == 0
    assert (await store.get_snapshot("user-1", "session-1")).turns == ()


@pytest.mark.asyncio
async def test_state_and_summary_are_committed_with_turn(store):
    await add_turn(store, 0, state={"stage": "created"}, summary="s0")
    snapshot = await store.get_snapshot("user-1", "session-1")
    assert snapshot.state == {"stage": "created"}
    assert snapshot.summary == "s0"

    # 不传 state/summary 时保留旧值。
    await add_turn(store, 1)
    snapshot = await store.get_snapshot("user-1", "session-1")
    assert snapshot.state == {"stage": "created"}
    assert snapshot.summary == "s0"

    await store.set_state("user-1", "session-1", {"stage": "updated"})
    await store.set_summary("user-1", "session-1", "s1")
    assert await store.get_state("user-1", "session-1") == {"stage": "updated"}
    assert await store.get_summary("user-1", "session-1") == "s1"


@pytest.mark.asyncio
async def test_serialization_error_writes_nothing(store, redis_client):
    with pytest.raises(TypeError):
        await add_turn(store, 0, state={"bad": object()})
    assert await redis_client.llen(store.working_key("user-1", "session-1")) == 0
    assert not await redis_client.exists(store.state_key("user-1", "session-1"))


@pytest.mark.asyncio
async def test_redis_lease_serializes_concurrent_sections(redis_client):
    events = []

    async def worker(name):
        async with redis_lease(redis_client, "lock:test", wait_s=5, lease_s=30):
            events.append(f"{name}:start")
            await asyncio.sleep(0.01)
            events.append(f"{name}:end")

    await asyncio.gather(worker("a"), worker("b"))
    assert events in (
        ["a:start", "a:end", "b:start", "b:end"],
        ["b:start", "b:end", "a:start", "a:end"],
    )


@pytest.mark.asyncio
async def test_receipt_is_written_with_turn_and_readable(store):
    turn = await store.commit_turn(
        "user-1", "session-1",
        request_id="req-1",
        user_message="同一个请求",
        assistant_message="只处理一次",
        client_request_id="client-1",
    )

    assert await store.get_receipt("user-1", "session-1", "client-1") == turn
    assert await store.get_receipt("user-1", "session-1", "other") is None


@pytest.mark.asyncio
async def test_user_receipt_points_to_original_session(store, redis_client):
    turn = await store.commit_turn(
        "user-1", "session-1",
        request_id="req-1",
        user_message="同一个请求",
        assistant_message="只处理一次",
        client_request_id="client-1",
    )

    assert await store.get_user_receipt("user-1", "client-1") == ("session-1", turn)
    assert await store.get_user_receipt("user-2", "client-1") is None
    key = store.user_receipt_key("user-1")
    assert 0 < await redis_client.ttl(key) <= 120


@pytest.mark.asyncio
async def test_failed_turn_does_not_write_receipt(store):
    await store.commit_turn(
        "user-1", "session-1",
        request_id="req-1",
        user_message="失败",
        assistant_message="不保存",
        succeeded=False,
        client_request_id="client-1",
    )

    assert await store.get_receipt("user-1", "session-1", "client-1") is None
    assert await store.get_user_receipt("user-1", "client-1") is None


@pytest.mark.asyncio
async def test_commit_inside_held_conversation_lock_does_not_deadlock(store):
    async with store.conversation("user-1", "session-1"):
        turn = await store.commit_turn(
            "user-1", "session-1",
            request_id="req-1",
            user_message="持锁提交",
            assistant_message="成功",
            lock_held=True,
        )

    assert turn is not None
    assert len(await store.get_turns("user-1", "session-1")) == 1
