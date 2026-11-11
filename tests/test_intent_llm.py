from types import SimpleNamespace

import pytest

from core.intent import IntentCategory, IntentSource
from core.intent_llm import IntentLLMClassifier


class FakeLLM:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []

    async def chat(self, message, *, system=None, history=None):
        self.calls.append({"message": message, "system": system, "history": history})
        if self.error:
            raise self.error
        return SimpleNamespace(text=self.text)


@pytest.mark.asyncio
async def test_valid_json_is_parsed_and_sent_with_classifier_prompt():
    llm = FakeLLM('{"intent":"refund","confidence":0.93,"reasoning":"用户明确要求退款"}')
    result = await IntentLLMClassifier(llm).recognize("这个订单我要退款")
    assert result.intent is IntentCategory.REFUND
    assert result.confidence == 0.93
    assert result.source is IntentSource.LLM
    assert llm.calls[0]["message"] == "这个订单我要退款"
    assert "只输出一个 JSON" in llm.calls[0]["system"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "不是 JSON", "```json\n{}\n```", '{"intent":"unknown","confidence":0.8,"reasoning":"x"}',
    '{"intent":"refund","confidence":NaN,"reasoning":"x"}',
    '{"intent":"refund","confidence":true,"reasoning":"x"}',
    '{"intent":"refund","confidence":0.8}',
])
async def test_malformed_output_degrades_to_other(text):
    result = await IntentLLMClassifier(FakeLLM(text)).recognize("我要退款")
    assert result.intent is IntentCategory.OTHER
    assert result.confidence == 0
    assert result.source is IntentSource.LLM


@pytest.mark.asyncio
async def test_llm_call_failure_degrades_to_other():
    result = await IntentLLMClassifier(FakeLLM(error=RuntimeError("offline"))).recognize("我要退款")
    assert result.intent is IntentCategory.OTHER
    assert result.confidence == 0


@pytest.mark.asyncio
async def test_empty_message_skips_llm_call():
    llm = FakeLLM("unused")
    result = await IntentLLMClassifier(llm).recognize(" ")
    assert result.intent is IntentCategory.OTHER
    assert result.confidence == 0
    assert llm.calls == []
