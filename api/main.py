"""CommerceMind FastAPI 入口：LLM 对话 + 短期多轮记忆。

当前阶段包含两条链路：

    GET  /health  -> 检查服务、模型和记忆依赖（Redis/Chroma）
    POST /chat    -> 读 working memory -> 调用 LLM -> 原子提交本轮

不包含意图识别、Agent 路由、RAG、MCP 和监控；长期记忆检索暂未接入。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from core.config import Settings
from core.llm import SYSTEM_PROMPT, ChatResult, LLMClient, LLMError
from memory.locks import LeaseLostError, LockBusyError
from memory.service import ConversationMemory, MemoryHealth

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("commercemind.api")

CHAT_TIMEOUT_S = 50.0


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    model_configured: bool
    model: str
    memory: MemoryHealth
    detail: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    user_id: str = Field(default="anonymous", min_length=1, max_length=128)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("message", "user_id", "session_id", "client_request_id")
    @classmethod
    def strip_text_fields(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        return value


class ChatResponse(BaseModel):
    request_id: str
    session_id: str
    response: str
    latency_ms: float
    input_tokens: int | None = None
    output_tokens: int | None = None


class _ReceiptConflict(Exception):
    """同一 client_request_id 被用于不同消息。"""


def _error_response(
    request_id: str,
    status_code: int,
    detail: str,
    *,
    error_code: str,
) -> JSONResponse:
    """返回不包含上游异常细节的安全错误响应。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": detail,
            "request_id": request_id,
            "error_code": error_code,
        },
        headers={"X-Request-ID": request_id},
    )


def _result_text(result: ChatResult | str) -> str:
    """兼容旧测试替身返回纯字符串。"""
    return result if isinstance(result, str) else result.text


def _result_usage(result: ChatResult | str) -> tuple[int | None, int | None]:
    if isinstance(result, ChatResult):
        return result.input_tokens, result.output_tokens
    return None, None


