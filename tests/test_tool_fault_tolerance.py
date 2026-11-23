"""T10 工具调用容错专项：协议边界、repair、依赖故障和安全兜底。"""
import asyncio
import copy
import json
from contextlib import asynccontextmanager

import anthropic
import httpx
import pytest

from core.config import Settings, ToolSettings
from core.llm import LLMClient, LLMError, _parse_message_turn
from tooling import FunctionCallingLoop, ToolContext, ToolManager, ToolSpec


CTX = ToolContext("u", "s", "r")
SCHEMA = {
    "type": "object",
    "properties": {
        "count": {"type": "integer", "minimum": 1, "maximum": 3},
        "kind": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["count", "kind"], "additionalProperties": False,
}


def raw_call(call_id="a", name="check", arguments=None):
    return {"type": "tool_use", "id": call_id, "name": name, "input": arguments if arguments is not None else {"count": 1, "kind": "a"}}


def response(*blocks, stop="tool_use"):
    return {"content": list(blocks), "stop_reason": stop}


class ScriptedLLM:
    """走真实消息解析器，确保无效响应整轮拒绝。"""
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []
        self.streams = []
        self.final = ["核验结束"]

    async def create_message(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return _parse_message_turn(value)

    async def stream_message(self, **kwargs):
        self.streams.append(copy.deepcopy(kwargs))
        for value in self.final:
            if isinstance(value, Exception):
                raise value
            yield value


def setup(llm, *, handler=None, schema=None, fallback=None, retry_safe=True, max_retries=0, **settings):
    manager = ToolManager(circuit_failure_threshold=1)
    manager.register(ToolSpec(
        "check", "核验", schema or SCHEMA, handler or (lambda p, c: {"value": p["count"]}),
        max_retries=max_retries, retry_backoff_s=0, retry_safe=retry_safe,
        timeout_s=0.02, fallback=fallback,
    ))
    loop = FunctionCallingLoop(
        llm, manager, ToolSettings(**({"enabled": True, "trigger": "always"} | settings)),
    )
    return loop, manager


@pytest.mark.asyncio
@pytest.mark.parametrize("block", [
    {"type": "tool_use", "name": "check", "input": {}},
    {"type": "tool_use", "id": "b", "input": {}},
    raw_call("b", arguments=[]),
    {"type": "tool_use", "id": "b", "name": "check"},
    raw_call("b", arguments=42),
    raw_call("b", arguments='"{}"'),
    raw_call("b", arguments='{"count": NaN}'),
    raw_call("b", arguments={"count": float("inf")}),
    raw_call("a"),  # 与本轮前一个合法调用的 ID 重复。
    {"type": None},
])
async def test_invalid_block_rejects_whole_turn_then_bounded_repair(block):
    llm = ScriptedLLM(
        response(raw_call(), block),
        response(raw_call("repaired")),
    )
    loop, manager = setup(llm)
    result = await loop.run("核验", CTX)
    assert result.success and result.repairs == 1 and result.calls == 1
    assert manager.get_stats()["check"]["attempts"] == 1
    assert all(not isinstance(item["content"], list) for item in llm.requests[1]["messages"])
    assert "格式无效" in llm.requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_repeated_malformed_responses_cannot_create_unbounded_repair_loop():
    llm = ScriptedLLM(*[response(raw_call("a", arguments="bad")) for _ in range(2)])
    loop, manager = setup(llm)
    result = await loop.run("核验", CTX)
    assert result.repairs == len(llm.requests) == 2
    assert result.calls == manager.get_stats()["check"]["attempts"] == 0
    assert len(llm.streams) == 1


@pytest.mark.asyncio
async def test_reused_call_id_across_rounds_cannot_execute_another_operation():
    llm = ScriptedLLM(
        response(raw_call("a", arguments={"count": 1, "kind": "a"})),
        response(raw_call("a", arguments={"count": 2, "kind": "b"})),
    )
    loop, _ = setup(llm)
    result = await loop.run("核验", CTX)
    assert result.repairs == result.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"kind": "a"}, {"count": "1", "kind": "a"}, {"count": 1, "kind": "a", "extra": 1},
    {"count": 1, "kind": "invalid"}, {"count": 0, "kind": "a"}, {"count": 4, "kind": "a"},
])
async def test_schema_errors_never_retry_or_trip_circuit_and_can_be_repaired(arguments):
    llm = ScriptedLLM(
        response(raw_call(arguments=arguments)),
        response(raw_call("b")),
    )
    loop, manager = setup(llm, max_retries=3)
    result = await loop.run("核验", CTX)
    stats = manager.get_stats()["check"]
    assert result.success and stats["invalid"] == 1
    assert result.repairs == 1
    assert stats["attempts"] == 1 and stats["retries"] == 0
    assert stats["consecutive_fails"] == 0
    error = json.loads(llm.requests[1]["messages"][-1]["content"][0]["content"])
    assert error["error"]["code"] == "invalid_arguments"
    assert not error["error"]["retryable"]
    assert error["hint"]["properties"]["kind"]["enum"] == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "exception"])
