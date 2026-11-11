"""基于关键词规则的同步意图识别。"""
from __future__ import annotations

from dataclasses import dataclass
import re

from core.intent import IntentCandidate, IntentCategory, IntentSource


@dataclass(frozen=True, slots=True)
class PatternMatch:
    candidate: IntentCandidate
    matched_patterns: tuple[str, ...]
    priority: str


_SPECIFIC_RULES: tuple[tuple[IntentCategory, tuple[str, ...]], ...] = (
    (IntentCategory.HUMAN_HANDOFF, ("转人工", "人工客服", "找人工")),
    (IntentCategory.ORDER_STATUS, ("订单状态", "发货了吗", "处理到哪", "order status")),
    (IntentCategory.LOGISTICS, ("物流", "快递", "配送", "运单", "delivery", "shipping")),
    (IntentCategory.REFUND, ("退款", "退货", "refund", "return")),
    (IntentCategory.INVOICE, ("发票", "抬头", "税号", "invoice")),
    (IntentCategory.PAYMENT_ISSUE, ("重复扣款", "多扣", "支付失败", "扣费", "payment failed")),
    (IntentCategory.ACCOUNT, ("被盗", "异常登录", "重置密码", "两步验证", "安全")),
    (IntentCategory.TECHNICAL, ("无法登录", "登录失败", "401", "验证码", "崩溃", "闪退", "500", "报错", "crash")),
)

_GENERIC_RULES: tuple[tuple[IntentCategory, tuple[str, ...]], ...] = (
    (IntentCategory.COMPLAINT, ("投诉", "太差", "糟糕", "horrible", "等了很久")),
    (IntentCategory.COMPLAINT, ("经理", "supervisor")),
    (IntentCategory.OTHER, ("?", "？", "怎么", "什么", "status")),
    (IntentCategory.OTHER, ("帮我", "需要", "please", "help")),
    (IntentCategory.GREETING, ("你好", "嗨", "hello", "hi")),
    (IntentCategory.OTHER, ("密码", "邮箱", "账户", "password")),
)


def _contains(text: str, pattern: str) -> bool:
    if pattern.isascii() and pattern.isalpha():
        return re.search(rf"(?<![a-z0-9]){re.escape(pattern)}(?![a-z0-9])", text) is not None
    return pattern in text


def _best_match(text: str, rules: tuple[tuple[IntentCategory, tuple[str, ...]], ...]):
    for category, patterns in rules:
        hits = tuple(pattern for pattern in patterns if _contains(text, pattern))
        if hits:
            return category, hits
    return None, ()


def recognize_pattern(message: str) -> PatternMatch:
    """按具体规则优先、通用规则兜底识别一条消息。

    输入原始消息；输出候选、所有命中关键词及命中规则层级。无命中返回 OTHER/0。
    """
    if not isinstance(message, str):
        raise TypeError("message 必须是字符串")
    text = message.strip().lower()
    category, hits = _best_match(text, _SPECIFIC_RULES)
    priority = "specific"
    confidence = 0.90
    if category is None:
        category, hits = _best_match(text, _GENERIC_RULES)
        priority = "generic"
        confidence = 0.60
    if category is None:
        category, hits, priority, confidence = IntentCategory.OTHER, (), "none", 0.0
    return PatternMatch(
        candidate=IntentCandidate(category, confidence, IntentSource.PATTERN),
        matched_patterns=hits,
        priority=priority,
    )
