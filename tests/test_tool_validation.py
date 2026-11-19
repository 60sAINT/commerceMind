"""T2：JSON Schema 参数校验。

验证参数在进入 handler 之前被校验、错误结构可被模型修复，
且参数错误不执行工具、不重试、不影响熔断。
"""
from __future__ import annotations

import json

import pytest

from tooling.errors import ToolErrorCode, ToolValidationError
from tooling.tool_manager import (
    CircuitState,
    ToolContext,
    ToolManager,
    ToolSpec,
    validate_params,
)

VALID_ORDER = "A1001-001"

ORDER_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {
            "type": "string",
            "minLength": 8,
            "maxLength": 20,
            "pattern": "^[A-Za-z0-9-]+$",
        },
        "status": {"type": "string", "enum": ["paid", "shipped"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    "required": ["order_id"],
    "additionalProperties": False,
}

NESTED_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def make_context() -> ToolContext:
    return ToolContext(user_id="u1", session_id="s1", request_id="r1")


def make_spec(schema: dict | None = None, handler=None, **overrides) -> ToolSpec:
    if handler is None:
        handler = lambda params, context: params  # noqa: E731
    values = {
        "name": "lookup_order",
        "description": "按订单号查询订单；只读。",
        "input_schema": schema or ORDER_SCHEMA,
        "handler": handler,
    }
    values.update(overrides)
    return ToolSpec(**values)


def hint_size(hint: dict) -> int:
    return len(json.dumps(hint, ensure_ascii=False, separators=(",", ":")))


def invalid_params(spec: ToolSpec, params) -> ToolValidationError:
    with pytest.raises(ToolValidationError) as excinfo:
        validate_params(spec, params)
    return excinfo.value


# ── 参数结构与规范化 ──────────────────────────────────────────────────────────

def test_validate_params_returns_normalized_plain_dict():
    params = {"order_id": "  A1001-001  ", "limit": 3}
    spec = make_spec()

    arguments = validate_params(spec, params)

    assert arguments == {"order_id": VALID_ORDER, "limit": 3}
    assert isinstance(arguments, dict)
    assert arguments is not params
    assert params["order_id"] == "  A1001-001  "


def test_validate_params_strips_nested_strings():
    spec = make_spec(NESTED_SCHEMA)

    arguments = validate_params(spec, {"items": [{"sku": "  SKU-1  "}]})

    assert arguments == {"items": [{"sku": "SKU-1"}]}


@pytest.mark.parametrize("params", [None, [], "{}", 42, 3.14, object()])
def test_non_object_params_are_rejected(params):
    exc = invalid_params(make_spec(), params)

    assert exc.code is ToolErrorCode.INVALID_ARGUMENTS
    assert exc.retryable is False
    assert exc.validator == "type"
    assert "JSON 对象" in exc.message


def test_valid_params_pass_without_type_coercion():
    spec = make_spec()

    arguments = validate_params(spec, {"order_id": VALID_ORDER})
    assert arguments["order_id"] == VALID_ORDER

    exc = invalid_params(spec, {"order_id": VALID_ORDER, "limit": "3"})
    assert exc.validator == "type"
    assert "integer" in exc.message


# ── 错误结构 ──────────────────────────────────────────────────────────────────

def test_missing_required_field_reports_path_and_name():
    exc = invalid_params(make_spec(), {})

    assert exc.code is ToolErrorCode.INVALID_ARGUMENTS
    assert exc.validator == "required"
    assert exc.path == "order_id"
    assert "order_id" in exc.message


def test_wrong_type_reports_path():
    exc = invalid_params(make_spec(), {"order_id": 123})

    assert exc.validator == "type"
    assert exc.path == "order_id"
    assert "order_id" in exc.message


def test_extra_field_is_rejected():
    exc = invalid_params(make_spec(), {"order_id": VALID_ORDER, "debug": True})

    assert exc.validator == "additionalProperties"
    assert exc.path == "debug"
    assert "debug" in exc.message


def test_enum_out_of_range_reports_allowed_values():
    exc = invalid_params(make_spec(), {"order_id": VALID_ORDER, "status": "cancelled"})

    assert exc.validator == "enum"
    assert exc.path == "status"
    assert "paid" in exc.message
    assert "shipped" in exc.message


@pytest.mark.parametrize(
    ("limit", "validator"),
    [(0, "minimum"), (9, "maximum")],
)
def test_numeric_range(limit, validator):
    exc = invalid_params(make_spec(), {"order_id": VALID_ORDER, "limit": limit})

    assert exc.validator == validator
    assert exc.path == "limit"


def test_string_length_and_pattern():
    exc = invalid_params(make_spec(), {"order_id": "A1"})
    assert exc.validator == "minLength"
    assert exc.path == "order_id"

    exc = invalid_params(make_spec(), {"order_id": "ABCDEFG!"})
    assert exc.validator == "pattern"
    assert exc.path == "order_id"


def test_nested_array_item_type_reports_index_path():
    exc = invalid_params(make_spec(NESTED_SCHEMA), {"items": [{"sku": "A1"}, {"sku": 2}]})

    assert exc.validator == "type"
    assert exc.path == "items.1.sku"
    assert "items.1.sku" in exc.message


def test_nested_missing_field_reports_index_path():
    exc = invalid_params(make_spec(NESTED_SCHEMA), {"items": [{}]})

    assert exc.validator == "required"
    assert exc.path == "items.0.sku"


def test_nested_extra_field_reports_index_path():
    exc = invalid_params(make_spec(NESTED_SCHEMA), {"items": [{"sku": "A1", "color": "red"}]})

    assert exc.validator == "additionalProperties"
    assert exc.path == "items.0.color"


def test_invalid_schema_is_rejected_at_construction():
    schema = {"type": "object", "properties": {"order_id": {"type": "nonsense"}}}

    with pytest.raises(ValueError, match="JSON Schema"):
        make_spec(schema)


# ── 执行链路 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_params_never_call_handler():
    calls: list[dict] = []

    def handler(params, context):
        calls.append(params)
        return {"ok": True}

    manager = ToolManager()
    manager.register(make_spec(handler=handler))

    result = await manager.execute("lookup_order", {}, make_context())

    assert result.success is False
    assert result.usable is False
    assert result.error_code == ToolErrorCode.INVALID_ARGUMENTS.value
    assert result.retryable is False
    assert result.attempts == 0
    assert calls == []


@pytest.mark.asyncio
async def test_invalid_params_update_only_invalid_counter():
    manager = ToolManager()
    manager.register(make_spec())

    await manager.execute("lookup_order", {"order_id": 123}, make_context())

    stats = manager.get_stats()["lookup_order"]
    assert stats["invalid"] == 1
    assert stats["total"] == 0
    assert stats["attempts"] == 0
    assert stats["success"] == 0
    assert stats["failed"] == 0
    assert stats["consecutive_fails"] == 0


@pytest.mark.asyncio
async def test_repeated_invalid_params_do_not_open_breaker():
    manager = ToolManager()
    manager.register(make_spec())

    for _ in range(6):
        await manager.execute("lookup_order", {}, make_context())

    registered = manager._tools["lookup_order"]
    assert registered.breaker.state is CircuitState.CLOSED
    assert manager.get_stats()["lookup_order"]["invalid"] == 6


@pytest.mark.asyncio
async def test_handler_receives_normalized_arguments():
    captured: dict = {}

    def handler(params, context):
        captured.update(params)
        return {"ok": True}

    manager = ToolManager()
    manager.register(make_spec(handler=handler))

    result = await manager.execute(
        "lookup_order",
        {"order_id": "  A1001-001  ", "limit": 2},
        make_context(),
    )

    assert result.success is True
    assert captured == {"order_id": VALID_ORDER, "limit": 2}


# ── schema hint ───────────────────────────────────────────────────────────────

def test_schema_hint_contains_required_and_field_constraints():
    manager = ToolManager()
    manager.register(make_spec())

    hint = manager.schema_hint("lookup_order")

    assert hint["type"] == "object"
    assert hint["required"] == ["order_id"]
    assert hint["additionalProperties"] is False
    assert hint["properties"]["order_id"]["type"] == "string"
    assert hint["properties"]["order_id"]["pattern"] == "^[A-Za-z0-9-]+$"
    assert hint["properties"]["status"]["enum"] == ["paid", "shipped"]
    assert hint["properties"]["limit"]["minimum"] == 1
    assert "description" not in json.dumps(hint, ensure_ascii=False)


def test_schema_hint_returns_none_for_unknown_tool():
    manager = ToolManager()

    assert manager.schema_hint("missing") is None


def test_schema_hint_accepts_spec_directly():
    manager = ToolManager()
    spec = make_spec()
    manager.register(spec)

    assert manager.schema_hint(spec) == manager.schema_hint(spec.name)


def test_schema_hint_respects_budget_and_drops_details():
    schema = {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "pattern": "^" + "A" * 400 + "$",
                "minLength": 8,
            },
            "status": {"type": "string", "enum": ["paid", "shipped"]},
        },
        "required": ["order_id"],
        "additionalProperties": False,
    }
    manager = ToolManager(max_schema_hint_chars=150)
    manager.register(make_spec(schema))

    hint = manager.schema_hint("lookup_order")

    assert hint_size(hint) <= 150
    assert hint["required"] == ["order_id"]
    assert hint["properties"]["order_id"] == {}
    assert hint["properties"]["status"] == {}


