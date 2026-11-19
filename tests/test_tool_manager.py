"""T1：工具契约与注册表。

只验证注册表、定义输出、基础执行与熔断状态机；
参数校验、超时重试、缓存、降级和重排由 T2–T6 接入。
"""
from __future__ import annotations

import asyncio
import copy

import pytest

from tooling.errors import ToolErrorCode
from tooling.tool_manager import (
    CircuitBreaker,
    CircuitState,
    ToolContext,
    ToolManager,
    ToolSpec,
    ToolStats,
)


def make_schema(**properties) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


def make_context(**overrides) -> ToolContext:
    values = {
        "user_id": "u1",
        "session_id": "s1",
        "request_id": "r1",
        "intent": "order_status",
        "entities": {"order_id": ["A1001"]},
    }
    values.update(overrides)
    return ToolContext(**values)


def make_spec(name: str = "lookup_order", handler=None, **overrides) -> ToolSpec:
    if handler is None:
        handler = lambda params, context: {"order_id": params.get("order_id")}  # noqa: E731
    values = {
        "name": name,
        "description": "按订单号查询订单状态；只读，演示数据。",
        "input_schema": make_schema(order_id={"type": "string"}),
        "handler": handler,
    }
    values.update(overrides)
    return ToolSpec(**values)


# ── 契约校验 ──────────────────────────────────────────────────────────────────

def test_tool_spec_defaults():
    spec = make_spec()

    assert spec.timeout_s == 3.0
    assert spec.max_retries == 1
    assert spec.retry_backoff_s == 0.1
    assert spec.cache_ttl_s == 30.0
    assert spec.cache_scope == ("user_id",)
    assert spec.retry_safe is True
    assert spec.supports_rerank is False
    assert spec.rerank_query_field == "query"
    assert spec.fallback is None


def test_tool_spec_normalizes_schema_and_copies_input():
    schema = {"type": "object", "properties": {"order_id": {"type": "string"}}}
    spec = ToolSpec(
        name="lookup_order",
        description="查询订单",
        input_schema=schema,
        handler=lambda params, context: params,
    )

    assert spec.input_schema["required"] == []
    assert spec.input_schema["additionalProperties"] is False

    schema["properties"]["order_id"]["type"] = "integer"
    assert spec.input_schema["properties"]["order_id"]["type"] == "string"


@pytest.mark.parametrize("name", ["", "Lookup", "1lookup", "look-up", "lookup order", "a" * 65])
def test_tool_spec_rejects_invalid_name(name):
    with pytest.raises(ValueError):
        make_spec(name=name)


def test_tool_spec_rejects_empty_description():
    with pytest.raises(ValueError):
        make_spec(description="   ")


@pytest.mark.parametrize(
    "schema",
    [
        None,
        [],
        {"type": "array", "properties": {}},
        {"type": "object"},
        {"type": "object", "properties": []},
        {"type": "object", "properties": {}, "required": "order_id"},
        {"type": "object", "properties": {}, "required": ["order_id"]},
        {"type": "object", "properties": {}, "additionalProperties": True},
    ],
)
def test_tool_spec_rejects_invalid_schema(schema):
    with pytest.raises((TypeError, ValueError)):
        make_spec(input_schema=schema)


def test_tool_spec_rejects_non_callable_handler():
    with pytest.raises(TypeError):
        make_spec(handler="not-callable")


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout_s": 0},
        {"timeout_s": -1},
        {"timeout_s": True},
        {"max_retries": -1},
        {"max_retries": 1.5},
        {"retry_backoff_s": -0.1},
        {"cache_ttl_s": -1},
        {"cache_scope": ("unknown",)},
        {"cache_scope": "user_id"},
        {"fallback": "not-callable"},
    ],
)
def test_tool_spec_rejects_invalid_execution_policy(overrides):
    with pytest.raises((TypeError, ValueError)):
        make_spec(**overrides)


def test_tool_context_strips_ids_and_copies_entities():
    entities = {"order_id": ["A1001"]}
    context = ToolContext(
        user_id=" u1 ",
        session_id=" s1 ",
        request_id=" r1 ",
        intent=" order_status ",
        entities=entities,
    )

    assert context.user_id == "u1"
    assert context.session_id == "s1"
    assert context.request_id == "r1"
    assert context.intent == "order_status"

    entities["order_id"].append("A1002")
    assert context.entities == {"order_id": ["A1001"]}


@pytest.mark.parametrize("field", ["user_id", "session_id", "request_id"])
def test_tool_context_rejects_empty_ids(field):
    with pytest.raises(ValueError):
        make_context(**{field: "  "})


def test_tool_context_rejects_blank_intent():
    with pytest.raises(ValueError):
        make_context(intent="   ")


def test_tool_context_rejects_non_mapping_entities():
    with pytest.raises(TypeError):
        make_context(entities=["order_id"])


# ── 注册表 ────────────────────────────────────────────────────────────────────

def test_register_lookup_and_names():
    manager = ToolManager()
    spec = make_spec()

    manager.register(spec)

    assert len(manager) == 1
    assert "lookup_order" in manager
    assert manager.names() == ("lookup_order",)
    assert manager.get_spec("lookup_order") is spec
    assert manager.get_spec("missing") is None


