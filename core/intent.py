"""意图识别共享的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IntentCategory(str, Enum):
    ORDER_STATUS = "order_status"
    LOGISTICS = "logistics"
    REFUND = "refund"
    INVOICE = "invoice"
    PAYMENT_ISSUE = "payment_issue"
    ACCOUNT = "account"
    TECHNICAL = "technical"
    COMPLAINT = "complaint"
    HUMAN_HANDOFF = "human_handoff"
    GREETING = "greeting"
    OTHER = "other"


class IntentSource(str, Enum):
    LLM = "llm"
    EMBEDDING = "embedding"
    PATTERN = "pattern"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class IntentResult:
    intent: IntentCategory
    confidence: float
    source: IntentSource
    source_scores: dict[str, float] = field(default_factory=dict)
    entities: dict[str, list[str]] = field(default_factory=dict)
    reasoning: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.intent, IntentCategory):
            raise TypeError("intent 必须是 IntentCategory")
        if not isinstance(self.source, IntentSource):
            raise TypeError("source 必须是 IntentSource")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence 必须在 0 到 1 之间")
        if not isinstance(self.source_scores, dict) or not isinstance(self.entities, dict):
            raise TypeError("source_scores 和 entities 必须是字典")
        if not isinstance(self.reasoning, str):
            raise TypeError("reasoning 必须是字符串")

    def to_state(self) -> dict[str, Any]:
        """转为可写入短期记忆 state 的 JSON 数据。"""
        return {
            "intent": self.intent.value,
            "confidence": float(self.confidence),
            "source": self.source.value,
            "source_scores": dict(self.source_scores),
            "entities": {key: list(values) for key, values in self.entities.items()},
            "reasoning": self.reasoning,
        }


@dataclass(frozen=True, slots=True)
class IntentCandidate:
    """供三路识别融合使用的单路候选。"""
    intent: IntentCategory
    confidence: float
    source: IntentSource

    def __post_init__(self) -> None:
        IntentResult(self.intent, self.confidence, self.source)
