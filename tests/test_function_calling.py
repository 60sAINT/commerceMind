"""T9：模型往返、工具结果和预算限制，全部使用本地替身。"""
import asyncio
import copy
import json

import pytest

from core.config import ToolSettings
from core.llm import LLMError, MessageTurn, ToolUse
from tooling import FunctionCallingLoop, ToolContext, ToolManager, ToolSpec, build_commerce_tools


class FakeLLM:
    def __init__(self, *turns):
        self.turns = iter(turns)
        self.requests = []
        self.streams = []
        self.final = ["最终", "回答"]

    async def create_message(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        return turn

    async def stream_message(self, **kwargs):
        self.streams.append(copy.deepcopy(kwargs))
        for text in self.final:
            if isinstance(text, Exception):
                raise text
            yield text


def tool_turn(*calls):
    return MessageTurn(
        content=[{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input} for call in calls],
        text="", tool_uses=calls, stop_reason="tool_use", input_tokens=10, output_tokens=5,
    )


def answer(text="直接回答"):
    return MessageTurn([{"type": "text", "text": text}], text, (), "end_turn", 2, 3)


def make_loop(llm, **settings):
    manager = ToolManager()
    for spec in build_commerce_tools().values():
        manager.register(spec)
    return FunctionCallingLoop(
        llm, manager, ToolSettings(**({"enabled": True, "trigger": "always"} | settings)),
    ), manager


def ctx(**kwargs):
    return ToolContext("u", "s", "r", **kwargs)


@pytest.mark.asyncio
async def test_direct_answer_is_not_requested_again_and_history_is_untouched():
    llm = FakeLLM(answer())
    loop, _ = make_loop(llm)
    history = [{"role": "user", "content": "之前"}]
    result = await loop.run("你好", ctx(), history=history, system="system")
    assert result.answer == "直接回答" and result.success
    assert result.calls == result.rounds == 0
    assert result.input_tokens == 2
    assert not llm.streams
    assert history == [{"role": "user", "content": "之前"}]
    assert llm.requests[0]["messages"][-1] == {"role": "user", "content": "你好"}


@pytest.mark.asyncio
async def test_tool_round_trip_and_parameter_repair_before_direct_answer():
    llm = FakeLLM(
        tool_turn(ToolUse("a", "compare_amounts", {"amount_a": 10})),
        tool_turn(ToolUse("b", "compare_amounts", {"amount_a": 10, "amount_b": 3})),
    )
    loop, _ = make_loop(llm)
    result = await loop.run("比较金额", ctx())
    assert result.success and result.answer == "最终回答"
    assert result.calls == result.rounds == 2
    assert result.tools_used == ["compare_amounts"]
    repair = json.loads(llm.requests[1]["messages"][-1]["content"][0]["content"])
    assert repair["error"]["code"] == "invalid_arguments"
    assert repair["hint"]["required"] == ["amount_a", "amount_b"]
    final = llm.streams[0]
    last = final["messages"][-1]["content"][0]
    assert last["tool_use_id"] == "b" and last["is_error"] is False
    assert json.loads(last["content"])["data"]["difference"] == 7
    assert final["tool_choice"] == {"type": "none"}
    assert result.input_tokens == 20 and result.output_tokens == 10


@pytest.mark.asyncio
async def test_followup_answer_avoids_extra_final_stream_request():
    llm = FakeLLM(tool_turn(ToolUse("a", "lookup_error_code", {"error_code": "401"})), answer("认证失败"))
    loop, _ = make_loop(llm)
    result = await loop.run("401 是什么", ctx())
    assert result.answer == "认证失败" and len(llm.requests) == 2
    assert not llm.streams


@pytest.mark.asyncio
async def test_parallel_calls_are_paired_and_excess_calls_get_limit_errors():
    calls = tuple(ToolUse(str(i), "lookup_error_code", {"error_code": str(i)}) for i in range(4))
    llm = FakeLLM(tool_turn(*calls))
    loop, manager = make_loop(llm, max_calls_per_round=2, max_calls_total=3)
    result = await loop.run("查询", ctx())
    replies = llm.streams[0]["messages"][-1]["content"]
    assert [item["tool_use_id"] for item in replies] == ["0", "1", "2", "3"]
    assert [item["is_error"] for item in replies] == [False, False, True, True]
    assert json.loads(replies[2]["content"])["error"]["code"] == "too_many_tool_calls"
    assert result.calls == manager.get_stats()["lookup_error_code"]["total"] == 2


@pytest.mark.asyncio
async def test_duplicate_call_is_not_executed_twice():
    llm = FakeLLM(
        tool_turn(ToolUse("a", "lookup_error_code", {"error_code": "401"})),
        tool_turn(ToolUse("b", "lookup_error_code", {"error_code": "401"})),
    )
    loop, manager = make_loop(llm)
    result = await loop.run("查询", ctx())
    assert result.calls == 1
    assert result.tool_traces[-1]["error_code"] == "duplicate_tool_call"
    assert manager.get_stats()["lookup_error_code"]["total"] == 1


@pytest.mark.asyncio
async def test_allowlist_filters_definitions_and_enforces_execution():
    llm = FakeLLM(tool_turn(ToolUse("a", "compare_amounts", {"amount_a": 10, "amount_b": 2})))
    loop, manager = make_loop(llm, max_rounds=1)
    result = await loop.run("查询", ctx(), allowed_tools=["lookup_error_code"])
    assert [item["name"] for item in llm.requests[0]["tools"]] == ["lookup_error_code"]
    assert result.tool_traces[0]["error_code"] == "tool_not_allowed"
    assert result.calls == manager.get_stats()["compare_amounts"]["total"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("options,intent", [
    ({"enabled": False}, "technical"), ({"trigger": "off"}, "technical"),
    ({"trigger": "auto"}, "greeting"),
])
async def test_disabled_or_unmatched_intent_keeps_normal_stream(options, intent):
    llm = FakeLLM()
    manager = ToolManager()
    for spec in build_commerce_tools().values():
        manager.register(spec)
    loop = FunctionCallingLoop(llm, manager, ToolSettings(**({"enabled": True, "trigger": "auto"} | options)))
    result = await loop.run("你好", ctx(intent=intent))
    assert result.answer == "最终回答" and not llm.requests
    assert "tools" not in llm.streams[0]


@pytest.mark.asyncio
async def test_phase_budget_bounds_model_decision_and_then_allows_final_answer():
    class SlowLLM(FakeLLM):
        async def create_message(self, **kwargs):
            self.requests.append(kwargs)
            await asyncio.Event().wait()

    llm = SlowLLM()
    loop, _ = make_loop(llm, timeout_s=0.02, phase_timeout_s=0.02)
    result = await asyncio.wait_for(loop.run("查询", ctx()), timeout=1)
    assert result.answer == "最终回答" and result.calls == 0
    assert len(llm.requests) == len(llm.streams) == 1


@pytest.mark.asyncio
async def test_result_truncation_stays_valid_json_and_signals_incomplete_data():
    llm = FakeLLM(tool_turn(ToolUse("a", "large", {})))
    loop, manager = make_loop(llm, max_rounds=1, max_result_chars=256)
    manager.register(ToolSpec("large", "大结果", {"type": "object", "properties": {}}, lambda p, c: '"中文\\' * 1000))
    result = await loop.run("查询", ctx())
    content = llm.streams[0]["messages"][-1]["content"][0]["content"]
    assert len(content) <= 256 and json.loads(content)["truncated"]
    assert result.tool_traces[0]["truncated"]


@pytest.mark.asyncio
async def test_business_failure_is_reported_as_error_to_model():
    llm = FakeLLM(tool_turn(ToolUse("a", "search_knowledge_base", {"query": "x"})))
    loop, _ = make_loop(llm, max_rounds=1)
    await loop.run("查询", ctx())
    reply = llm.streams[0]["messages"][-1]["content"][0]
    assert reply["is_error"]
    assert json.loads(reply["content"])["data"]["success"] is False


@pytest.mark.asyncio
async def test_concurrent_requests_keep_context_and_transcripts_separate():
    class ContextLLM(FakeLLM):
        async def create_message(self, **kwargs):
            self.requests.append(copy.deepcopy(kwargs))
            await asyncio.sleep(0)
            return tool_turn(ToolUse("same-id", "create_handoff_summary", {}))

    llm = ContextLLM()
    loop, _ = make_loop(llm, max_rounds=1)
    await asyncio.gather(
        loop.run("A", ToolContext("a", "a", "request-a")),
        loop.run("B", ToolContext("b", "b", "request-b")),
    )
    observed = {
        item["messages"][0]["content"]: json.loads(item["messages"][-1]["content"][0]["content"])["data"]["request_id"]
        for item in llm.streams
    }
    assert observed == {"A": "request-a", "B": "request-b"}


@pytest.mark.asyncio
async def test_failure_after_streamed_text_does_not_restart_or_repeat_tools():
    llm = FakeLLM(tool_turn(ToolUse("a", "lookup_error_code", {"error_code": "401"})))
    llm.final = ["已生成的文本", LLMError("private upstream")]
    loop, _ = make_loop(llm, max_rounds=1)
    result = await loop.run("查询", ctx())
    assert not result.success and result.error_code == "tool_answer_failed"
    assert result.answer.startswith("已生成的文本\n")
    assert "private upstream" not in result.answer and result.calls == 1


@pytest.mark.asyncio
async def test_total_call_limit_applies_across_rounds():
    llm = FakeLLM(
        tool_turn(ToolUse("a", "lookup_error_code", {"error_code": "401"})),
        tool_turn(
            ToolUse("b", "lookup_error_code", {"error_code": "403"}),
            ToolUse("c", "lookup_error_code", {"error_code": "404"}),
        ),
    )
    loop, manager = make_loop(llm, max_rounds=5, max_calls_per_round=2, max_calls_total=2)
    result = await loop.run("查询", ctx())
    assert result.rounds == result.calls == 2
    assert manager.get_stats()["lookup_error_code"]["total"] == 2
    assert result.tool_traces[-1]["error_code"] == "too_many_tool_calls"
    assert len(llm.streams) == 1


@pytest.mark.asyncio
async def test_duplicate_detection_uses_normalized_arguments():
    llm = FakeLLM(
        tool_turn(ToolUse("a", "lookup_error_code", {"error_code": "401"})),
        tool_turn(ToolUse("b", "lookup_error_code", {"error_code": " 401 "})),
    )
    loop, _ = make_loop(llm)
    result = await loop.run("查询", ctx())
    assert result.calls == 1
    assert result.tool_traces[-1]["error_code"] == "duplicate_tool_call"


@pytest.mark.asyncio
async def test_optional_events_are_structured_and_do_not_include_arguments_or_data():
    llm = FakeLLM(tool_turn(ToolUse("a", "lookup_error_code", {"error_code": "401"})))
    loop, _ = make_loop(llm, max_rounds=1, stream_events=True)
    events = [event async for event in loop.stream("查询", ctx())]
    assert [event.type for event in events] == ["tool_start", "tool_end", "delta", "delta", "done"]
    assert events[-1].data["answer"] == "".join(event.data["text"] for event in events if event.type == "delta")
    for event in events[:2]:
        assert "input" not in event.data and "data" not in event.data


@pytest.mark.asyncio
async def test_cancelling_tool_call_propagates_without_a_final_model_request():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(params, context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    llm = FakeLLM(tool_turn(ToolUse("a", "wait_tool", {})))
    loop, manager = make_loop(llm)
    manager.register(ToolSpec("wait_tool", "等待", {"type": "object", "properties": {}}, handler))
    task = asyncio.create_task(loop.run("查询", ctx()))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and not llm.streams
    assert manager.get_stats()["wait_tool"]["failed"] == 0


@pytest.mark.asyncio
async def test_closing_stream_releases_upstream_stream():
    closed = asyncio.Event()

    class ClosingLLM(FakeLLM):
        async def stream_message(self, **kwargs):
            try:
                yield "第一个片段"
                yield "第二个片段"
            finally:
                closed.set()

    loop, _ = make_loop(ClosingLLM(), trigger="off")
    stream = loop.stream("查询", ctx())
    first = await anext(stream)
    assert first.data["text"] == "第一个片段"
    await stream.aclose()
    assert closed.is_set()
