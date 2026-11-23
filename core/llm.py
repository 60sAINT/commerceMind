"""最小 LLM 客户端封装。

支持 Anthropic 兼容的文本对话和带工具的结构化消息。

- 统一超时：SDK 超时 + `asyncio.timeout` 双保险。
- 统一错误：把 Anthropic SDK 的异常转换成带 HTTP 状态码的 `LLMError`，
  避免把裸异常和上游错误细节直接暴露给调用方。
- 统一取文本：兼容 Anthropic 的 content block，也兼容测试中的 dict。
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

import anthropic
from anthropic import AsyncAnthropic

from core.config import Settings

SYSTEM_PROMPT = (
    "你是 CommerceMind 的电商客服助手。"
    "请用简洁、准确、友好的中文回答用户问题；"
    "不确定的信息要明确说明，不要编造订单、退款或物流状态。"
)


class LLMError(Exception):
    """对外可安全展示的模型调用错误。"""

    def __init__(self, message: str, *, status_code: int = 503, code: str = "llm_unavailable") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True, slots=True)
class ChatResult:
    """一次模型调用的文本和 usage；上游不返回 usage 时为 None。"""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MessageTurn:
    """模型的一轮回复。content 可直接回传为 assistant 消息。"""

    content: list[dict[str, Any]]
    text: str
    tool_uses: tuple[ToolUse, ...]
    stop_reason: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _tool_options(
    tools: Sequence[dict[str, Any]] | None, tool_choice: dict[str, Any] | None,
) -> dict[str, Any]:
    if not tools:
        if tool_choice is not None:
            raise ValueError("tool_choice 需要同时提供 tools")
        return {}
    options: dict[str, Any] = {"tools": list(tools)}
    if tool_choice is not None:
        options["tool_choice"] = tool_choice
    return options


def _tools_unsupported(exc: Exception, tools: Any) -> bool:
    """只识别明确的协议能力拒绝，普通参数、鉴权或服务故障不能触发回退。"""
    if not tools or getattr(exc, "status_code", None) not in {400, 422, 501}:
        return False
    body = getattr(exc, "body", None)
    detail = (str(getattr(exc, "message", "")) + " " + str(body)).lower()
    if any(marker in detail for marker in ("input_schema", "input schema", "tool_use_id", "tools[", "tools.")):
        return False
    if "tool_choice" in detail and "none" in detail and any(
        marker in detail for marker in ("input should be", "must be one of", "invalid enum", "cannot be")
    ):
        return True
    return any(key in detail for key in (
        "tools", "tool_choice", "tool use", "tool_use", "function calling", "function_calling", "function calls",
    )) and any(
        marker in detail for marker in (
            "not supported", "unsupported", "does not support", "not support",
            "unrecognized", "unknown field", "unknown parameter", "unknown argument",
            "extra inputs are not permitted", "not implemented", "不支持",
            "unexpected keyword", "tools are not allowed", "tool_choice is not allowed",
        )
    )


def _parse_message_turn(response: Any) -> MessageTurn:
    content: list[dict[str, Any]] = []
    tool_uses: list[ToolUse] = []
    ids: set[str] = set()
    stop_reason = _field(response, "stop_reason")
    raw = _field(response, "content")
    if not isinstance(raw, (list, tuple)):
        raise LLMError("模型返回了无效的消息内容", status_code=502, code="tool_protocol_error")

    for block in raw:
        if isinstance(block, Mapping):
            payload = deepcopy(dict(block))
        elif callable(getattr(block, "model_dump", None)):
            payload = block.model_dump(mode="json", exclude_unset=True)
        else:
            # 兼容只暴露属性的上游适配器和测试替身。
            payload = vars(block).copy() if hasattr(block, "__dict__") else {}
        kind = payload.get("type")
        if not isinstance(kind, str) or not kind:
            raise LLMError("模型返回了无效的消息块", status_code=502, code="tool_protocol_error")
        if kind == "tool_use":
            call_id, name, arguments = (payload.get(key) for key in ("id", "name", "input"))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            if (
                not isinstance(call_id, str) or not call_id.strip() or call_id in ids
                or not isinstance(name, str) or not name.strip()
                or not isinstance(arguments, dict)
                or stop_reason == "max_tokens"
            ):
                raise LLMError("模型返回了无效的工具调用", status_code=502, code="tool_protocol_error")
            try:
                json.dumps(arguments, allow_nan=False)
            except (ValueError, TypeError, RecursionError):
                raise LLMError("工具参数不是合法 JSON", status_code=502, code="tool_protocol_error") from None
            ids.add(call_id)
            payload["input"] = deepcopy(arguments)
            tool_uses.append(ToolUse(call_id, name, deepcopy(arguments)))
        content.append(payload)

    text = extract_text_content(content)
    if stop_reason == "tool_use" and not tool_uses:
        raise LLMError("模型未返回工具调用块", status_code=502, code="tool_protocol_error")
    if tool_uses and stop_reason != "tool_use":
        raise LLMError("工具调用的停止原因非法", status_code=502, code="tool_protocol_error")
    if not text and not tool_uses:
        raise LLMError("模型返回了空内容，请稍后重试", status_code=502, code="llm_empty_response")
    return MessageTurn(content, text, tuple(tool_uses), stop_reason, *_usage_tokens(_field(response, "usage")))


def _build_messages(
    message: str,
    history: Sequence[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    """把历史消息和当前问题拼成 Anthropic messages；system 不放在这里。"""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message 不能为空")

    messages: list[dict[str, str]] = []
    for item in history or ():
        if not isinstance(item, dict):
            raise ValueError("history 的每一项必须是字典")
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant"):
            raise ValueError("history 只支持 user / assistant 角色")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("history 的 content 不能为空")
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    return messages


def _usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    if usage is None:
        return None, None
    if isinstance(usage, dict):
        input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    else:
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
    return (
        input_tokens if isinstance(input_tokens, int) else None,
        output_tokens if isinstance(output_tokens, int) else None,
    )


def extract_text_content(content: Iterable[Any], separator: str = "\n") -> str:
    """从 Anthropic 风格的 content 中提取文本块。"""
    texts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
            continue

        block_type = getattr(block, "type", None)
        text = getattr(block, "text", None)
        if isinstance(block, dict):
            block_type = block.get("type", block_type)
            text = block.get("text", text)

        if isinstance(text, str) and block_type in (None, "text"):
            texts.append(text)

    return separator.join(t for t in texts if t).strip()


class LLMClient:
    """Anthropic / Anthropic 兼容接口的最小异步客户端。"""

    def __init__(self, settings: Settings) -> None:
        if not settings.model_configured:
            raise ValueError("未配置 ANTHROPIC_API_KEY")

        kwargs: dict[str, Any] = {
            "api_key": settings.api_key,
            "timeout": settings.timeout_s,
            "max_retries": 0,
        }
        if settings.base_url:
            kwargs["base_url"] = settings.base_url

        self.settings = settings
        self._client = AsyncAnthropic(**kwargs)

    async def chat(
        self,
        message: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> ChatResult:
        """发送当前问题及可选历史，返回文本和 usage。"""
        messages = _build_messages(message, history)
        return await self._generate(messages, system=system or SYSTEM_PROMPT)

    async def stream_chat(
        self,
        message: str,
        *,
        history: Sequence[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas; cancellation closes the upstream Anthropic stream."""
        messages = _build_messages(message, history)
        async with aclosing(self.stream_message(messages=messages, system=system)) as stream:
            async for text in stream:
                yield text

    async def stream_message(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        system: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """在已有 tool_use / tool_result 上下文中流式生成最终文本。"""
        options = _tool_options(tools, tool_choice)
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                async with self._client.messages.stream(
                    model=self.settings.model,
                    max_tokens=self.settings.max_tokens,
                    system=system or SYSTEM_PROMPT,
                    messages=list(messages),
                    **options,
                ) as stream:
                    async for text in stream.text_stream:
                        if text:
                            yield text
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, anthropic.APITimeoutError) as ex:
            raise LLMError("模型响应超时，请稍后重试", status_code=503, code="llm_timeout") from ex
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as ex:
            raise LLMError("模型鉴权失败，请检查 ANTHROPIC_API_KEY 和 ANTHROPIC_BASE_URL", status_code=502, code="llm_auth_failed") from ex
        except anthropic.NotFoundError as ex:
            raise LLMError("模型或接口地址不存在，请检查配置", status_code=502, code="llm_not_found") from ex
        except anthropic.BadRequestError as ex:
            if _tools_unsupported(ex, tools):
                raise LLMError("上游不支持当前工具协议", status_code=502, code="tool_protocol_unsupported") from ex
            raise LLMError("模型请求参数错误，请检查配置和消息内容", status_code=502, code="llm_bad_request") from ex
        except anthropic.RateLimitError as ex:
            raise LLMError("模型服务限流，请稍后重试", status_code=503, code="llm_rate_limited") from ex
        except anthropic.APIConnectionError as ex:
            raise LLMError("无法连接模型服务，请检查网络", status_code=503, code="llm_connection_failed") from ex
        except anthropic.APIStatusError as ex:
            if _tools_unsupported(ex, tools):
                raise LLMError("上游不支持当前工具协议", status_code=502, code="tool_protocol_unsupported") from ex
            raise LLMError("模型服务返回错误，请稍后重试", status_code=502, code="llm_upstream_error") from ex
        except anthropic.APIError as ex:
            raise LLMError("模型服务异常，请稍后重试", status_code=502, code="llm_api_error") from ex

    async def summarize(self, prompt: str) -> str:
        """供 MemoryCompactor 注入使用；模型输出被截断时不能提交摘要。"""
        result = await self._generate(
            [{"role": "user", "content": prompt}],
            system="你是会话摘要器，只输出忠实、简洁的摘要正文。",
            reject_truncated=True,
        )
        return result.text

    async def extract_profile(self, prompt: str) -> str:
        """用户画像抽取；独立 JSON 指令，截断输出视为失败。"""
        result = await self._generate(
            [{"role": "user", "content": prompt}],
            system="你是用户画像提取器，只输出符合要求的 JSON 对象。",
            reject_truncated=True,
        )
        return result.text

    async def _generate(
        self,
        messages: list[dict[str, str]],
        *,
        system: str,
        reject_truncated: bool = False,
    ) -> ChatResult:
        response = await self._request_message(messages=messages, system=system)
        if reject_truncated and getattr(response, "stop_reason", None) == "max_tokens":
            raise LLMError("摘要输出被截断", code="summary_truncated")
        text = extract_text_content(getattr(response, "content", []))
        if not text:
            raise LLMError(
                "模型返回了空内容，请稍后重试",
                status_code=502,
                code="llm_empty_response",
            )
        input_tokens, output_tokens = _usage_tokens(getattr(response, "usage", None))
        return ChatResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)

    async def create_message(
        self,
        *,
        messages: Sequence[dict[str, Any]],
        system: str | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> MessageTurn:
        """执行一次非流式模型请求，保留原生 content block 和结构化调用。"""
        response = await self._request_message(
            messages=list(messages), system=system or SYSTEM_PROMPT, **_tool_options(tools, tool_choice),
        )
        return _parse_message_turn(response)

    async def _request_message(
        self, *, messages: list[dict[str, Any]], system: str, **options: Any,
    ) -> Any:
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                return await self._client.messages.create(
                    model=self.settings.model,
                    max_tokens=self.settings.max_tokens,
                    system=system,
                    messages=messages,
                    **options,
                )
        except (asyncio.TimeoutError, anthropic.APITimeoutError) as ex:
            raise LLMError(
                "模型响应超时，请稍后重试",
                status_code=503,
                code="llm_timeout",
            ) from ex
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as ex:
            raise LLMError(
                "模型鉴权失败，请检查 ANTHROPIC_API_KEY 和 ANTHROPIC_BASE_URL",
                status_code=502,
                code="llm_auth_failed",
            ) from ex
        except anthropic.NotFoundError as ex:
            raise LLMError(
                "模型或接口地址不存在，请检查 ANTHROPIC_MODEL 和 ANTHROPIC_BASE_URL",
                status_code=502,
                code="llm_not_found",
            ) from ex
        except anthropic.BadRequestError as ex:
            if _tools_unsupported(ex, options.get("tools")):
                raise LLMError("上游不支持当前工具协议", status_code=502, code="tool_protocol_unsupported") from ex
            raise LLMError(
                "模型请求参数错误，请检查 ANTHROPIC_MODEL 和消息内容",
                status_code=502,
                code="llm_bad_request",
            ) from ex
        except anthropic.RateLimitError as ex:
            raise LLMError(
                "模型服务限流，请稍后重试",
                status_code=503,
                code="llm_rate_limited",
            ) from ex
        except anthropic.APIConnectionError as ex:
            raise LLMError(
                "无法连接模型服务，请检查网络和 ANTHROPIC_BASE_URL",
                status_code=503,
                code="llm_connection_failed",
            ) from ex
        except anthropic.APIStatusError as ex:
            if _tools_unsupported(ex, options.get("tools")):
                raise LLMError("上游不支持当前工具协议", status_code=502, code="tool_protocol_unsupported") from ex
            raise LLMError(
                "模型服务返回错误，请稍后重试",
                status_code=502,
                code="llm_upstream_error",
            ) from ex
        except anthropic.APIError as ex:
            raise LLMError(
                "模型服务异常，请稍后重试",
                status_code=502,
                code="llm_api_error",
            ) from ex
