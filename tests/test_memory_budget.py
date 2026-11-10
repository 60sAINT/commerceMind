import pytest

from memory.budget import DEFAULT_COMPACT_TRIGGER_TOKENS, TokenBudget
from tooling.memory_budget_report import build_report


@pytest.fixture(autouse=True)
def clean_budget_environment(monkeypatch):
    monkeypatch.delenv("COMMERCEMIND_MEMORY_COMPACT_TRIGGER_TOKENS", raising=False)


def test_default_threshold_and_boundaries():
    budget = TokenBudget()
    assert budget.compact_trigger_tokens == DEFAULT_COMPACT_TRIGGER_TOKENS == 10_800
    assert not budget.should_compact(10_799)
    assert budget.should_compact(10_800)
    assert budget.should_compact(10_801)
    assert not budget.should_compact(0)


def test_trigger_env(monkeypatch):
    monkeypatch.setenv("COMMERCEMIND_MEMORY_COMPACT_TRIGGER_TOKENS", "5000")
    budget = TokenBudget.from_env()
    assert budget.compact_trigger_tokens == 5_000
    assert not budget.should_compact(4_999)
    assert budget.should_compact(5_000)


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_threshold(value):
    with pytest.raises(ValueError):
        TokenBudget(compact_trigger_tokens=value)


def test_invalid_env(monkeypatch):
    monkeypatch.setenv("COMMERCEMIND_MEMORY_COMPACT_TRIGGER_TOKENS", "bad")
    with pytest.raises(ValueError):
        TokenBudget.from_env()
    monkeypatch.setenv("COMMERCEMIND_MEMORY_COMPACT_TRIGGER_TOKENS", "0")
    with pytest.raises(ValueError):
        TokenBudget.from_env()


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_invalid_runtime_count(value):
    with pytest.raises(ValueError):
        TokenBudget().should_compact(value)


def test_report_acceptance():
    report = build_report()
    assert report["compact_trigger_tokens"] == 10_800
    assert report["samples"][0]["message_count"] == 20
    assert report["samples"][0]["should_compact"] is False
    assert report["samples"][1]["message_count"] == 6
    assert report["samples"][1]["should_compact"] is True
    assert report["samples"][2]["working_tokens"] > 0