def test_schema_hint_truncates_when_fields_exceed_budget():
    schema = {
        "type": "object",
        "properties": {
            f"very_long_field_name_{index}": {"type": "string"}
            for index in range(20)
        },
        "required": [],
        "additionalProperties": False,
    }
    manager = ToolManager(max_schema_hint_chars=128)
    manager.register(make_spec(schema))

    hint = manager.schema_hint("lookup_order")

    assert hint_size(hint) <= 128
    assert hint["truncated"] is True
    assert hint["required"] == []


def test_fork_keeps_schema_hint_budget():
    schema = {
        "type": "object",
        "properties": {
            "order_id": {
                "type": "string",
                "pattern": "^" + "A" * 400 + "$",
            }
        },
        "required": ["order_id"],
        "additionalProperties": False,
    }
    manager = ToolManager(max_schema_hint_chars=180)
    manager.register(make_spec(schema))

    forked = manager.fork()
    hint = forked.schema_hint("lookup_order")

    assert hint_size(hint) <= 180


def test_schema_hint_budget_is_validated():
    with pytest.raises(ValueError):
        ToolManager(max_schema_hint_chars=127)
    with pytest.raises(ValueError):
        ToolManager(max_schema_hint_chars=4001)
    with pytest.raises(ValueError):
        ToolManager(max_schema_hint_chars=True)