def test_register_duplicate_is_rejected():
    manager = ToolManager()
    manager.register(make_spec())

    with pytest.raises(ValueError, match="已注册"):
        manager.register(make_spec())


def test_register_rejects_non_spec():
    manager = ToolManager()

    with pytest.raises(TypeError):
        manager.register({"name": "lookup_order"})


def test_unregister_removes_tool_and_stats():
    manager = ToolManager()
    manager.register(make_spec())

    assert manager.unregister("lookup_order") is True
    assert manager.unregister("lookup_order") is False
    assert len(manager) == 0
    assert manager.get_stats() == {}


def test_definitions_shape_and_order():
    manager = ToolManager()
    manager.register(make_spec(name="lookup_order"))
    manager.register(make_spec(name="get_logistics"))

    definitions = manager.definitions()

    assert [item["name"] for item in definitions] == ["get_logistics", "lookup_order"]
    for item in definitions:
        assert set(item) == {"name", "description", "input_schema"}
        assert "handler" not in item
        assert item["input_schema"]["type"] == "object"


def test_definitions_returns_schema_copy():
    manager = ToolManager()
    manager.register(make_spec())

    definitions = manager.definitions()
    definitions[0]["input_schema"]["properties"]["order_id"]["type"] = "integer"

    assert manager.get_spec("lookup_order").input_schema["properties"]["order_id"]["type"] == "string"


def test_definitions_reflect_unregister():
    manager = ToolManager()
    manager.register(make_spec())

    manager.unregister("lookup_order")

    assert manager.definitions() == []


# ── 基础执行 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error_without_raising():
    manager = ToolManager()

    result = await manager.execute("missing_tool", {}, make_context())

    assert result.success is False
    assert result.usable is False
    assert result.error_code == ToolErrorCode.TOOL_NOT_FOUND.value
    assert result.attempts == 0
    assert manager.get_stats() == {}


@pytest.mark.asyncio
async def test_execute_sync_handler_success():
    manager = ToolManager()
    manager.register(make_spec())
    context = make_context()

    result = await manager.execute("lookup_order", {"order_id": "A1001"}, context)

    assert result.success is True
    assert result.usable is True
    assert result.data == {"order_id": "A1001"}
    assert result.attempts == 1
    assert result.latency_ms >= 0
    assert result.error_code is None

    stats = manager.get_stats()["lookup_order"]
    assert stats["total"] == 1
    assert stats["attempts"] == 1
    assert stats["success"] == 1
    assert stats["failed"] == 0
    assert stats["consecutive_fails"] == 0
    assert stats["success_rate"] == 1.0


@pytest.mark.asyncio
async def test_execute_async_handler_success():
    async def handler(params, context):
        await asyncio.sleep(0)
        return {"order_id": params["order_id"], "user_id": context.user_id}

    manager = ToolManager()
    manager.register(make_spec(handler=handler))

    result = await manager.execute("lookup_order", {"order_id": "A1001"}, make_context())

    assert result.success is True
    assert result.data == {"order_id": "A1001", "user_id": "u1"}


@pytest.mark.asyncio
async def test_execute_coroutine_returned_from_sync_handler():
    async def inner(params, context):
        return {"ok": True}

    manager = ToolManager()
    manager.register(make_spec(handler=lambda params, context: inner(params, context)))

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is True
    assert result.data == {"ok": True}


@pytest.mark.asyncio
async def test_execute_handler_error_is_returned_not_raised():
    def handler(params, context):
        raise RuntimeError("demo gateway down")

    manager = ToolManager()
    manager.register(make_spec(handler=handler))

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is False
    assert result.usable is False
    assert result.error_code == ToolErrorCode.TOOL_HANDLER_ERROR.value
    assert result.error == "demo gateway down"
    assert result.retryable is True
    assert result.attempts == 1

    stats = manager.get_stats()["lookup_order"]
    assert stats["failed"] == 1
    assert stats["success"] == 0
    assert stats["consecutive_fails"] == 1
    assert stats["success_rate"] == 0.0


@pytest.mark.asyncio
async def test_execute_failure_then_success_resets_consecutive_fails():
    def handler(params, context):
        if not params.get("ok"):
            raise RuntimeError("boom")
        return {"ok": True}

    manager = ToolManager()
    manager.register(make_spec(
        input_schema=make_schema(order_id={"type": "string"}, ok={"type": "boolean"}),
        handler=handler,
    ))

    await manager.execute("lookup_order", {"ok": False}, make_context())
    result = await manager.execute("lookup_order", {"ok": True}, make_context())

    assert result.success is True
    stats = manager.get_stats()["lookup_order"]
    assert stats["total"] == 2
    assert stats["failed"] == 1
    assert stats["success"] == 1
    assert stats["consecutive_fails"] == 0
    assert stats["success_rate"] == 0.5


