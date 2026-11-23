"""T5 执行入口的熔断与降级回归测试。"""

import asyncio

import pytest

from tooling import ToolContext, ToolError, ToolErrorCode, ToolManager, ToolSpec


def setup_tool(handler, *, fallback=None, threshold=1, **options):
    now = 100.0

    def advance(seconds):
        nonlocal now
        now += seconds

    manager = ToolManager(
        clock=lambda: now, circuit_failure_threshold=threshold, circuit_recovery_s=10,
    )
    manager.register(ToolSpec(
        "lookup", "查询", {"type": "object", "properties": {}}, handler,
        fallback=fallback, retry_backoff_s=0, **options,
    ))

    async def execute(params=None, **kwargs):
        return await manager.execute(
            "lookup", {} if params is None else params, ToolContext("u", "s", "r"), **kwargs,
        )

    return manager, execute, advance


@pytest.mark.asyncio
async def test_retries_count_once_and_fallback_does_not_reset_or_cache():
    failures = []

    def fallback(params, context, reason):
        failures.append(reason)
        return []  # 空列表也是可用的业务结果。

    async def handler(params, context):
        raise RuntimeError("private backend detail")

    manager, execute, _ = setup_tool(
        handler, fallback=fallback, threshold=2, cache_ttl_s=30, max_retries=2,
    )
    first = await execute()
    assert (first.success, first.usable, first.degraded, first.attempts) == (False, True, True, 3)
    assert first.data == []
    assert failures[0].code == ToolErrorCode.TOOL_HANDLER_ERROR.value
    assert "private backend detail" not in failures[0].message
    assert manager.get_stats()["lookup"]["circuit_state"] == "closed"

    assert (await execute()).attempts == 3
    rejected = await execute()
    assert rejected.error_code == ToolErrorCode.TOOL_CIRCUIT_OPEN.value
    assert rejected.attempts == 0 and rejected.degraded and not rejected.retryable
    stats = manager.get_stats()["lookup"]
    assert stats["consecutive_fails"] == 2
    assert stats["success"] == 0
    assert stats["total"] == stats["failed"] == stats["degraded"] == 3
    assert stats["circuit_rejections"] == 1
    assert manager.cache_size == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_only_one_half_open_probe_and_outcome_controls_recovery(outcome):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise TimeoutError()
        if calls == 4:
            started.set()
            await release.wait()
            if outcome == "failure":
                raise TimeoutError()
        return "fresh"

    manager, execute, advance = setup_tool(handler, max_retries=2)
    assert (await execute()).attempts == 3
    advance(10)
    probe = asyncio.create_task(execute())
    await asyncio.wait_for(started.wait(), timeout=1)
    rejected = await execute()
    assert rejected.attempts == 0
    assert rejected.error_code == ToolErrorCode.TOOL_CIRCUIT_OPEN.value
    if outcome == "cancel":
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe
        assert manager.get_stats()["lookup"]["consecutive_fails"] == 1
        assert (await execute()).success
    else:
        release.set()
        result = await probe
        assert result.attempts == 1
        assert result.success is (outcome == "success")
        assert calls == 4
    expected_state = "open" if outcome == "failure" else "closed"
    assert manager.get_stats()["lookup"]["circuit_state"] == expected_state


@pytest.mark.asyncio
async def test_non_dependency_failure_and_expired_budget_do_not_consume_probe():
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError()
        if calls == 2:
            raise ToolError("缺少业务信息", code=ToolErrorCode.INVALID_SEMANTIC)
        return "fresh"

    manager, execute, advance = setup_tool(handler, max_retries=0)
    await execute()
    advance(10)
    assert (await execute({"extra": True})).error_code == ToolErrorCode.INVALID_ARGUMENTS.value
    assert (await execute(deadline=110)).error_code == ToolErrorCode.TOOL_DEADLINE_EXCEEDED.value
    assert (await execute()).error_code == ToolErrorCode.INVALID_SEMANTIC.value
    assert manager.get_stats()["lookup"]["consecutive_fails"] == 1
    assert (await execute()).success


@pytest.mark.asyncio
async def test_old_success_cannot_close_circuit_or_populate_cache():
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(params, context):
        if started.is_set():
            raise RuntimeError()
        started.set()
        await release.wait()
        return "stale"

    manager, execute, _ = setup_tool(handler, cache_ttl_s=30, max_retries=0)
    old = asyncio.create_task(execute())
    await asyncio.wait_for(started.wait(), timeout=1)
    await execute()
    release.set()
    assert (await old).success
    assert manager.get_stats()["lookup"]["consecutive_fails"] == 1
    assert manager.cache_size == 0
    assert (await execute()).error_code == ToolErrorCode.TOOL_CIRCUIT_OPEN.value


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_mode", ["none", "raise", "timeout"])
async def test_unusable_fallback_preserves_primary_failure(fallback_mode):
    async def handler(params, context):
        raise RuntimeError()

    async def fallback(params, context, reason):
        if fallback_mode == "raise":
            raise ValueError()
        if fallback_mode == "timeout":
            await asyncio.Event().wait()

    _, execute, _ = setup_tool(handler, fallback=fallback, max_retries=0, timeout_s=0.02)
    result = await asyncio.wait_for(execute(), timeout=1)
    assert result.error_code == ToolErrorCode.TOOL_HANDLER_ERROR.value
    assert result.attempts == 1
    assert not (result.success or result.usable or result.degraded)
