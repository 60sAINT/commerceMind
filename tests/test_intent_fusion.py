import pytest

from core.config import IntentSettings
from core.intent import IntentCandidate, IntentCategory as I, IntentSource as S
from core.intent_fusion import combine_intents


def candidate(source, intent, confidence):
    return IntentCandidate(intent, confidence, source)


def test_agreement_accumulates_weighted_scores_and_uses_llm_as_source():
    result = combine_intents({
        S.LLM: candidate(S.LLM, I.REFUND, 0.8),
        S.EMBEDDING: candidate(S.EMBEDDING, I.REFUND, 0.9),
        S.PATTERN: candidate(S.PATTERN, I.REFUND, 0.9),
    })
    assert result.intent is I.REFUND
    assert result.confidence == pytest.approx(0.83)
    assert result.source is S.LLM
    assert result.source_scores == {"llm": 0.8, "embedding": 0.9, "pattern": 0.9}


def test_llm_weight_wins_conflict():
    result = combine_intents({
        S.LLM: candidate(S.LLM, I.REFUND, 0.9),
        S.EMBEDDING: candidate(S.EMBEDDING, I.LOGISTICS, 0.9),
        S.PATTERN: candidate(S.PATTERN, I.LOGISTICS, 0.9),
    })
    assert result.intent is I.REFUND
    assert result.confidence == pytest.approx(0.63)


def test_failed_source_is_ignored_and_remaining_scores_are_not_renormalized():
    result = combine_intents({
        S.LLM: candidate(S.LLM, I.OTHER, 0.0),
        S.EMBEDDING: candidate(S.EMBEDDING, I.LOGISTICS, 0.9),
        S.PATTERN: candidate(S.PATTERN, I.LOGISTICS, 0.9),
    }, IntentSettings(confidence_threshold=0.2))
    assert result.intent is I.LOGISTICS
    assert result.confidence == pytest.approx(0.27)
    assert result.source is S.EMBEDDING


def test_threshold_falls_back_and_failure_policy_can_be_strict():
    candidates = {
        S.LLM: candidate(S.LLM, I.REFUND, 0.4),
        S.EMBEDDING: candidate(S.EMBEDDING, I.OTHER, 0.0),
        S.PATTERN: candidate(S.PATTERN, I.OTHER, 0.0),
    }
    available = combine_intents(candidates)
    assert available.intent is I.OTHER
    assert available.confidence == pytest.approx(0.28)

    strict = combine_intents(candidates, IntentSettings(failure_policy="other"))
    assert strict.intent is I.OTHER and strict.confidence == 0
    assert strict.source is S.FALLBACK


def test_disabled_sources_are_excluded_from_votes_and_failures():
    settings = IntentSettings(embedding_enabled=False, pattern_enabled=False)
    result = combine_intents({
        S.LLM: candidate(S.LLM, I.INVOICE, 0.8),
        S.EMBEDDING: None,
        S.PATTERN: None,
    }, settings)
    assert result.intent is I.INVOICE
    assert result.confidence == pytest.approx(0.56)


def test_exact_score_tie_breaks_by_source_precedence():
    result = combine_intents({
        S.LLM: candidate(S.LLM, I.REFUND, 0.2),
        S.EMBEDDING: candidate(S.EMBEDDING, I.LOGISTICS, 0.7),
        S.PATTERN: candidate(S.PATTERN, I.OTHER, 0.0),
    }, IntentSettings(confidence_threshold=0.1))
    assert result.confidence == pytest.approx(0.14)
    assert result.intent is I.REFUND


def test_malformed_candidate_is_ignored():
    result = combine_intents({S.LLM: object(), S.EMBEDDING: None, S.PATTERN: None})
    assert result.intent is I.OTHER
    assert result.confidence == 0
