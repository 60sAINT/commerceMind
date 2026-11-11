"""融合 LLM、Embedding、Pattern 三路意图候选。"""
from __future__ import annotations

import math
from collections.abc import Mapping

from core.config import IntentSettings
from core.intent import IntentCandidate, IntentCategory, IntentResult, IntentSource


_SOURCE_ORDER = (IntentSource.LLM, IntentSource.EMBEDDING, IntentSource.PATTERN)


def combine_intents(
    candidates: Mapping[IntentSource, IntentCandidate | None],
    settings: IntentSettings | None = None,
) -> IntentResult:
    """按配置权重累计类别分数，并按阈值产出统一结果。

    缺失、OTHER/0 或未启用来源不参与投票；置信度使用原始加权和，
    不按成功来源重新归一化。分数相同时按 LLM > Embedding > Pattern 决胜。
    """
    settings = settings or IntentSettings()
    weights = {
        IntentSource.LLM: settings.llm_weight,
        IntentSource.EMBEDDING: settings.embedding_weight,
        IntentSource.PATTERN: settings.pattern_weight,
    }
    enabled = {
        IntentSource.LLM: settings.llm_enabled,
        IntentSource.EMBEDDING: settings.embedding_enabled,
        IntentSource.PATTERN: settings.pattern_enabled,
    }
    scores: dict[IntentCategory, float] = {}
    contributors: dict[IntentCategory, list[IntentSource]] = {}
    source_scores: dict[str, float] = {}
    unavailable = False

    for source in _SOURCE_ORDER:
        if not enabled[source]:
            continue
        candidate = candidates.get(source)
        valid = isinstance(candidate, IntentCandidate) and candidate.source is source
        if valid:
            confidence = candidate.confidence
            valid = (not isinstance(confidence, bool) and isinstance(confidence, (int, float))
                     and math.isfinite(confidence) and 0 <= confidence <= 1
                     and isinstance(candidate.intent, IntentCategory))
        if not valid or (candidate.intent is IntentCategory.OTHER and candidate.confidence == 0):
            source_scores[source.value] = 0.0
            unavailable = True
            continue
        assert candidate is not None
        source_scores[source.value] = float(candidate.confidence)
        weighted = weights[source] * float(candidate.confidence)
        scores[candidate.intent] = scores.get(candidate.intent, 0.0) + weighted
        contributors.setdefault(candidate.intent, []).append(source)

    if settings.failure_policy == "other" and unavailable:
        return IntentResult(IntentCategory.OTHER, 0.0, IntentSource.FALLBACK, source_scores=source_scores)
    if not scores:
        return IntentResult(IntentCategory.OTHER, 0.0, IntentSource.FALLBACK, source_scores=source_scores)

    best_intent = max(
        scores,
        key=lambda intent: (
            scores[intent],
            -min(_SOURCE_ORDER.index(source) for source in contributors[intent]),
        ),
    )
    confidence = min(1.0, scores[best_intent])
    if confidence < settings.confidence_threshold:
        return IntentResult(IntentCategory.OTHER, confidence, IntentSource.FALLBACK, source_scores=source_scores)
    winning_sources = contributors[best_intent]
    source = min(winning_sources, key=_SOURCE_ORDER.index)
    return IntentResult(best_intent, confidence, source, source_scores=source_scores)
