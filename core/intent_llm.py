"""通过现有 LLMClient 执行单路意图分类。"""
from __future__ import annotations

import json
import math
from typing import Any

from core.intent import IntentCandidate, IntentCategory, IntentSource
from core.llm import LLMClient


_SYSTEM_PROMPT = """你是 CommerceMind 的电商客服意图分类器。根据用户当前消息判断一个最主要意图。
只输出一个 JSON 对象，不要 Markdown、代码围栏或额外说明。格式必须是：
{"intent":"<intent_value>","confidence":0.0,"reasoning":"简短中文理由"}
confidence 必须是 0 到 1 的数字。intent 必须从以下值中选择：order_status, logistics, refund, invoice, payment_issue, account, technical, complaint, human_handoff, greeting, other。
优先选择最具体的业务意图；无法判断时选择 other。"""


class IntentLLMClassifier:
    """把用户消息交给现有 LLMClient，并解析为 IntentCandidate。"""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def recognize(self, message: str, *, history=None) -> IntentCandidate:
        if not isinstance(message, str):
            raise TypeError("message 必须是字符串")
        if not message.strip():
            return IntentCandidate(IntentCategory.OTHER, 0.0, IntentSource.LLM)
        try:
            response = await self._llm.chat(message, history=history, system=_SYSTEM_PROMPT)
            data = self._parse(response.text)
            return IntentCandidate(
                IntentCategory(data["intent"]), data["confidence"], IntentSource.LLM,
            )
        except Exception:
            return IntentCandidate(IntentCategory.OTHER, 0.0, IntentSource.LLM)

    @staticmethod
    def _parse(raw: str) -> dict[str, Any]:
        if not isinstance(raw, str):
            raise ValueError("LLM 输出必须是字符串")
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"intent", "confidence", "reasoning"}:
            raise ValueError("LLM 输出必须包含 intent、confidence、reasoning 三个字段")
        if not isinstance(data["intent"], str):
            raise ValueError("intent 必须是字符串")
        IntentCategory(data["intent"])
        confidence = data["confidence"]
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise ValueError("confidence 必须是 0 到 1 的有限数字")
        if not isinstance(data["reasoning"], str):
            raise ValueError("reasoning 必须是字符串")
        return data
