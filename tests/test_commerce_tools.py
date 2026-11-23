"""对照 EchoMind 的工具契约和可观察业务行为。"""
import pytest

from core.config import ToolSettings
from tooling import ToolContext, ToolManager, ToolResult, build_commerce_tools


EXPECTED = {
    "inspect_request_context", "suggest_required_fields", "lookup_error_code",
    "build_diagnostic_plan", "check_billing_fields", "compare_amounts",
    "create_handoff_summary", "search_knowledge_base",
}


def make_manager(**kwargs):
    manager = ToolManager()
    for spec in build_commerce_tools(**kwargs).values():
        manager.register(spec)
    return manager


def context(**kwargs):
    return ToolContext("u", "s", "r", **kwargs)


def test_tool_catalog_and_readonly_request_scoped_contract():
    tools = build_commerce_tools(settings=ToolSettings(timeout_s=2, max_retries=0))
    assert set(tools) == EXPECTED
    assert tools["compare_amounts"].input_schema["required"] == ["amount_a", "amount_b"]
    for spec in tools.values():
        assert spec.retry_safe and spec.cache_ttl_s == 0
        assert (spec.timeout_s, spec.max_retries) == (2, 0)
        assert spec.input_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_request_snapshot_uses_current_context_not_previous_request():
    manager = make_manager()
    original = {"order_id": ["A1001"]}
    ctx = context(
        intent="order_status", intent_group="general", urgency="HIGH",
        intent_confidence=0.87654, context_available=True, entities=original,
    )
    result = await manager.execute("inspect_request_context", {"focus": "orders"}, ctx)
    assert result.data == {
        "intent": "order_status", "intent_group": "general", "urgency": "HIGH",
        "intent_confidence": 0.8765, "entities": original, "context_available": True,
        "requested_focus": "orders",
    }
    result.data["entities"]["order_id"].append("changed")
    assert ctx.entities == original
    other = await manager.execute("inspect_request_context", {}, context())
    assert other.data["intent"] is None and not other.cached


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,fields", [
    ("order_status", ["订单号或下单时间"]),
    ("logistics", ["订单号或下单时间"]),
    ("account", ["登录方式或账号标识", "问题发生时间"]),
    ("complaint", ["事件时间", "期望处理方式"]),
    (None, ["希望解决的具体问题"]),
    ("refund", []),
])
async def test_suggest_fields_matches_echomind_rules(intent, fields):
    result = await make_manager().execute("suggest_required_fields", {}, context(intent=intent))
    assert result.data["required_fields"] == fields


@pytest.mark.asyncio
@pytest.mark.parametrize("code,meaning", [
    ("401", "认证失败"), ("403", "权限不足"), ("404", "资源或路径不存在"),
    ("500", "服务端处理异常"), ("unknown", "暂未识别的错误码"),
])
async def test_error_code_guidance_does_not_claim_logs_were_checked(code, meaning):
    result = await make_manager().execute("lookup_error_code", {"error_code": code}, context())
    assert result.data["meaning"] == meaning
    assert result.data["server_log_checked"] is False


@pytest.mark.asyncio
async def test_diagnostics_billing_and_handoff_do_not_perform_external_actions():
    manager = make_manager()
    ctx = context(intent="refund", urgency="HIGH", entities={"order_id": "A1001"})
    diagnostic = await manager.execute("build_diagnostic_plan", {"environment": "Docker", "reproduced": True}, ctx)
    assert len(diagnostic.data["diagnostic_steps"]) == 4
    billing = await manager.execute("check_billing_fields", {"payment_channel": "微信"}, ctx)
    assert billing.data["missing_fields"] == ["amount", "date"]
    assert billing.data["can_confirm_refund"] is False
    summary = await manager.execute("create_handoff_summary", {"reason": "需要核验"}, ctx)
    assert summary.data["request_id"] == "r"
    assert summary.data["urgency"] == "HIGH"
    assert summary.data["sensitive_data_required"] is False
    assert "ticket_id" not in summary.data


@pytest.mark.asyncio
async def test_amount_schema_and_numeric_result_match_echomind():
    manager = make_manager()
    result = await manager.execute("compare_amounts", {"amount_a": 0.3, "amount_b": 0.1}, context())
    assert result.data == {
        "success": True, "amount_a": 0.3, "amount_b": 0.1, "difference": 0.2,
        "interpretation": "仅表示金额差值，不代表重复扣款或退款结论",
    }
    for amount in [float("nan"), float("inf"), "1", True]:
        invalid = await manager.execute("compare_amounts", {"amount_a": amount, "amount_b": 0}, context())
        assert invalid.error_code == "invalid_arguments"


@pytest.mark.asyncio
async def test_shared_search_reports_unconfigured_backend():
    result = await make_manager().execute("search_knowledge_base", {"query": "退款规则"}, context())
    assert result.data == {"success": False, "error": "RAG 工具未初始化", "results": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_shared_search_uses_injected_backend_and_propagates_status(success):
    async def search(query, top_k, ctx):
        assert (query, top_k, ctx.user_id) == ("退款规则", 3, "u")
        return ToolResult(
            tool_name="knowledge_search", success=success, data=[{"content": "规则"}],
            error=None if success else "检索失败", cached=True, reranked=True,
        )

    result = await make_manager(knowledge_search=search).execute(
        "search_knowledge_base", {"query": "退款规则", "top_k": 3}, context(),
    )
    assert result.data["success"] is success
    assert result.data["reranked"] is success
    assert result.data.get("results") == ([{"content": "规则"}] if success else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("name,params", [
    ("lookup_error_code", {}),
    ("build_diagnostic_plan", {"environment": "Docker", "reproduced": "true"}),
    ("inspect_request_context", {"user_id": "another"}),
    ("search_knowledge_base", {"query": "x", "top_k": 21}),
])
async def test_schema_errors_do_not_invoke_handler(name, params):
    result = await make_manager().execute(name, params, context())
    assert result.error_code == "invalid_arguments" and result.attempts == 0