def create_app(
    settings: Settings | None = None,
    *,
    memory: ConversationMemory | None = None,
) -> FastAPI:
    """创建 FastAPI 应用，便于测试时注入独立配置和记忆服务。"""
    settings = settings or Settings.from_env()
    llm = LLMClient(settings) if settings.model_configured else None
    started_memory: ConversationMemory | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal started_memory
        # 注入的服务归调用方所有；只关闭自己启动的实例。
        if settings.memory.enabled and llm is not None and app.state.memory is None:
            try:
                started_memory = await ConversationMemory.start(settings.memory, llm)
                app.state.memory = started_memory
            except Exception as exc:
                if settings.memory.required:
                    raise RuntimeError(
                        "记忆依赖不可用，且 COMMERCEMIND_MEMORY_REQUIRED=true"
                    ) from exc
                logger.warning(
                    "记忆初始化失败，按无记忆模式运行: %s", type(exc).__name__,
                )
        try:
            yield
        finally:
            if started_memory is not None:
                try:
                    await started_memory.close()
                except Exception:
                    logger.warning("关闭记忆服务失败", exc_info=True)

    app = FastAPI(
        title="CommerceMind 智能客服",
        version="0.1.0",
        description="LLM 对话链路：/health + /chat（含短期多轮记忆）",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.llm = llm
    app.state.memory = memory

    if settings.model_configured:
        logger.info(
            "模型配置已加载: model=%s base_url=%s",
            settings.model,
            settings.base_url or "(官方)",
        )
    else:
        logger.warning("未配置 ANTHROPIC_API_KEY，/chat 将返回 503")

    if settings.memory.enabled:
        logger.info(
            "记忆配置已加载: ttl_s=%d required=%s chroma=%s:%d",
            settings.memory.ttl_s,
            settings.memory.required,
            settings.memory.chroma_host,
            settings.memory.chroma_port,
        )

    @app.get("/health", response_model=HealthResponse, tags=["基础"])
    async def health() -> HealthResponse:
        """健康检查：不依赖模型 Key，方便先启动再补配置。"""
        configured = settings.model_configured

        if app.state.memory is not None:
            memory_health = await app.state.memory.health()
        elif settings.memory.enabled and llm is not None:
            memory_health = MemoryHealth(
                enabled=True,
                redis="error",
                chroma="disabled",
                detail="记忆服务未启动",
            )
        else:
            memory_health = MemoryHealth(
                enabled=False, redis="disabled", chroma="disabled",
            )

        problems: list[str] = []
        if not configured:
            problems.append("未配置 ANTHROPIC_API_KEY，/chat 将返回 503")
        if memory_health.enabled and not memory_health.ok:
            problems.append(f"记忆依赖不可用：{memory_health.detail or 'unknown'}")

        return HealthResponse(
            status="ok" if not problems else "degraded",
            model_configured=configured,
            model=settings.model,
            memory=memory_health,
            detail="；".join(problems) or None,
        )

    @app.post("/chat", response_model=ChatResponse, tags=["对话"])
    async def chat(
        req: ChatRequest, request: Request, response: Response,
    ) -> ChatResponse | JSONResponse:
        """读记忆、调用模型、提交本轮；失败轮次不写入。"""
        request_id = uuid.uuid4().hex[:12]
        session_id = req.session_id or uuid.uuid4().hex[:16]
        started = time.perf_counter()

        # 只记录长度和标识，不记录用户原文，避免隐私进入日志。
        logger.info(
            "chat_start request_id=%s session_id=%s memory=%s message_chars=%d",
            request_id,
            session_id,
            request.app.state.memory is not None,
            len(req.message),
        )

        llm: LLMClient | None = request.app.state.llm
        if llm is None:
            latency_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "chat_error request_id=%s status=503 error_code=model_not_configured "
                "error_type=none latency_ms=%.1f",
                request_id,
                latency_ms,
            )
            return _error_response(
                request_id,
                503,
                "模型未配置：请先在 .env 中设置 ANTHROPIC_API_KEY",
                error_code="model_not_configured",
            )

        memory: ConversationMemory | None = request.app.state.memory

        async def run_with_memory() -> ChatResponse:
            async with memory.conversation(req.user_id, session_id):
                previous = await memory.get_receipt(
                    req.user_id, session_id, req.client_request_id,
                )
                if previous is not None:
                    if previous.user_message != req.message:
                        raise _ReceiptConflict
                    latency_ms = (time.perf_counter() - started) * 1000
                    logger.info(
                        "chat_replay request_id=%s session_id=%s latency_ms=%.1f",
                        previous.request_id,
                        session_id,
                        latency_ms,
                    )
                    return ChatResponse(
                        request_id=previous.request_id,
                        session_id=session_id,
                        response=previous.assistant_message,
                        latency_ms=round(latency_ms, 1),
                    )

                try:
                    context = await memory.read_context(req.user_id, session_id)
                except Exception:
                    # 读记忆失败按无历史继续，不能因为 Redis 故障拒绝本轮对话。
                    logger.warning(
                        "memory_read_failed request_id=%s session_id=%s",
                        request_id,
                        session_id,
                        exc_info=True,
                    )
                    context = None

                if context is None:
                    result = await llm.chat(req.message)
                else:
                    result = await llm.chat(
                        req.message,
                        history=list(context.recent_messages),
                        system=context.to_system_text(SYSTEM_PROMPT),
                    )
                text = _result_text(result)
                input_tokens, output_tokens = _result_usage(result)

                try:
                    # 已在会话锁内，避免 commit_turn 再次加锁自锁。
                    await memory.commit(
                        req.user_id,
                        session_id,
                        request_id=request_id,
                        question=req.message,
                        answer=text,
                        client_request_id=req.client_request_id,
                        lock_held=True,
                    )
                except Exception:
                    # 答案已经生成，写入失败只降级记忆，不把本轮回答丢掉。
                    logger.warning(
                        "memory_commit_failed request_id=%s session_id=%s",
                        request_id,
                        session_id,
                        exc_info=True,
                    )

                latency_ms = (time.perf_counter() - started) * 1000
                return ChatResponse(
                    request_id=request_id,
                    session_id=session_id,
                    response=text,
                    latency_ms=round(latency_ms, 1),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )

        try:
            if memory is None:
                result = await llm.chat(req.message)
                text = _result_text(result)
                input_tokens, output_tokens = _result_usage(result)
                latency_ms = (time.perf_counter() - started) * 1000
                chat_response = ChatResponse(
                    request_id=request_id,
                    session_id=session_id,
                    response=text,
                    latency_ms=round(latency_ms, 1),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            else:
                async with asyncio.timeout(CHAT_TIMEOUT_S):
                    chat_response = await run_with_memory()
        except TimeoutError:
            latency_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "chat_error request_id=%s status=504 error_code=request_timeout "
                "error_type=timeout latency_ms=%.1f",
                request_id,
                latency_ms,
            )
            return _error_response(
                request_id,
                504,
                "本轮处理超时，请稍后重试",
                error_code="request_timeout",
            )
        except _ReceiptConflict:
            latency_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "chat_error request_id=%s status=409 error_code=client_request_id_conflict "
                "latency_ms=%.1f",
                request_id,
                latency_ms,
            )
            return _error_response(
                request_id,
                409,
                "同一 client_request_id 不能用于不同消息",
                error_code="client_request_id_conflict",
            )
        except LockBusyError as ex:
            latency_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "chat_error request_id=%s status=409 error_code=conversation_busy "
                "latency_ms=%.1f",
                request_id,
                latency_ms,
            )
            return JSONResponse(
                status_code=409,
                content={
                    "detail": str(ex),
                    "request_id": request_id,
                    "error_code": "conversation_busy",
                },
                headers={"X-Request-ID": request_id, "Retry-After": "1"},
            )
        except LeaseLostError as ex:
            latency_ms = (time.perf_counter() - started) * 1000
            logger.warning(
                "chat_error request_id=%s status=503 error_code=conversation_lease_lost "
                "latency_ms=%.1f",
                request_id,
                latency_ms,
            )
            return _error_response(
                request_id,
                503,
                str(ex),
                error_code="conversation_lease_lost",
            )
        except LLMError as ex:
            latency_ms = (time.perf_counter() - started) * 1000
            cause_type = type(ex.__cause__).__name__ if ex.__cause__ else "none"
            logger.warning(
                "chat_error request_id=%s status=%d error_code=%s error_type=%s latency_ms=%.1f",
                request_id,
                ex.status_code,
                ex.code,
                cause_type,
                latency_ms,
            )
            return _error_response(
                request_id,
                ex.status_code,
                str(ex),
                error_code=ex.code,
            )
        except Exception:
            latency_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "chat_unexpected request_id=%s latency_ms=%.1f",
                request_id,
                latency_ms,
            )
            return _error_response(
                request_id,
                500,
                "服务内部错误，请稍后重试",
                error_code="internal_error",
            )

        logger.info(
            "chat_done request_id=%s session_id=%s status=200 latency_ms=%.1f "
            "response_chars=%d input_tokens=%s output_tokens=%s",
            chat_response.request_id,
            session_id,
            chat_response.latency_ms,
            len(chat_response.response),
            chat_response.input_tokens,
            chat_response.output_tokens,
        )
        response.headers["X-Request-ID"] = chat_response.request_id
        return chat_response

    return app


app = create_app()
