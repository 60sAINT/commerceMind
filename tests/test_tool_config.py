"""T0：工具管理器配置与开关。

只验证配置契约，不创建工具、不调用模型。
"""
from __future__ import annotations

import pytest

from core.config import Settings, ToolSettings


def test_tool_settings_defaults_are_safe_and_disabled():
    settings = ToolSettings()

    assert settings.enabled is False
    assert settings.trigger == "auto"
    assert settings.max_rounds == 2
    assert settings.max_calls_per_round == 4
    assert settings.max_calls_total == 6
    assert settings.phase_timeout_s == 15.0
    assert settings.timeout_s == 3.0
    assert settings.max_retries == 1
    assert settings.retry_backoff_s == 0.1
    assert settings.circuit_failure_threshold == 5
    assert settings.circuit_recovery_s == 60.0
    assert settings.cache_ttl_s == 30.0
    assert settings.cache_max_entries == 1024
    assert settings.max_result_chars == 4000
    assert settings.max_schema_hint_chars == 800
    assert settings.rerank_enabled is False
    assert settings.rerank_top_k == 5
    assert settings.stream_events is False
    assert settings.debug is False
    assert settings.protocol_fallback is True
    assert settings.demo_orders_path == "data/demo_orders.json"


def test_tool_settings_from_env_reads_all_switches(monkeypatch):
    values = {
        "ENABLED": "true",
        "TRIGGER": "ALWAYS",
        "MAX_ROUNDS": "3",
        "MAX_CALLS_PER_ROUND": "5",
        "MAX_CALLS_TOTAL": "8",
        "PHASE_TIMEOUT_S": "12.5",
        "TIMEOUT_S": "2.5",
        "MAX_RETRIES": "0",
        "RETRY_BACKOFF_S": "0",
        "CIRCUIT_FAILURE_THRESHOLD": "4",
        "CIRCUIT_RECOVERY_S": "30",
        "CACHE_TTL_S": "0",
        "CACHE_MAX_ENTRIES": "64",
        "MAX_RESULT_CHARS": "2048",
        "MAX_SCHEMA_HINT_CHARS": "512",
        "RERANK_ENABLED": "true",
        "RERANK_TOP_K": "3",
        "STREAM_EVENTS": "true",
        "DEBUG": "true",
        "PROTOCOL_FALLBACK": "false",
        "DEMO_ORDERS": "/tmp/orders.json",
    }
    for key, value in values.items():
        monkeypatch.setenv("COMMERCEMIND_TOOLS_" + key, value)

    settings = ToolSettings.from_env()

    assert settings.enabled is True
    assert settings.trigger == "always"
    assert settings.max_rounds == 3
    assert settings.max_calls_per_round == 5
    assert settings.max_calls_total == 8
    assert settings.phase_timeout_s == 12.5
    assert settings.timeout_s == 2.5
    # 0 是合法值，不能被“正数回退”逻辑吞掉。
    assert settings.max_retries == 0
    assert settings.retry_backoff_s == 0.0
    assert settings.cache_ttl_s == 0.0
    assert settings.circuit_failure_threshold == 4
    assert settings.circuit_recovery_s == 30.0
    assert settings.cache_max_entries == 64
    assert settings.max_result_chars == 2048
    assert settings.max_schema_hint_chars == 512
    assert settings.rerank_enabled is True
    assert settings.rerank_top_k == 3
    assert settings.stream_events is True
    assert settings.debug is True
    assert settings.protocol_fallback is False
    assert settings.demo_orders_path == "/tmp/orders.json"


def test_tool_settings_invalid_env_values_fall_back(monkeypatch):
    monkeypatch.setenv("COMMERCEMIND_TOOLS_TRIGGER", "sometimes")
    monkeypatch.setenv("COMMERCEMIND_TOOLS_MAX_ROUNDS", "not-a-number")
    monkeypatch.setenv("COMMERCEMIND_TOOLS_MAX_RETRIES", "-1")
    monkeypatch.setenv("COMMERCEMIND_TOOLS_CACHE_TTL_S", "-2")
    monkeypatch.setenv("COMMERCEMIND_TOOLS_TIMEOUT_S", "0")
    monkeypatch.setenv("COMMERCEMIND_TOOLS_DEMO_ORDERS", "  ")

    settings = ToolSettings.from_env()

    assert settings.trigger == "auto"
    assert settings.max_rounds == 2
    assert settings.max_retries == 1
    assert settings.cache_ttl_s == 30.0
    assert settings.timeout_s == 3.0
    assert settings.demo_orders_path == "data/demo_orders.json"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trigger": "sometimes"},
        {"max_rounds": 0},
        {"max_calls_per_round": 0},
        {"max_calls_total": 0},
        {"circuit_failure_threshold": 0},
        {"cache_max_entries": 0},
        {"rerank_top_k": 0},
        {"max_retries": -1},
        {"phase_timeout_s": 0},
        {"timeout_s": 0},
        {"circuit_recovery_s": 0},
        {"retry_backoff_s": -0.1},
        {"cache_ttl_s": -1},
        {"timeout_s": 20, "phase_timeout_s": 10},
        {"max_calls_per_round": 10, "max_calls_total": 6},
        {"max_result_chars": 100},
        {"max_result_chars": 20001},
        {"max_schema_hint_chars": 100},
        {"max_schema_hint_chars": 4001},
        {"demo_orders_path": "  "},
    ],
)
def test_tool_settings_validate_invalid_values(kwargs):
    with pytest.raises(ValueError):
        ToolSettings(**kwargs)


def test_settings_defaults_to_disabled_tools():
    settings = Settings(api_key="test-key")

    assert settings.tools.enabled is False
    assert settings.tools == ToolSettings()


def test_settings_from_env_attaches_tool_switch(monkeypatch):
    monkeypatch.setenv("COMMERCEMIND_TOOLS_ENABLED", "true")
    monkeypatch.setenv("COMMERCEMIND_TOOLS_TRIGGER", "always")

    settings = Settings.from_env()

    assert settings.tools.enabled is True
    assert settings.tools.trigger == "always"