@pytest.mark.asyncio
async def test_execute_propagates_cancellation_without_marking_failure():
    async def handler(params, context):
        raise asyncio.CancelledError()

    manager = ToolManager()
    manager.register(make_spec(handler=handler))

    with pytest.raises(asyncio.CancelledError):
        await manager.execute("lookup_order", {}, make_context())

    stats = manager.get_stats()["lookup_order"]
    assert stats["failed"] == 0
    assert stats["success"] == 0


def test_get_stats_uses_injected_clock():
    ticks = iter([10.0, 10.25])
    manager = ToolManager(clock=lambda: next(ticks))
    manager.register(make_spec())

    asyncio.run(manager.execute("lookup_order", {}, make_context()))
    stats = manager.get_stats()["lookup_order"]

    assert stats["avg_latency_ms"] == 250.0
    assert stats["circuit_state"] == "closed"


def test_get_stats_reports_registered_tool_without_calls():
    manager = ToolManager()
    manager.register(make_spec())

    stats = manager.get_stats()["lookup_order"]

    assert stats["total"] == 0
    assert stats["success_rate"] == 1.0
    assert stats["avg_latency_ms"] == 0.0


def test_tool_stats_properties():
    stats = ToolStats()

    assert stats.success_rate == 1.0
    assert stats.avg_latency_ms == 0.0

    stats.total = 2
    stats.success = 1
    stats.total_latency_ms = 10.0

    assert stats.success_rate == 0.5
    assert stats.avg_latency_ms == 5.0


# ── 熔断状态机 ────────────────────────────────────────────────────────────────

def test_breaker_opens_after_threshold_and_rejects_calls():
    now = [100.0]
    breaker = CircuitBreaker(failure_threshold=2, recovery_s=10.0, clock=lambda: now[0])

    assert breaker.allow() is True
    breaker.record_failure(breaker.generation)
    breaker.record_failure(breaker.generation)

    assert breaker.state is CircuitState.OPEN
    assert breaker.generation == 1
    assert breaker.allow() is False


def test_breaker_half_open_allows_single_probe_and_closes_on_success():
    now = [100.0]
    breaker = CircuitBreaker(failure_threshold=1, recovery_s=10.0, clock=lambda: now[0])
    breaker.record_failure(breaker.generation)

    now[0] = 109.0
    assert breaker.allow() is False

    now[0] = 110.0
    assert breaker.allow() is True
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow() is False  # 已有探测在途

    breaker.record_success(breaker.generation)
    assert breaker.state is CircuitState.CLOSED
    assert breaker.fail_count == 0


def test_breaker_probe_failure_reopens():
    now = [100.0]
    breaker = CircuitBreaker(failure_threshold=1, recovery_s=10.0, clock=lambda: now[0])
    breaker.record_failure(breaker.generation)

    now[0] = 110.0
    assert breaker.allow() is True
    opened_generation = breaker.generation

    breaker.record_failure(opened_generation)

    assert breaker.state is CircuitState.OPEN
    assert breaker.generation == opened_generation + 1
    assert breaker.allow() is False


def test_breaker_ignores_stale_generation_result():
    breaker = CircuitBreaker(failure_threshold=1, recovery_s=10.0, clock=lambda: 100.0)
    stale_generation = breaker.generation
    breaker.record_failure(stale_generation)

    breaker.record_success(stale_generation)

    assert breaker.state is CircuitState.OPEN


def test_breaker_rejects_invalid_configuration():
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=True)
    with pytest.raises(ValueError):
        CircuitBreaker(recovery_s=-1)


# ── fork ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fork_isolates_stats_registry_and_breaker():
    manager = ToolManager()
    spec = make_spec()
    manager.register(spec)
    await manager.execute("lookup_order", {}, make_context())

    forked = manager.fork()

    assert forked.names() == manager.names()
    assert forked.get_spec("lookup_order") is spec
    assert forked.get_stats()["lookup_order"]["total"] == 0

    forked.register(make_spec(name="get_logistics"))
    assert "get_logistics" in forked
    assert "get_logistics" not in manager

    parent_breaker = manager._tools["lookup_order"].breaker
    fork_breaker = forked._tools["lookup_order"].breaker
    assert fork_breaker is not parent_breaker

    fork_breaker.record_failure(fork_breaker.generation)
    assert fork_breaker.fail_count == 1
    assert parent_breaker.fail_count == 0


def test_fork_keeps_breaker_configuration():
    manager = ToolManager()
    manager.register(make_spec())
    manager._tools["lookup_order"].breaker = CircuitBreaker(
        failure_threshold=2,
        recovery_s=5.0,
        clock=manager._clock,
    )

    forked = manager.fork()

    breaker = forked._tools["lookup_order"].breaker
    assert breaker.threshold == 2
    assert breaker.recovery_s == 5.0
    assert breaker.state is CircuitState.CLOSED


def test_definitions_copy_does_not_leak_between_forks():
    manager = ToolManager()
    manager.register(make_spec())
    forked = manager.fork()

    forked.definitions()[0]["input_schema"]["properties"]["order_id"]["type"] = "integer"

    original = manager.definitions()[0]["input_schema"]["properties"]["order_id"]["type"]
    assert original == "string"
    assert copy.deepcopy(original) == "string"
