"""T3：超时与重试。

验证单次超时有界、阶段 deadline 生效、只读工具指数退避重试，
写工具与不可重试错误只调用一次，取消原样传播。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tooling.errors import ToolError, ToolErrorCode
from tooling.tool_manager import ToolContext, ToolManager, ToolSpec

EMPTY_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def make_context() -> ToolContext:
    return ToolContext(user_id="u1", session_id="s1", request_id="r1")


def make_spec(handler, **overrides) -> ToolSpec:
    values = {
        "name": "lookup_order",
        "description": "查询订单；只读。",
        "input_schema": EMPTY_SCHEMA,
        "handler": handler,
    }
    values.update(overrides)
    return ToolSpec(**values)


def stats_of(manager: ToolManager) -> dict:
    return manager.get_stats()["lookup_order"]


@pytest.mark.asyncio
async def test_timeout_returns_tool_timeout_within_budget():
    async def handler(params, context):
        await asyncio.sleep(0.2)

    manager = ToolManager()
    manager.register(make_spec(handler, timeout_s=0.01, max_retries=0))

    started = time.monotonic()
    result = await manager.execute("lookup_order", {}, make_context())
    elapsed = time.monotonic() - started

    assert result.success is False
    assert result.error_code == ToolErrorCode.TOOL_TIMEOUT.value
    assert result.attempts == 1
    assert result.retryable is True
    assert elapsed < 0.15

    stats = stats_of(manager)
    assert stats["total"] == 1
    assert stats["attempts"] == 1
    assert stats["timeout"] == 1
    assert stats["failed"] == 1
    assert stats["success_rate"] == 0.0


@pytest.mark.asyncio
async def test_first_timeout_then_success():
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(0.2)
        return {"ok": True}

    manager = ToolManager()
    manager.register(
        make_spec(handler, timeout_s=0.01, max_retries=1, retry_backoff_s=0.0)
    )

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is True
    assert result.data == {"ok": True}
    assert result.attempts == 2
    assert calls == 2

    stats = stats_of(manager)
    assert stats["total"] == 1
    assert stats["attempts"] == 2
    assert stats["timeout"] == 1
    assert stats["success"] == 1
    assert stats["failed"] == 0
    assert stats["retries"] == 1


@pytest.mark.asyncio
async def test_non_retryable_tool_error_called_once():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        raise ToolError("工具不允许", code=ToolErrorCode.TOOL_NOT_ALLOWED)

    manager = ToolManager()
    manager.register(make_spec(handler, max_retries=3))

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is False
    assert result.error_code == ToolErrorCode.TOOL_NOT_ALLOWED.value
    assert result.retryable is False
    assert result.attempts == 1
    assert calls == 1
    assert stats_of(manager)["retries"] == 0


@pytest.mark.asyncio
async def test_retryable_tool_error_is_retried():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ToolError("依赖暂时不可用", retryable=True)
        return {"ok": True}

    manager = ToolManager()
    manager.register(make_spec(handler, max_retries=1, retry_backoff_s=0.0))

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is True
    assert result.attempts == 2
    assert calls == 2


@pytest.mark.asyncio
async def test_plain_exception_is_retried_then_succeeds():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    manager = ToolManager()
    manager.register(make_spec(handler, max_retries=1, retry_backoff_s=0.0))

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is True
    assert result.attempts == 2
    assert stats_of(manager)["retries"] == 1


@pytest.mark.asyncio
async def test_retry_safe_false_never_retries():
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)

    manager = ToolManager()
    manager.register(
        make_spec(
            handler,
            timeout_s=0.01,
            max_retries=3,
            retry_safe=False,
        )
    )

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is False
    assert result.error_code == ToolErrorCode.TOOL_TIMEOUT.value
    assert result.attempts == 1
    assert result.retryable is False
    assert calls == 1
    assert stats_of(manager)["retries"] == 0


@pytest.mark.asyncio
async def test_attempts_are_capped_by_max_retries():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        raise RuntimeError("always down")

    manager = ToolManager()
    manager.register(make_spec(handler, max_retries=2, retry_backoff_s=0.0))

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is False
    assert result.error_code == ToolErrorCode.TOOL_HANDLER_ERROR.value
    assert result.attempts == 3
    assert calls == 3

    stats = stats_of(manager)
    assert stats["total"] == 1
    assert stats["attempts"] == 3
    assert stats["failed"] == 1
    assert stats["consecutive_fails"] == 1
    assert stats["retries"] == 2


@pytest.mark.asyncio
async def test_repeated_timeouts_count_each_attempt():
    async def handler(params, context):
        await asyncio.sleep(0.2)

    manager = ToolManager()
    manager.register(
        make_spec(handler, timeout_s=0.01, max_retries=1, retry_backoff_s=0.0)
    )

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.error_code == ToolErrorCode.TOOL_TIMEOUT.value
    assert result.attempts == 2

    stats = stats_of(manager)
    assert stats["timeout"] == 2
    assert stats["attempts"] == 2
    assert stats["failed"] == 1
    assert stats["retries"] == 1


@pytest.mark.asyncio
async def test_deadline_exhausted_before_first_attempt():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        return {"ok": True}

    manager = ToolManager(clock=lambda: 100.0)
    manager.register(make_spec(handler))

    result = await manager.execute(
        "lookup_order",
        {},
        make_context(),
        deadline=100.0,
    )

    assert result.success is False
    assert result.error_code == ToolErrorCode.TOOL_DEADLINE_EXCEEDED.value
    assert result.attempts == 0
    assert result.retryable is False
    assert calls == 0

    stats = stats_of(manager)
    assert stats["total"] == 1
    assert stats["attempts"] == 0
    assert stats["failed"] == 1
    assert stats["timeout"] == 0


@pytest.mark.asyncio
async def test_deadline_prevents_second_attempt_after_timeout():
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.2)

    manager = ToolManager()
    manager.register(
        make_spec(handler, timeout_s=0.01, max_retries=5, retry_backoff_s=0.0)
    )
    deadline = time.monotonic() + 0.002

    result = await manager.execute(
        "lookup_order",
        {},
        make_context(),
        deadline=deadline,
    )

    assert result.success is False
    assert result.error_code == ToolErrorCode.TOOL_TIMEOUT.value
    assert result.attempts == 1
    assert calls == 1


@pytest.mark.asyncio
async def test_backoff_is_exponential_and_uses_injected_sleep():
    delays: list[float] = []
    calls = 0

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(params, context):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError("boom")
        return {"ok": True}

    manager = ToolManager(sleep=fake_sleep)
    manager.register(make_spec(handler, max_retries=2, retry_backoff_s=0.5))

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is True
    assert result.attempts == 3
    assert len(delays) == 2
    assert 0.5 <= delays[0] <= 0.55
    assert 1.0 <= delays[1] <= 1.1
    assert stats_of(manager)["retries"] == 2


@pytest.mark.asyncio
async def test_backoff_is_capped_by_remaining_deadline():
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    def handler(params, context):
        raise RuntimeError("always down")

    manager = ToolManager(clock=lambda: 100.0, sleep=fake_sleep)
    manager.register(make_spec(handler, max_retries=1, retry_backoff_s=5.0))

    result = await manager.execute(
        "lookup_order",
        {},
        make_context(),
        deadline=100.2,
    )

    assert result.success is False
    assert result.attempts == 2
    assert delays == [pytest.approx(0.2)]
    assert stats_of(manager)["retries"] == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_without_counting_failure():
    async def handler(params, context):
        raise asyncio.CancelledError()

    manager = ToolManager()
    manager.register(make_spec(handler, max_retries=3))

    with pytest.raises(asyncio.CancelledError):
        await manager.execute("lookup_order", {}, make_context())

    stats = stats_of(manager)
    assert stats["attempts"] == 1
    assert stats["failed"] == 0
    assert stats["retries"] == 0


@pytest.mark.asyncio
async def test_sync_handler_timeout_does_not_block_event_loop():
    def handler(params, context):
        time.sleep(0.05)
        return {"ok": True}

    manager = ToolManager()
    manager.register(make_spec(handler, timeout_s=0.01, max_retries=0))

    started = time.monotonic()
    result = await manager.execute("lookup_order", {}, make_context())
    elapsed = time.monotonic() - started

    assert result.error_code == ToolErrorCode.TOOL_TIMEOUT.value
    assert result.attempts == 1
    assert elapsed < 0.05


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    ToolErrorCode.INVALID_ARGUMENTS,
    ToolErrorCode.TOOL_NOT_FOUND,
    ToolErrorCode.TOOL_CIRCUIT_OPEN,
    ToolErrorCode.TOOL_DEADLINE_EXCEEDED,
])
async def test_non_dependency_errors_never_retry_even_when_flagged(code):
    async def handler(params, context):
        raise ToolError("不可重试", code=code, retryable=True)

    manager = ToolManager()
    manager.register(make_spec(handler, max_retries=3, retry_backoff_s=0))
    result = await manager.execute("lookup_order", {}, make_context())
    assert result.attempts == 1
    assert result.retryable is False


@pytest.mark.asyncio
async def test_deadline_bounds_backoff_even_if_sleep_overruns():
    async def handler(params, context):
        raise RuntimeError("down")

    async def slow_sleep(delay):
        await asyncio.sleep(10)

    manager = ToolManager(sleep=slow_sleep)
    manager.register(make_spec(handler, max_retries=5, retry_backoff_s=1))
    started = time.monotonic()
    result = await manager.execute(
        "lookup_order", {}, make_context(), deadline=started + 0.03,
    )
    assert time.monotonic() - started < 0.3
    assert result.attempts == 1
    assert stats_of(manager)["retries"] == 0


@pytest.mark.asyncio
async def test_external_cancellation_during_backoff_propagates():
    sleeping = asyncio.Event()

    async def handler(params, context):
        raise RuntimeError("down")

    async def sleep(delay):
        sleeping.set()
        await asyncio.Event().wait()

    manager = ToolManager(sleep=sleep)
    manager.register(make_spec(handler, max_retries=3))
    task = asyncio.create_task(manager.execute("lookup_order", {}, make_context()))
    await asyncio.wait_for(sleeping.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stats_of(manager)["failed"] == 0
    assert stats_of(manager)["retries"] == 0


@pytest.mark.asyncio
async def test_cancelled_second_attempt_preserves_retry_count():
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("down")
        raise asyncio.CancelledError()

    manager = ToolManager()
    manager.register(make_spec(handler, retry_backoff_s=0))
    with pytest.raises(asyncio.CancelledError):
        await manager.execute("lookup_order", {}, make_context())
    assert stats_of(manager)["attempts"] == 2
    assert stats_of(manager)["retries"] == 1
    assert stats_of(manager)["failed"] == 0


@pytest.mark.asyncio
async def test_retries_receive_fresh_arguments():
    seen = []

    async def handler(params, context):
        seen.append(dict(params))
        if len(seen) == 1:
            params["injected"] = True
            raise RuntimeError("down")
        return params

    manager = ToolManager()
    manager.register(make_spec(handler, retry_backoff_s=0))
    result = await manager.execute("lookup_order", {}, make_context())
    assert result.success
    assert seen == [{}, {}]


@pytest.mark.parametrize("field", ["timeout_s", "retry_backoff_s", "cache_ttl_s"])
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_durations_are_rejected(field, value):
    with pytest.raises(ValueError):
        make_spec(lambda p, c: None, **{field: value})
