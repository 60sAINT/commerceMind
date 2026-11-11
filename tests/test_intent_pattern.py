import pytest

from core.intent import IntentCategory, IntentSource
from core.intent_pattern import recognize_pattern


def test_specific_rules_take_priority_and_report_all_hits():
    result = recognize_pattern("请查物流，快递发货了吗？")
    assert result.candidate.intent is IntentCategory.ORDER_STATUS
    assert result.candidate.source is IntentSource.PATTERN
    assert result.candidate.confidence == 0.9
    assert result.priority == "specific"
    assert result.matched_patterns == ("发货了吗",)


def test_generic_rule_is_used_when_no_specific_rule_matches():
    result = recognize_pattern("你好")
    assert result.candidate.intent is IntentCategory.GREETING
    assert result.priority == "generic"
    assert result.matched_patterns == ("你好",)


def test_unmatched_message_returns_other_with_zero_confidence():
    result = recognize_pattern("蓝色的商品还有库存吗")
    assert result.candidate.intent is IntentCategory.OTHER
    assert result.candidate.confidence == 0
    assert result.priority == "none"
    assert result.matched_patterns == ()


def test_english_keyword_matching_is_case_insensitive_and_token_bounded():
    assert recognize_pattern("Where is my SHIPPING update?").candidate.intent is IntentCategory.LOGISTICS
    assert recognize_pattern("membership benefits").candidate.intent is IntentCategory.OTHER


def test_non_string_message_is_rejected():
    with pytest.raises(TypeError):
        recognize_pattern(None)
