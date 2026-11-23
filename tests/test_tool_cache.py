"""T4：TTL 缓存。

验证缓存命中、深拷贝隔离、TTL 过期、scope 隔离、主动失效不回填、
容量淘汰，以及缓存与熔断恢复的先后关系。
"""
from __future__ import annotations

import asyncio

import pytest

from tooling.tool_manager import (
    CircuitState,
    ToolContext,
    ToolManager,
    ToolSpec,
)

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "minLength": 8},
        "top_k": {"type": "integer", "minimum": 1},
    },
    "required": ["order_id"],
    "additionalProperties": False,
}


def make_context(user_id: str = "u1", session_id: str = "s1", request_id: str = "r1") -> ToolContext:
    return ToolContext(user_id=user_id, session_id=session_id, request_id=request_id)


def make_spec(handler=None, **overrides) -> ToolSpec:
    if handler is None:
        def handler(params, context):
            return {"order_id": params["order_id"], "status": "shipped"}

    values = {
        "name": "lookup_order",
        "description": "查询订单；只读。",
        "input_schema": ORDER_SCHEMA,
        "handler": handler,
        # ToolSpec 默认不缓存；这些用例统一显式开启 30 秒 TTL。
        "cache_ttl_s": 30.0,
    }
    values.update(overrides)
    return ToolSpec(**values)


def stats_of(manager: ToolManager) -> dict:
    return manager.get_stats()["lookup_order"]


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_second_call_hits_cache():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"], "status": "shipped"}

    manager = ToolManager()
    manager.register(make_spec(handler))

    first = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    second = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    assert first.cached is False
    assert second.cached is True
    assert second.data == first.data
    assert calls == 1

    stats = stats_of(manager)
    assert stats["total"] == 2
    assert stats["attempts"] == 1
    assert stats["success"] == 2
    assert stats["cache_hits"] == 1
    assert stats["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_cache_hit_counts_as_success_but_not_attempt():
    manager = ToolManager()
    manager.register(make_spec())

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    result = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    assert result.cached is True
    assert result.attempts == 0
    assert result.latency_ms == 0.0

    stats = stats_of(manager)
    assert stats["total"] == 2
    assert stats["success"] == 2
    assert stats["attempts"] == 1
    assert stats["cache_hits"] == 1


@pytest.mark.asyncio
async def test_cached_data_is_deep_copied_per_hit():
    payload = {"order_id": "A1001-001", "items": [{"sku": "SKU-1"}]}

    manager = ToolManager()
    manager.register(make_spec(handler=lambda params, context: payload))

    first = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    first.data["items"][0]["sku"] = "BROKEN"
    first.data["extra"] = True

    second = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert second.cached is True
    assert second.data == {"order_id": "A1001-001", "items": [{"sku": "SKU-1"}]}

    second.data["items"].append({"sku": "SKU-2"})
    third = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert third.data == {"order_id": "A1001-001", "items": [{"sku": "SKU-1"}]}


@pytest.mark.asyncio
async def test_ttl_expiry_re_executes_handler():
    clock = Clock()
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"], "call": calls}

    manager = ToolManager(clock=clock)
    manager.register(make_spec(handler, cache_ttl_s=10.0))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    clock.now += 9.9
    hit = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert hit.cached is True
    assert calls == 1

    clock.now += 0.1  # 正好到过期点
    refreshed = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert refreshed.cached is False
    assert refreshed.data["call"] == 2
    assert calls == 2

    stats = stats_of(manager)
    assert stats["cache_hits"] == 1
    assert stats["total"] == 3


@pytest.mark.asyncio
async def test_different_users_do_not_share_cache():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"], "user": context.user_id}

    manager = ToolManager()
    manager.register(make_spec(handler))

    first = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(user_id="u1"))
    second = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(user_id="u2"))

    assert first.cached is False
    assert second.cached is False
    assert second.data["user"] == "u2"
    assert calls == 2


@pytest.mark.asyncio
async def test_same_user_different_requests_share_cache():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"]}

    manager = ToolManager()
    manager.register(make_spec(handler))

    await manager.execute(
        "lookup_order",
        {"order_id": "A1001-001"},
        make_context(session_id="s1", request_id="r1"),
    )
    hit = await manager.execute(
        "lookup_order",
        {"order_id": "A1001-001"},
        make_context(session_id="s2", request_id="r2"),
    )

    assert hit.cached is True
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_scope_session_id_isolates_sessions():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"], "session": context.session_id}

    manager = ToolManager()
    manager.register(make_spec(handler, cache_scope=("session_id",)))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(session_id="s1"))
    other = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(session_id="s2"))
    same = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(session_id="s1"))

    assert other.cached is False
    assert other.data["session"] == "s2"
    assert same.cached is True
    assert calls == 2


