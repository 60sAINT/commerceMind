import math

import pytest

from core.intent import IntentCategory, IntentSource
from core.intent_embedding import IntentEmbeddingMatcher


def fake_embed(texts):
    return [[1.0, 0.0] if "订单" in text or "order" in text.lower() else [0.0, 1.0] for text in texts]


def test_embedding_match_returns_best_category_and_similarity():
    matcher = IntentEmbeddingMatcher(fake_embed, threshold=0.7)
    result = matcher.recognize("查询我的订单")
    assert result.intent is IntentCategory.ORDER_STATUS
    assert result.source is IntentSource.EMBEDDING
    assert result.confidence == pytest.approx(1.0)


def test_below_threshold_returns_other_without_confidence():
    matcher = IntentEmbeddingMatcher(
        lambda texts: [[1.0, 0.0] if text != "unrelated" else [0.0, 1.0] for text in texts],
        threshold=0.7,
    )
    result = matcher.recognize("unrelated")
    assert result.intent is IntentCategory.OTHER
    assert result.confidence == 0


@pytest.mark.parametrize("vectors", [[], [[0.0, 0.0]], [[math.nan, 1.0]]])
def test_invalid_template_vectors_degrade_to_other(vectors):
    matcher = IntentEmbeddingMatcher(lambda texts: vectors * len(texts), threshold=0.5)
    result = matcher.recognize("订单状态")
    assert result.intent is IntentCategory.OTHER
    assert result.confidence == 0


def test_query_dimension_mismatch_degrades_to_other():
    def embed(texts):
        return [[1.0, 0.0] if len(texts) > 1 else [1.0] for _ in texts]
    result = IntentEmbeddingMatcher(embed).recognize("订单状态")
    assert result.intent is IntentCategory.OTHER


def test_embedding_backend_failure_degrades_and_stays_unavailable():
    matcher = IntentEmbeddingMatcher(lambda texts: (_ for _ in ()).throw(ConnectionError()), threshold=0.5)
    assert matcher.recognize("订单状态").intent is IntentCategory.OTHER
    assert matcher.recognize("订单状态").confidence == 0


def test_empty_and_invalid_messages():
    matcher = IntentEmbeddingMatcher(fake_embed)
    assert matcher.recognize(" ").intent is IntentCategory.OTHER
    with pytest.raises(TypeError):
        matcher.recognize(None)
