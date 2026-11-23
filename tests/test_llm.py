"""LLM 客户端：历史消息、system 与 usage。全部使用替身，不访问网络。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from core.config import Settings
from core.llm import SYSTEM_PROMPT, ChatResult, LLMClient, LLMError, ToolUse


class _Messages:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def make_client(*, usage=None, text: str = "已经为您查询。"):
    client = LLMClient(Settings(api_key="test-key", model="test-model", timeout_s=2))
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=usage,
    )
    messages = _Messages(response)
    client._client.messages.create = messages.create
    return client, messages


@pytest.mark.asyncio
async def test_chat_sends_history_in_order_and_system_separately():
    client, messages = make_client(
        usage=SimpleNamespace(input_tokens=21, output_tokens=8),
    )

    result = await client.chat(
        "那我刚才说了什么？",
        history=[
            {"role": "user", "content": "我叫小李"},
            {"role": "assistant", "content": "你好，小李。"},
        ],
        system="自定义 system",
    )

    assert result == ChatResult("已经为您查询。", 21, 8)
    call = messages.calls[0]
    assert call["system"] == "自定义 system"
    assert call["messages"] == [
        {"role": "user", "content": "我叫小李"},
        {"role": "assistant", "content": "你好，小李。"},
        {"role": "user", "content": "那我刚才说了什么？"},
    ]


@pytest.mark.asyncio
async def test_chat_without_history_keeps_single_message_and_default_system():
    client, messages = make_client()

    result = await client.chat("你好")

    assert result.text == "已经为您查询。"
    assert result.input_tokens is None
    assert messages.calls[0]["system"] == SYSTEM_PROMPT
    assert messages.calls[0]["messages"] == [{"role": "user", "content": "你好"}]


@pytest.mark.asyncio
async def test_chat_rejects_invalid_history_before_calling_upstream():
    client, messages = make_client()

    with pytest.raises(ValueError):
        await client.chat("你好", history=[{"role": "system", "content": "x"}])
    with pytest.raises(ValueError):
        await client.chat("你好", history=[{"role": "user", "content": "  "}])
    with pytest.raises(ValueError):
        await client.chat("   ")

    assert messages.calls == []


@pytest.mark.asyncio
async def test_usage_accepts_dict_and_ignores_invalid_values():
    client, _ = make_client(usage={"input_tokens": 7, "output_tokens": "bad"})

    result = await client.chat("你好")

    assert result.input_tokens == 7
    assert result.output_tokens is None


@pytest.mark.asyncio
async def test_summarize_still_returns_plain_text():
    client, messages = make_client(text="用户咨询退款。")

    summary = await client.summarize("总结这段对话")

    assert summary == "用户咨询退款。"
    assert messages.calls[0]["system"].startswith("你是会话摘要器")


@pytest.mark.asyncio
async def test_native_tool_turn_can_be_replayed_to_finish_in_stream():
    client, upstream = make_client()
    upstream.response = SimpleNamespace(
        content=[
            TextBlock(type="text", text="我来查一下。"),
            ToolUseBlock(type="tool_use", id="c1", name="lookup", input={"id": "A1001"}),
            {"type": "tool_use", "id": "c2", "name": "lookup", "input": '{"id":"A1002"}'},
        ],
        stop_reason="tool_use",
        usage={"input_tokens": 25, "output_tokens": 15},
    )
    messages = [{"role": "user", "content": "查询两个订单"}]
    tools = [{"name": "lookup", "input_schema": {"type": "object"}}]
    turn = await client.create_message(messages=messages, tools=tools, tool_choice={"type": "auto"})
    assert turn.text == "我来查一下。"
    assert turn.tool_uses == (
        ToolUse("c1", "lookup", {"id": "A1001"}),
        ToolUse("c2", "lookup", {"id": "A1002"}),
    )
    assert (turn.stop_reason, turn.input_tokens, turn.output_tokens) == ("tool_use", 25, 15)
    assert upstream.calls[0]["tools"] == tools
    assert upstream.calls[0]["tool_choice"] == {"type": "auto"}
    assert upstream.calls[0]["messages"] == messages

    transcript = [
        *messages,
        {"role": "assistant", "content": turn.content},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": "已发货"}
            for call in turn.tool_uses
        ]},
    ]
    assert turn.content[2]["input"] == {"id": "A1002"}

    async def deltas():
        yield "两个订单"
        yield "均已发货。"

    @asynccontextmanager
    async def stream(**kwargs):
        assert kwargs["messages"] == transcript
        assert kwargs["system"] == "工具对话规则"
        assert kwargs["tools"] == tools
        assert kwargs["tool_choice"] == {"type": "none"}
        yield SimpleNamespace(text_stream=deltas())

    client._client.messages.stream = stream
    result = [delta async for delta in client.stream_message(
        messages=transcript, system="工具对话规则", tools=tools, tool_choice={"type": "none"},
    )]
    assert result == ["两个订单", "均已发货。"]


@pytest.mark.asyncio
async def test_direct_answer_is_available_without_second_request():
    client, upstream = make_client()
    turn = await client.create_message(messages=[{"role": "user", "content": "你好"}])
    assert turn.text == "已经为您查询。"
    assert turn.stop_reason == "end_turn"
    assert turn.tool_uses == ()
    assert len(upstream.calls) == 1
    assert "tools" not in upstream.calls[0] and "tool_choice" not in upstream.calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,stop_reason", [
    ({"id": None}, "tool_use"),
    ({"name": " "}, "tool_use"),
    ({"input": []}, "tool_use"),
    ({"input": '"{}"'}, "tool_use"),
    ({"input": "{"}, "tool_use"),
    ({"id": "first"}, "tool_use"),
    ({}, "max_tokens"),
])
async def test_invalid_tool_block_rejects_entire_turn(fields, stop_reason):
    client, upstream = make_client()
    upstream.response = {
        "stop_reason": stop_reason,
        "content": [
            {"type": "tool_use", "id": "first", "name": "lookup", "input": {}},
            {"type": "tool_use", "id": "second", "name": "lookup", "input": {}, **fields},
        ],
    }
    with pytest.raises(LLMError) as raised:
        await client.create_message(messages=[{"role": "user", "content": "查询"}])
    assert raised.value.code == "tool_protocol_error"