@pytest.mark.asyncio
async def test_invalidate_cache_removes_entries():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"], "call": calls}

    manager = ToolManager()
    manager.register(make_spec(handler))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert manager.cache_size == 1

    removed = manager.invalidate_cache("lookup_order")
    assert removed == 1
    assert manager.cache_size == 0

    refreshed = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert refreshed.cached is False
    assert refreshed.data["call"] == 2
    assert calls == 2

    stats = stats_of(manager)
    assert stats["cache_hits"] == 1
    assert stats["total"] == 3


@pytest.mark.asyncio
async def test_invalidate_all_clears_every_tool():
    def handler(params, context):
        return {"order_id": params.get("order_id")}

    manager = ToolManager()
    manager.register(make_spec(handler))
    manager.register(
        make_spec(
            handler,
            name="lookup_shipment",
            input_schema={
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
        )
    )

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    await manager.execute("lookup_shipment", {"order_id": "A1001-001"}, make_context())
    assert manager.cache_size == 2

    assert manager.invalidate_cache() == 2
    assert manager.cache_size == 0
    # register() 时各 +1，invalidate_cache() 再各 +1。
    assert manager._cache_versions == {"lookup_order": 2, "lookup_shipment": 2}

    result = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert result.cached is False


@pytest.mark.asyncio
async def test_inflight_result_is_not_written_after_invalidate():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"order_id": params["order_id"]}

    manager = ToolManager()
    manager.register(make_spec(handler))

    task = asyncio.create_task(
        manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    )
    await started.wait()
    assert manager.invalidate_cache("lookup_order") == 0

    release.set()
    result = await task
    assert result.success is True
    assert result.cached is False
    assert manager.cache_size == 0

    again = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert again.cached is False
    assert calls == 2


@pytest.mark.asyncio
async def test_failures_are_not_cached():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        raise RuntimeError("gateway down")

    manager = ToolManager()
    manager.register(make_spec(handler, max_retries=0, retry_backoff_s=0.0))

    first = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    second = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    assert first.success is False
    assert second.success is False
    assert second.cached is False
    assert calls == 2
    assert manager.cache_size == 0


@pytest.mark.asyncio
async def test_timeouts_are_not_cached():
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)

    manager = ToolManager()
    manager.register(make_spec(handler, timeout_s=0.01, max_retries=0))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    assert calls == 2
    assert manager.cache_size == 0


@pytest.mark.asyncio
async def test_cache_ttl_zero_bypasses_cache():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"]}

    manager = ToolManager()
    manager.register(make_spec(handler, cache_ttl_s=0))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    second = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    assert second.cached is False
    assert calls == 2
    assert manager.cache_size == 0


@pytest.mark.asyncio
async def test_side_effecting_tool_ignores_positive_cache_ttl():
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        return calls

    manager = ToolManager()
    manager.register(make_spec(handler, retry_safe=False))
    first = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    second = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    assert first.data == 1
    assert second.data == 2
    assert second.cached is False
    assert manager.cache_size == 0


@pytest.mark.asyncio
async def test_use_cache_false_neither_reads_nor_writes():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"]}

    manager = ToolManager()
    manager.register(make_spec(handler))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(), use_cache=False)
    second = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(), use_cache=False)

    assert second.cached is False
    assert calls == 2
    assert manager.cache_size == 0


@pytest.mark.asyncio
async def test_capacity_evicts_oldest_quarter():
    calls: list[str] = []

    def handler(params, context):
        calls.append(params["order_id"])
        return {"order_id": params["order_id"]}

    manager = ToolManager(cache_max_entries=4)
    manager.register(make_spec(handler))

    for index in range(4):
        await manager.execute("lookup_order", {"order_id": f"A1001-00{index}"}, make_context())
    assert manager.cache_size == 4

    await manager.execute("lookup_order", {"order_id": "A1001-004"}, make_context())
    assert manager.cache_size == 4

    # 最旧的 A1001-000 被淘汰，需要重新执行；A1001-004 仍在缓存。
    before = len(calls)
    await manager.execute("lookup_order", {"order_id": "A1001-000"}, make_context())
    assert len(calls) == before + 1
    assert manager.cache_size == 4

    before = len(calls)
    await manager.execute("lookup_order", {"order_id": "A1001-004"}, make_context())
    assert len(calls) == before


