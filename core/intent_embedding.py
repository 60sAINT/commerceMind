"""用 embedding 后端的意图示例匹配。"""
from __future__ import annotations

import math
from collections.abc import Callable

from core.config import IntentSettings
from core.intent import IntentCandidate, IntentCategory, IntentSource
from memory.embeddings import embedding_from_env, encode_vectors


_EXAMPLES: dict[IntentCategory, tuple[str, ...]] = {
    IntentCategory.ORDER_STATUS: ("我的订单现在是什么状态？", "订单有没有发货？"),
    IntentCategory.LOGISTICS: ("快递什么时候到？", "物流一直没有更新"),
    IntentCategory.REFUND: ("我要申请退款", "退货退款怎么处理？"),
    IntentCategory.INVOICE: ("帮我开发票", "发票抬头怎么修改？"),
    IntentCategory.PAYMENT_ISSUE: ("支付失败或重复扣款", "为什么多扣了钱？"),
    IntentCategory.ACCOUNT: ("账户无法访问或需要重置密码", "账户信息怎么修改？"),
    IntentCategory.TECHNICAL: ("应用报错或无法登录", "页面崩溃、系统闪退"),
    IntentCategory.COMPLAINT: ("我要投诉，服务太差了", "我等了很久还没有人处理"),
    IntentCategory.HUMAN_HANDOFF: ("请转人工客服", "我要找人工处理"),
    IntentCategory.GREETING: ("你好", "您好，有人在吗？"),
}


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding 向量维度不一致")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        raise ValueError("零向量不能计算余弦相似度")
    return dot / (left_norm * right_norm)


class IntentEmbeddingMatcher:
    """把消息与意图示例比较；默认后端复用 memory.embedding_from_env。"""

    def __init__(
        self,
        embed: Callable[[list[str]], list[list[float]]] | None = None,
        *,
        threshold: float = 0.72,
        embedding_id: str = "injected",
    ) -> None:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
            raise ValueError("threshold 必须在 0 到 1 之间")
        self.threshold = float(threshold)
        self.embedding_id = embedding_id
        self._embed = embed
        self._unavailable = False
        self._template_vectors: dict[IntentCategory, list[list[float]]] = {}

    @classmethod
    def from_env(cls, settings: IntentSettings | None = None) -> "IntentEmbeddingMatcher":
        settings = settings or IntentSettings()
        try:
            embedding_id, embed = embedding_from_env()
            return cls(embed, threshold=settings.embedding_threshold, embedding_id=embedding_id)
        except Exception:
            matcher = cls(threshold=settings.embedding_threshold)
            matcher._unavailable = True
            return matcher

    def _load_templates(self) -> None:
        if self._template_vectors:
            return
        if self._embed is None or self._unavailable:
            raise RuntimeError("embedding 后端不可用")
        categories = list(_EXAMPLES)
        texts = [text for category in categories for text in _EXAMPLES[category]]
        vectors = encode_vectors(self._embed, texts)
        offset = 0
        for category in categories:
            count = len(_EXAMPLES[category])
            self._template_vectors[category] = vectors[offset:offset + count]
            offset += count

    def recognize(self, message: str) -> IntentCandidate:
        """输入当前消息，返回最高相似意图；失败或未过阈值时返回 OTHER/0。"""
        if not isinstance(message, str):
            raise TypeError("message 必须是字符串")
        if not message.strip():
            return IntentCandidate(IntentCategory.OTHER, 0.0, IntentSource.EMBEDDING)
        try:
            self._load_templates()
            assert self._embed is not None
            query = encode_vectors(self._embed, [message])[0]
            scores = [
                (max(_cosine(query, vector) for vector in vectors), category)
                for category, vectors in self._template_vectors.items()
            ]
            score, category = max(scores, key=lambda item: item[0])
            score = max(0.0, min(1.0, score))
            if score < self.threshold:
                return IntentCandidate(IntentCategory.OTHER, 0.0, IntentSource.EMBEDDING)
            return IntentCandidate(category, score, IntentSource.EMBEDDING)
        except Exception:
            self._unavailable = True
            return IntentCandidate(IntentCategory.OTHER, 0.0, IntentSource.EMBEDDING)
