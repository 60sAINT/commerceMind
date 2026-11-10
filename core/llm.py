"""最小 LLM 客户端封装。

把单条用户消息或摘要任务发给 Anthropic 兼容接口，并返回纯文本。

- 统一超时：SDK 超时 + `asyncio.timeout` 双保险。
- 统一错误：把 Anthropic SDK 的异常转换成带 HTTP 状态码的 `LLMError`，
  避免把裸异常和上游错误细节直接暴露给调用方。
- 统一取文本：兼容 Anthropic 的 content block，也兼容测试中的 dict。
"""
from __future__ import annotations

import asyncio
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

    async def chat(self, message: str) -> str:
        """发送一条消息，返回模型生成的纯文本。"""
        return await self._generate(message, system=SYSTEM_PROMPT)

    async def summarize(self, prompt: str) -> str:
        """供 MemoryCompactor 注入使用；模型输出被截断时不能提交摘要。"""
        return await self._generate(
            prompt, system="你是会话摘要器，只输出忠实、简洁的摘要正文。", reject_truncated=True,
        )

    async def extract_profile(self, prompt: str) -> str:
        """用户画像抽取；独立 JSON 指令，截断输出视为失败。"""
        return await self._generate(prompt, system="你是用户画像提取器，只输出符合要求的 JSON 对象。",
                                    reject_truncated=True)

    async def _generate(self, message: str, *, system: str, reject_truncated: bool = False) -> str:
        try:
            async with asyncio.timeout(self.settings.timeout_s):
                response = await self._client.messages.create(
                    model=self.settings.model,
                    max_tokens=self.settings.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": message}],
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

        if reject_truncated and getattr(response, "stop_reason", None) == "max_tokens":
            raise LLMError("摘要输出被截断", code="summary_truncated")
        text = extract_text_content(getattr(response, "content", []))
        if not text:
            raise LLMError(
                "模型返回了空内容，请稍后重试",
                status_code=502,
                code="llm_empty_response",
            )
        return text