@pytest.mark.asyncio
async def test_rerank_top_k_separates_cache_when_supported():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"], "top_k": params.get("top_k")}

    manager = ToolManager()
    manager.register(make_spec(handler, supports_rerank=True))

    await manager.execute("lookup_order", {"order_id": "A1001-001", "top_k": 3}, make_context(), rerank_top_k=3)
    other = await manager.execute(
        "lookup_order", {"order_id": "A1001-001", "top_k": 3}, make_context(), rerank_top_k=5,
    )
    same = await manager.execute(
        "lookup_order", {"order_id": "A1001-001", "top_k": 3}, make_context(), rerank_top_k=3,
    )

    assert other.cached is False
    assert same.cached is True
    assert calls == 2


@pytest.mark.asyncio
async def test_rerank_top_k_ignored_when_unsupported():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"]}

    manager = ToolManager()
    manager.register(make_spec(handler, supports_rerank=False))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(), rerank_top_k=3)
    hit = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context(), rerank_top_k=9)

    assert hit.cached is True
    assert calls == 1


@pytest.mark.asyncio
async def test_open_breaker_still_serves_valid_cache():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"]}

    manager = ToolManager()
    manager.register(make_spec(handler))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    registered = manager._tools["lookup_order"]
    registered.breaker.state = CircuitState.OPEN
    registered.breaker.opened_at = manager._clock()

    hit = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    assert hit.cached is True
    assert calls == 1


@pytest.mark.asyncio
async def test_cache_does_not_block_execution_when_recovery_is_due():
    clock = Clock()
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"], "call": calls}

    manager = ToolManager(clock=clock)
    manager.register(make_spec(handler, cache_ttl_s=600.0))
    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    registered = manager._tools["lookup_order"]
    breaker = registered.breaker
    for _ in range(breaker.threshold):
        breaker.record_failure()

    clock.now += 59
    hit = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert hit.cached is True
    assert calls == 1

    # 正好到达恢复时间，仍有效的缓存也让出执行机会。
    clock.now += 1
    refreshed = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert refreshed.cached is False
    assert refreshed.data["call"] == 2
    assert refreshed.attempts == 1
    assert stats_of(manager)["cache_hits"] == 1
    assert breaker.state is CircuitState.CLOSED
    hit = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert hit.cached is True
    assert hit.data == refreshed.data


@pytest.mark.asyncio
async def test_parameter_order_does_not_change_key():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    manager = ToolManager()
    manager.register(make_spec(handler))

    await manager.execute(
        "lookup_order", {"order_id": "A1001-001", "top_k": 2}, make_context(),
    )
    hit = await manager.execute(
        "lookup_order", {"top_k": 2, "order_id": "A1001-001"}, make_context(),
    )

    assert hit.cached is True
    assert calls == 1


@pytest.mark.asyncio
async def test_different_params_use_separate_entries():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"]}

    manager = ToolManager()
    manager.register(make_spec(handler))

    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    second = await manager.execute("lookup_order", {"order_id": "A1001-002"}, make_context())

    assert second.cached is False
    assert calls == 2
    assert manager.cache_size == 2


@pytest.mark.asyncio
async def test_cancellation_does_not_write_cache():
    async def handler(params, context):
        raise asyncio.CancelledError()

    manager = ToolManager()
    manager.register(make_spec(handler, max_retries=3))

    with pytest.raises(asyncio.CancelledError):
        await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    assert manager.cache_size == 0


@pytest.mark.asyncio
async def test_unregister_clears_cache():
    manager = ToolManager()
    manager.register(make_spec())
    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert manager.cache_size == 1

    assert manager.unregister("lookup_order") is True
    assert manager.cache_size == 0


@pytest.mark.asyncio
async def test_fork_starts_with_isolated_empty_cache():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"order_id": params["order_id"]}

    manager = ToolManager()
    manager.register(make_spec(handler))
    await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())

    forked = manager.fork()
    assert forked.cache_size == 0

    forked_result = await forked.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert forked_result.cached is False
    assert calls == 2

    parent_hit = await manager.execute("lookup_order", {"order_id": "A1001-001"}, make_context())
    assert parent_hit.cached is True
    assert calls == 2


def test_cache_max_entries_is_validated():
    with pytest.raises(ValueError):
        ToolManager(cache_max_entries=0)
    with pytest.raises(ValueError):
        ToolManager(cache_max_entries=1_000_001)
    with pytest.raises(ValueError):
        ToolManager(cache_max_entries=True)


def test_invalidate_cache_rejects_empty_name():
    manager = ToolManager()
    with pytest.raises(ValueError):
        manager.invalidate_cache("")
    assert manager.invalidate_cache("never_registered") == 0
