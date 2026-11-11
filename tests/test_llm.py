"""LLM 客户端：历史消息、system 与 usage。全部使用替身，不访问网络。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.config import Settings
from core.llm import SYSTEM_PROMPT, ChatResult, LLMClient


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
