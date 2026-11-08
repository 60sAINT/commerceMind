"""CommerceMind FastAPI 入口：最小 LLM 对话纵向切片。

当前阶段只包含两条链路：

    GET  /health  -> 检查服务是否启动、模型是否已配置
    POST /chat    -> 参数校验 -> 调用 LLM -> 返回文本回复

不包含记忆、意图识别、Agent 路由、RAG、MCP 和监控。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from core.config import Settings
from core.llm import LLMClient, LLMError

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("commercemind.api")


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    model_configured: bool
    model: str
    detail: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message 不能为空")
        return value


class ChatResponse(BaseModel):
    request_id: str
    response: str
    latency_ms: float


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


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用，便于测试时注入独立配置。"""
    settings = settings or Settings.from_env()

    app = FastAPI(
        title="CommerceMind 智能客服",
        version="0.1.0",
        description="最小 LLM 对话链路：/health + /chat",
        docs_url="/docs",
    )
    app.state.settings = settings
    app.state.llm = LLMClient(settings) if settings.model_configured else None

    if settings.model_configured:
        logger.info(
            "模型配置已加载: model=%s base_url=%s",
            settings.model,
            settings.base_url or "(官方)",
        )
    else:
        logger.warning("未配置 ANTHROPIC_API_KEY，/chat 将返回 503")

    @app.get("/health", response_model=HealthResponse, tags=["基础"])
    async def health() -> HealthResponse:
        """健康检查：不依赖模型 Key，方便先启动再补配置。"""
        configured = settings.model_configured
        return HealthResponse(
            status="ok" if configured else "degraded",
            model_configured=configured,
            model=settings.model,
            detail=None if configured else "未配置 ANTHROPIC_API_KEY，/chat 将返回 503",
        )

    @app.post("/chat", response_model=ChatResponse, tags=["对话"])
    async def chat(req: ChatRequest, request: Request, response: Response) -> ChatResponse | JSONResponse:
        """最小对话接口：校验参数、调用 LLM、返回文本与耗时。"""
        request_id = uuid.uuid4().hex[:12]
        started = time.perf_counter()

        # 只记录长度，不记录用户原文，避免隐私进入日志。
        logger.info(
            "chat_start request_id=%s message_chars=%d",
            request_id,
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

        try:
            text = await llm.chat(req.message)
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

        latency_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "chat_done request_id=%s status=200 latency_ms=%.1f response_chars=%d",
            request_id,
            latency_ms,
            len(text),
        )
        response.headers["X-Request-ID"] = request_id
        return ChatResponse(
            request_id=request_id,
            response=text,
            latency_ms=round(latency_ms, 1),
        )

    return app


app = create_app()