@pytest.mark.parametrize("retry_safe", [True, False])
async def test_dependency_failures_retry_only_readonly_and_count_one_logical_failure(mode, retry_safe):
    calls = 0

    async def handler(params, context):
        nonlocal calls
        calls += 1
        if mode == "timeout":
            await asyncio.Event().wait()
        raise RuntimeError("secret backend path")

    llm = ScriptedLLM(
        response(raw_call()),
        response(raw_call("b", arguments={"count": 2, "kind": "b"})),
    )
    loop, manager = setup(llm, handler=handler, retry_safe=retry_safe, max_retries=2)
    result = await loop.run("核验", CTX)
    expected_attempts = 3 if retry_safe else 1
    assert calls == expected_attempts
    assert manager.get_stats()["check"]["consecutive_fails"] == 1
    assert result.tool_traces[0]["attempts"] == expected_attempts
    assert result.tool_traces[1]["error_code"] == "tool_circuit_open"
    assert result.tool_traces[1]["attempts"] == 0
    assert "secret backend path" not in json.dumps(llm.requests, ensure_ascii=False)


@pytest.mark.asyncio
async def test_duplicate_numeric_arguments_and_key_order_cannot_repeat_a_side_effect():
    calls = 0

    def handler(params, context):
        nonlocal calls
        calls += 1
        raise RuntimeError()

    llm = ScriptedLLM(
        response(raw_call(arguments={"count": 1, "kind": "a"})),
        response(raw_call("b", arguments={"kind": " a ", "count": 1.0})),
    )
    loop, _ = setup(llm, handler=handler, retry_safe=False, max_retries=5)
    result = await loop.run("核验", CTX)
    assert calls == result.calls == 1
    assert result.tool_traces[-1]["error_code"] == "duplicate_tool_call"


@pytest.mark.asyncio
async def test_unserializable_result_is_reported_as_invalid_not_a_success():
    llm = ScriptedLLM(response(raw_call()))
    loop, _ = setup(llm, handler=lambda p, c: object(), max_rounds=1)
    result = await loop.run("核验", CTX)
    reply = llm.streams[0]["messages"][-1]["content"][0]
    assert reply["is_error"] and json.loads(reply["content"])["error"]["code"] == "invalid_result"
    assert result.tool_traces[0]["error_code"] == "invalid_result"
    assert not result.tool_traces[0]["usable"]


