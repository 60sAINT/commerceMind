"""T12：可观测性必须准确、有界、只读且不记录业务正文。"""
import asyncio
import json
import logging

import httpx
import pytest

from core.config import Settings, ToolSettings
from api.main import create_app
from monitor.tool_monitor import LATENCY_WINDOW, prometheus_metrics
from tooling import FunctionCallingLoop, ToolContext, ToolManager, ToolSpec
from test_function_calling import FakeLLM, answer, tool_turn
from core.llm import LLMError, ToolUse


CTX = ToolContext("private-user", "private-session", "req-safe")
SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


def register(manager, handler=None, **kwargs):
    manager.register(ToolSpec(
        "check", "核验", SCHEMA, handler or (lambda p, c: {"secret": "PRIVATE_RESULT"}),
        **kwargs,
    ))


@pytest.mark.asyncio
async def test_logical_counts_cache_validation_and_physical_retries_stay_distinct():
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("PRIVATE_EXCEPTION")
        return {"ok": True}

    manager = ToolManager()
    register(manager, handler, cache_ttl_s=30, max_retries=1, retry_backoff_s=0)
    await manager.execute("check", {"n": 1}, CTX)
    await manager.execute("check", {"n": 1}, CTX)
    await manager.execute("check", {"n": "bad"}, CTX)
    stats = manager.get_stats()["check"]
    assert (stats["total"], stats["success"], stats["failed"], stats["invalid"]) == (2, 2, 0, 1)
    assert (stats["attempts"], stats["retries"], stats["cache_hits"]) == (2, 1, 1)
    assert stats["cache_hit_rate"] == 0.5
    assert stats["errors"] == {"invalid_arguments": 1}
    assert stats["latency_samples"] == 3
    assert stats["in_flight"] == 0


@pytest.mark.asyncio
async def test_latency_window_is_bounded_and_uses_nearest_rank_p95():
    now = [0.0]
    manager = ToolManager(clock=lambda: now[0])

    async def handler(params, context):
        now[0] += params["n"] / 1000
        return True

    register(manager, handler)
    for n in range(1, 301):
        await manager.execute("check", {"n": n}, CTX)
    stats = manager.get_stats()["check"]
    assert stats["total"] == 300 and stats["latency_samples"] == LATENCY_WINDOW
    assert stats["p95_latency_ms"] == 288.0  # 最近 45..300 毫秒的第 244 项。
    assert stats["avg_latency_ms"] == 150.5


@pytest.mark.asyncio
async def test_cancelled_call_is_observed_and_no_longer_in_flight():
    started = asyncio.Event()

    async def handler(params, context):
        started.set()
        await asyncio.Event().wait()

    manager = ToolManager()
    register(manager, handler)
    task = asyncio.create_task(manager.execute("check", {"n": 1}, CTX))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert manager.get_stats()["check"]["in_flight"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stats = manager.get_stats()["check"]
    assert stats["cancelled"] == 1 and stats["in_flight"] == stats["failed"] == 0
    assert stats["latency_samples"] == 1


@pytest.mark.asyncio
async def test_metrics_and_correlated_logs_exclude_private_fields_and_unknown_tool_names(caplog):
    caplog.set_level(logging.INFO)
    manager = ToolManager()
    register(manager)
    llm = FakeLLM(tool_turn(ToolUse("PRIVATE_ID", "check", {"n": 1})))
    loop = FunctionCallingLoop(llm, manager, ToolSettings(enabled=True, trigger="always", max_rounds=1))
    await loop.run("PRIVATE_MESSAGE", CTX)
    await manager.execute("PRIVATE_UNKNOWN_TOOL", {}, CTX)
    snapshot = manager.get_health()
    serialized = json.dumps(snapshot) + prometheus_metrics(snapshot) + caplog.text
    for secret in ("private-user", "private-session", "PRIVATE_ID", "PRIVATE_MESSAGE", "PRIVATE_RESULT", "PRIVATE_UNKNOWN_TOOL"):
        assert secret not in serialized
    records = []
    for record in caplog.records:
        text = record.getMessage()
        if text.startswith(("tool_call ", "function_calling_completed ")):
            records.append(json.loads(text.split(" ", 1)[1]))
    assert len(records) == 3 and all(record["request_id"] == "req-safe" for record in records)
    assert set(snapshot["tools"]) == {"check"}


@pytest.mark.asyncio
async def test_rejected_call_is_logged_without_logging_model_supplied_name(caplog):
    caplog.set_level(logging.INFO)
    manager = ToolManager()
    register(manager)
    llm = FakeLLM(tool_turn(ToolUse("PRIVATE_ID", "PRIVATE_UNKNOWN_NAME", {})))
    await FunctionCallingLoop(
        llm, manager, ToolSettings(enabled=True, trigger="always", max_rounds=1),
    ).run("PRIVATE_MESSAGE", CTX)
    records = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
               if record.getMessage().startswith("tool_result ")]
    assert len(records) == 1
    assert records[0]["tool"] == "_unknown" and records[0]["error_code"] == "tool_not_allowed"
    assert records[0]["request_id"] == "req-safe"
    assert "PRIVATE_" not in caplog.text


@pytest.mark.asyncio
async def test_health_reads_do_not_reserve_half_open_probe_or_change_stats():
    now = [0.0]
    manager = ToolManager(clock=lambda: now[0], circuit_failure_threshold=1, circuit_recovery_s=10)
    register(manager)
    breaker = manager._tools["check"].breaker
    breaker.record_failure()
    now[0] = 11.0
    before = manager.get_stats()
    assert manager.get_health()["status"] == "degraded"
    prometheus_metrics(manager.get_health())
    assert manager.get_stats() == before
    assert breaker.state.value == "open" and breaker.allow() is True


