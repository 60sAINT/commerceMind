"""与 EchoMind agents/tools.py 对齐的内置客服工具，不连接订单或支付系统。"""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import Any, Awaitable, Callable

from core.config import ToolSettings
from tooling.errors import ToolError, ToolErrorCode
from tooling.tool_manager import ToolContext, ToolResult, ToolSpec


def make_tool(name, description, properties, handler, required=None) -> ToolSpec:
    def adapted(params, context):
        return handler(context, params)

    return ToolSpec(
        name=name, description=description,
        input_schema={"type": "object", "properties": properties,
                      "required": required or [], "additionalProperties": False},
        handler=adapted, retry_safe=True, cache_ttl_s=0,
    )


def inspect_request_context(req: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """通用客服工具：返回脱敏后的当前请求快照。"""
    return {
        "intent": req.intent if req.intent else None,
        "intent_group": req.intent_group,
        "urgency": req.urgency if req.urgency else None,
        "intent_confidence": round(req.intent_confidence, 4),
        "entities": copy.deepcopy(dict(req.entities)),
        "context_available": req.context_available,
        "requested_focus": str(args.get("focus", "general"))[:40],
    }


def suggest_required_fields(req: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """通用客服工具：按业务类型计算下一轮只需询问的字段。"""
    intent = req.intent if req.intent else "other"
    fields: list[str] = []
    if intent in {"order_status", "logistics"}:
        fields = ["订单号或下单时间"]
    elif intent in {"account", "account_security"}:
        fields = ["登录方式或账号标识", "问题发生时间"]
    elif intent in {"complaint", "request"}:
        fields = ["事件时间", "期望处理方式"]
    elif intent == "other":
        fields = ["希望解决的具体问题"]
    return {
        "intent": intent,
        "required_fields": fields,
        "known_entities": copy.deepcopy(dict(req.entities)),
    }


def lookup_error_code(req: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """技术工具：解释常见错误码的排查方向，不声称读取了服务端日志。"""
    code = str(args.get("error_code", "")).upper().strip()
    mapping = {
        "401": ("认证失败", ["确认 Token/API Key 是否过期", "确认请求时间戳和签名", "确认账号登录状态"]),
        "403": ("权限不足", ["确认账号或套餐权限", "确认资源权限和 IP 白名单"]),
        "404": ("资源或路径不存在", ["确认接口路径和环境", "确认资源标识是否正确"]),
        "500": ("服务端处理异常", ["记录 request_id 和发生时间", "检查依赖服务、参数格式和服务端日志"]),
    }
    meaning, steps = mapping.get(
        code,
        ("暂未识别的错误码", ["补充完整错误信息、发生时间和运行环境"]),
    )
    return {
        "error_code": code,
        "meaning": meaning,
        "next_steps": steps,
        "server_log_checked": False,
    }


def build_diagnostic_plan(req: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """技术工具：生成低风险排障顺序。"""
    environment = str(args.get("environment", "unknown"))[:80]
    reproduced = bool(args.get("reproduced", False))
    steps = [
        "复现并记录完整错误信息",
        "确认网络、DNS、代理和证书",
        "确认版本、配置和权限",
    ]
    if reproduced:
        steps.append("用最小请求复现并记录 request_id")
    return {
        "environment": environment,
        "reproduced": reproduced,
        "diagnostic_steps": steps,
    }


def check_billing_fields(req: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """账单工具：检查必要核验字段是否齐全。"""
    fields = {
        "order_id": bool(req.entities.get("order_id")),
        "amount": bool(req.entities.get("amount")),
        "date": bool(req.entities.get("date")),
        "payment_channel": bool(args.get("payment_channel")),
    }
    return {
        "fields": fields,
        "missing_fields": [name for name, present in fields.items() if not present],
        "can_confirm_refund": False,
        "reason": "当前工具只做字段检查，不连接订单或支付系统",
    }


def compare_amounts(req: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """账单工具：只做用户明确提供金额之间的算术。"""
    try:
        first = float(args["amount_a"])
        second = float(args["amount_b"])
    except (KeyError, TypeError, ValueError):
        return {"success": False, "error": "amount_a 和 amount_b 必须是数字"}
    if not math.isfinite(first) or not math.isfinite(second) or not math.isfinite(first - second):
        raise ToolError("金额必须是有限数字", code=ToolErrorCode.INVALID_ARGUMENTS, retryable=False)
    return {
        "success": True,
        "amount_a": first,
        "amount_b": second,
        "difference": round(first - second, 2),
        "interpretation": "仅表示金额差值，不代表重复扣款或退款结论",
    }


def create_handoff_summary(req: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """升级工具：生成可交给人工客服的结构化摘要。"""
    return {
        "request_id": req.request_id,
        "reason": str(args.get("reason", "需要人工客服继续核验"))[:120],
        "intent": req.intent if req.intent else "unknown",
        "urgency": req.urgency if req.urgency else "UNKNOWN",
        "entities": copy.deepcopy(dict(req.entities)),
        "sensitive_data_required": False,
    }


def general_tools() -> dict[str, ToolSpec]:
    return {
        "inspect_request_context": make_tool(
            "inspect_request_context",
            "查看当前请求的意图、紧急度、实体和上下文可用性；不查询外部业务系统。",
            {"focus": {"type": "string", "description": "希望关注的业务方向"}},
            inspect_request_context,
        ),
        "suggest_required_fields": make_tool(
            "suggest_required_fields",
            "根据当前意图建议下一轮只需向用户补充的字段。",
            {},
            suggest_required_fields,
        ),
    }


def technical_tools() -> dict[str, ToolSpec]:
    return {
        "lookup_error_code": make_tool(
            "lookup_error_code",
            "解释常见 HTTP 错误码的可能含义和低风险排查方向；不会读取服务端日志。",
            {"error_code": {"type": "string", "description": "例如 401、403、500"}},
            lookup_error_code,
            required=["error_code"],
        ),
        "build_diagnostic_plan": make_tool(
            "build_diagnostic_plan",
            "根据运行环境和是否可复现生成排障顺序，不执行修改配置等操作。",
            {
                "environment": {"type": "string", "description": "App、浏览器、服务端或 Docker 等"},
                "reproduced": {"type": "boolean", "description": "问题是否可以稳定复现"},
            },
            build_diagnostic_plan,
            required=["environment", "reproduced"],
        ),
    }


def billing_tools() -> dict[str, ToolSpec]:
    return {
        "check_billing_fields": make_tool(
            "check_billing_fields",
            "检查账单核验字段是否齐全；不连接订单、支付或退款系统。",
            {"payment_channel": {"type": "string", "description": "支付渠道，例如微信、支付宝、银行卡"}},
            check_billing_fields,
        ),
        "compare_amounts": make_tool(
            "compare_amounts",
            "计算用户明确提供的两笔金额差值；不判断是否重复扣款，也不执行退款。",
            {
                "amount_a": {"type": "number", "description": "第一笔金额"},
                "amount_b": {"type": "number", "description": "第二笔金额"},
            },
            compare_amounts,
            required=["amount_a", "amount_b"],
        ),
    }


def escalation_tools() -> dict[str, ToolSpec]:
    return {
        "create_handoff_summary": make_tool(
            "create_handoff_summary",
            "生成交给人工客服的结构化交接摘要，不会创建真实工单。",
            {"reason": {"type": "string", "description": "需要升级的原因"}},
            create_handoff_summary,
        ),
    }


KnowledgeSearch = Callable[[str, int, ToolContext], Awaitable[ToolResult]]


def build_shared_rag_tools(search: KnowledgeSearch | None = None) -> dict[str, ToolSpec]:
    """保留 EchoMind 的共享检索工具；检索实现由应用注入，不内置向量 RAG。"""
    async def search_knowledge_base(req: ToolContext, args: dict) -> dict:
        query = args["query"]
        top_k = args.get("top_k", 5)
        if search is None:
            return {"success": False, "error": "RAG 工具未初始化", "results": []}
        result = await search(query, top_k, req)
        if not result.success:
            return {
                "success": False, "query": query,
                "error": result.error or "知识库检索失败", "results": [], "reranked": False,
            }
        return {
            "success": True, "query": query, "top_k": top_k, "results": copy.deepcopy(result.data),
            "cached": result.cached, "reranked": result.reranked,
        }

    return {
        "search_knowledge_base": make_tool(
            "search_knowledge_base",
            "检索知识库并返回最相关的文档片段；可用于通用、技术、账单和升级场景。",
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000, "description": "用户问题或检索关键词"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "description": "返回结果条数"},
            },
            search_knowledge_base,
            required=["query"],
        ),
    }


def build_commerce_tools(
    *,
    settings: ToolSettings | None = None,
    knowledge_search: KnowledgeSearch | None = None,
) -> dict[str, ToolSpec]:
    """汇总 EchoMind 的 7 个客服工具和 1 个共享检索工具。"""
    settings = settings or ToolSettings()
    tools = {
        **general_tools(), **technical_tools(), **billing_tools(), **escalation_tools(),
        **build_shared_rag_tools(knowledge_search),
    }
    # 请求快照和交接摘要依赖当前请求，不能缓存到下一条消息。
    return {
        name: replace(
            spec, timeout_s=settings.timeout_s, max_retries=settings.max_retries,
            retry_backoff_s=settings.retry_backoff_s,
        )
        for name, spec in tools.items()
    }