@pytest.mark.asyncio
async def test_fallback_usable_data_still_preserves_primary_failure():
    def handler(params, context):
        raise RuntimeError("secret")

    llm = ScriptedLLM(response(raw_call()))
    loop, _ = setup(llm, handler=handler, fallback=lambda p, c, f: {"limited": True}, max_rounds=1)
    result = await loop.run("核验", CTX)
    reply = json.loads(llm.streams[0]["messages"][-1]["content"][0]["content"])
    assert reply["usable"] and reply["degraded"] and not reply["success"]
    assert reply["error"]["code"] == "tool_handler_error"
    assert "secret" not in reply["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_initial_unsupported_tools_respects_fallback_setting(enabled):
    llm = ScriptedLLM(LLMError("unsupported", code="tool_protocol_unsupported"))
    loop, _ = setup(llm, protocol_fallback=enabled)
    result = await loop.run("核验", CTX)
    assert result.protocol_fallback is enabled
    assert result.success is enabled
    assert result.calls == 0
    assert len(llm.streams) == int(enabled)
    if enabled:
        assert "tools" not in llm.streams[0] and "tool_choice" not in llm.streams[0]


@pytest.mark.asyncio
async def test_final_unsupported_tools_replays_text_results_without_reexecuting_handler():
    class RejectToolsLLM(ScriptedLLM):
        async def stream_message(self, **kwargs):
            self.streams.append(copy.deepcopy(kwargs))
            if "tools" in kwargs:
                raise LLMError("unsupported", code="tool_protocol_unsupported")
            yield "根据已有核验结果回答"

    llm = RejectToolsLLM(response(raw_call()))
    loop, manager = setup(llm, max_rounds=1)
    result = await loop.run("核验", CTX)
    assert result.success and result.protocol_fallback
    assert manager.get_stats()["check"]["attempts"] == 1
    assert len(llm.streams) == 2
    plain = llm.streams[1]
    assert "tools" not in plain and all(isinstance(msg["content"], str) for msg in plain["messages"])
    blocks = json.loads(plain["messages"][-1]["content"].split("\n", 1)[1])
    assert json.loads(blocks[0]["content"])["data"] == {"value": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [LLMError("secret"), RuntimeError("secret"), TimeoutError("secret")])
async def test_final_failure_is_safe_and_does_not_retry_tool_or_leak_exception(failure):
    llm = ScriptedLLM(response(raw_call()))
    llm.final = [failure]
    loop, manager = setup(llm, max_rounds=1)
    result = await loop.run("核验", CTX)
    assert not result.success and result.error_code == "tool_answer_failed"
    assert "无法确认" in result.answer and "secret" not in result.answer
    assert manager.get_stats()["check"]["attempts"] == len(llm.streams) == 1


@pytest.mark.asyncio
async def test_partial_output_prevents_protocol_replay_even_if_error_is_unsupported():
    llm = ScriptedLLM(response(raw_call()))
    llm.final = ["已输出", LLMError("secret", code="tool_protocol_unsupported")]
    loop, _ = setup(llm, max_rounds=1)
    result = await loop.run("核验", CTX)
    assert len(llm.streams) == 1 and not result.protocol_fallback
    assert not result.success and result.answer.startswith("已输出\n")


@pytest.mark.asyncio
async def test_failed_plain_protocol_fallback_is_not_retried_again():
    class FailingLLM(ScriptedLLM):
        async def stream_message(self, **kwargs):
            self.streams.append(kwargs)
            raise LLMError("secret", code="tool_protocol_unsupported")
            yield  # pragma: no cover

    llm = FailingLLM(response(raw_call()))
    loop, manager = setup(llm, max_rounds=1)
    result = await loop.run("核验", CTX)
    assert not result.success and result.protocol_fallback and len(llm.streams) == 2
    assert manager.get_stats()["check"]["attempts"] == 1


@pytest.mark.asyncio
async def test_protocol_repair_shares_existing_phase_deadline():
    now = [100.0]

    class ExpiringLLM(ScriptedLLM):
        async def create_message(self, **kwargs):
            now[0] += 100
            return await super().create_message(**kwargs)

    llm = ExpiringLLM(response(raw_call(arguments=[])))
    loop, manager = setup(llm)
    loop._clock = lambda: now[0]
    result = await loop.run("核验", CTX)
    assert result.repairs == 1 and len(llm.requests) == 1
    assert manager.get_stats()["check"]["attempts"] == 0


@pytest.mark.asyncio
async def test_no_tool_request_never_triggers_unsupported_classification():
    client = LLMClient(Settings(api_key="test", model="test", timeout_s=1))

    async def create(**kwargs):
        assert "tools" not in kwargs
        raise upstream_error(400, "tools not supported")

    client._client.messages.create = create
    with pytest.raises(LLMError) as raised:
        await client.create_message(messages=[{"role": "user", "content": "test"}])
    assert raised.value.code == "llm_bad_request"


def upstream_error(status, message):
    resp = httpx.Response(status, request=httpx.Request("POST", "https://example.test/messages"))
    error_type = anthropic.BadRequestError if status == 400 else anthropic.APIStatusError
    return error_type(message, response=resp, body={"error": {"message": message}})


@pytest.mark.asyncio
@pytest.mark.parametrize("status,message,code", [
    (400, "tools is not supported for this model", "tool_protocol_unsupported"),
    (400, "This model does not support tool use", "tool_protocol_unsupported"),
    (422, "Unknown parameter: tools", "tool_protocol_unsupported"),
    (501, "function calling not implemented", "tool_protocol_unsupported"),
    (400, "tool_choice none: input should be auto, any or tool", "tool_protocol_unsupported"),
    (400, "tools[0].input_schema: extra inputs are not permitted", "llm_bad_request"),
    (400, "max_tokens must be a positive integer", "llm_bad_request"),
    (500, "tools not supported temporarily", "llm_upstream_error"),
])
@pytest.mark.parametrize("streaming", [False, True])
async def test_llm_adapter_only_classifies_explicit_protocol_rejection(status, message, code, streaming):
    client = LLMClient(Settings(api_key="test", model="test", timeout_s=1))
    error = upstream_error(status, message)

    async def create(**kwargs):
        raise error

    @asynccontextmanager
    async def stream(**kwargs):
        raise error
        yield  # pragma: no cover

    client._client.messages.create = create
    client._client.messages.stream = stream
    args = {"messages": [{"role": "user", "content": "test"}], "tools": [{"name": "check"}]}
    with pytest.raises(LLMError) as raised:
        if streaming:
            _ = [delta async for delta in client.stream_message(**args)]
        else:
            await client.create_message(**args)
    assert raised.value.code == code
    assert message not in str(raised.value)