@pytest.mark.asyncio
async def test_fork_and_unregister_do_not_retain_monitor_samples():
    manager = ToolManager()
    register(manager)
    await manager.execute("check", {"n": 1}, CTX)
    clone = manager.fork()
    assert clone.get_stats()["check"]["latency_samples"] == 0
    clone.get_stats()["check"]["errors"]["modified"] = 100
    assert manager.get_stats()["check"]["errors"] == {}
    manager.unregister("check")
    register(manager)
    assert manager.get_stats()["check"]["latency_samples"] == 0


@pytest.mark.asyncio
async def test_loop_aggregates_success_repair_duplicate_and_protocol_fallback():
    manager = ToolManager()
    register(manager)
    settings = ToolSettings(enabled=True, trigger="always")
    llm = FakeLLM(
        tool_turn(ToolUse("a", "check", {"n": "bad"})),
        tool_turn(ToolUse("b", "check", {"n": 1})),
    )
    await FunctionCallingLoop(llm, manager, settings).run("repair", CTX)
    duplicate = FakeLLM(
        tool_turn(ToolUse("c", "check", {"n": 2})),
        tool_turn(ToolUse("d", "check", {"n": 2})),
    )
    await FunctionCallingLoop(duplicate, manager, settings).run("duplicate", CTX)
    unsupported = FakeLLM(LLMError("PRIVATE", code="tool_protocol_unsupported"))
    await FunctionCallingLoop(unsupported, manager, settings).run("fallback", CTX)
    stats = manager.get_health()["function_calling"]
    assert stats["total"] == stats["success"] == 3
    assert stats["repairs"] == stats["duplicate_rejections"] == stats["protocol_fallbacks"] == 1
    assert stats["calls"] == 3 and stats["rounds"] == 4
    assert stats["input_tokens"] == 40 and stats["output_tokens"] == 20
    assert stats["in_flight"] == 0


@pytest.mark.asyncio
async def test_loop_counts_failure_and_explicit_stream_close_separately():
    manager = ToolManager()
    register(manager)
    settings = ToolSettings(enabled=True, trigger="always")
    bad = FakeLLM(LLMError("PRIVATE"))
    await FunctionCallingLoop(bad, manager, settings).run("fail", CTX)
    direct = FakeLLM(answer())
    stream = FunctionCallingLoop(direct, manager, settings).stream("direct", CTX)
    await anext(stream)
    assert manager.get_health()["function_calling"]["in_flight"] == 1
    await stream.aclose()
    stats = manager.get_health()["function_calling"]
    assert stats["failed"] == stats["aborted"] == 1
    assert stats["total"] == 2 and stats["in_flight"] == 0


@pytest.mark.asyncio
async def test_cancelled_model_request_is_recorded_once_and_releases_in_flight():
    started = asyncio.Event()

    class WaitingLLM(FakeLLM):
        async def create_message(self, **kwargs):
            started.set()
            await asyncio.Event().wait()

    manager = ToolManager()
    register(manager)
    loop = FunctionCallingLoop(
        WaitingLLM(), manager, ToolSettings(enabled=True, trigger="always"),
    )
    task = asyncio.create_task(loop.run("private", CTX))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert manager.get_health()["function_calling"]["in_flight"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stats = manager.get_health()["function_calling"]
    assert stats["total"] == stats["cancelled"] == 1
    assert stats["failed"] == stats["in_flight"] == 0


@pytest.mark.asyncio
async def test_loop_latency_uses_injected_clock_and_export_converts_to_seconds():
    now = [0.0]

    class ClockedLLM(FakeLLM):
        async def create_message(self, **kwargs):
            now[0] += 2.0
            return answer()

    manager = ToolManager()
    register(manager)
    result = await FunctionCallingLoop(
        ClockedLLM(), manager, ToolSettings(enabled=True, trigger="always"), clock=lambda: now[0],
    ).run("private", CTX)
    assert result.latency_ms == 2000.0
    assert manager.get_health()["function_calling"]["avg_latency_ms"] == 2000.0
    assert "commercemind_function_calling_latency_p95_seconds 2.0" in prometheus_metrics(manager.get_health())


async def get(app, path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/tools/stats", "/metrics"])
async def test_debug_routes_do_not_exist_when_debug_is_off(path):
    app = create_app(Settings(tools=ToolSettings(enabled=True, debug=False)))
    assert (await get(app, path)).status_code == 404
    assert path not in app.openapi()["paths"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,status", [(False, "disabled"), (True, "unavailable")])
async def test_debug_distinguishes_disabled_from_unavailable(enabled, status):
    app = create_app(Settings(tools=ToolSettings(enabled=enabled, debug=True)))
    response = await get(app, "/tools/stats")
    assert response.json()["status"] == status
    assert response.json()["tools"] == {}
    assert "commercemind_tools_available 0" in (await get(app, "/metrics")).text


@pytest.mark.asyncio
async def test_debug_exports_registered_tool_metrics_in_prometheus_format():
    app = create_app(Settings(
        api_key="test", tools=ToolSettings(enabled=True, debug=True),
    ))
    response = await get(app, "/tools/stats")
    assert response.json()["status"] == "ok" and len(response.json()["tools"]) == 8
    metrics = await get(app, "/metrics")
    assert metrics.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert '# TYPE commercemind_tool_calls_total counter' in metrics.text
    assert 'commercemind_tool_calls_total{tool="compare_amounts"} 0' in metrics.text
    assert 'commercemind_tool_circuit_state{tool="compare_amounts",state="closed"} 1' in metrics.text
