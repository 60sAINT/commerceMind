"""最小对话链路测试：全部使用 mock，不依赖真实 API Key 或网络。"""
from __future__ import annotations

import logging

import anthropic
import httpx
import pytest

from api.main import create_app
from core.config import Settings
from core.llm import LLMClient, LLMError, extract_text_content

MODEL = "test-model"


class FakeLLM:
    """替身 LLM：用于验证 FastAPI 层的行为，不访问网络。"""

    def __init__(self, text: str = "退款一般 3-5 个工作日到账。", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.last_message: str | None = None

    async def chat(self, message: str) -> str:
        self.last_message = message
        if self.error is not None:
            raise self.error
        return self.text


def make_app(*, api_key: str = "test-key") -> object:
    return create_app(Settings(api_key=api_key, model=MODEL, timeout_s=2, max_tokens=128))


async def send(app, method: str, url: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_health_reports_missing_key_without_failing_startup():
    app = make_app(api_key="")
    response = await send(app, "GET", "/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["model_configured"] is False
    assert "ANTHROPIC_API_KEY" in body["detail"]


@pytest.mark.asyncio
async def test_health_treats_template_placeholder_key_as_missing():
    app = make_app(api_key="xxx")
    response = await send(app, "GET", "/health")

    assert response.status_code == 200
    body = response.json()
    assert body["model_configured"] is False
    assert "ANTHROPIC_API_KEY" in body["detail"]


@pytest.mark.asyncio
async def test_health_reports_configured_model():
    app = make_app()
    response = await send(app, "GET", "/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["model_configured"] is True
    assert body["model"] == MODEL
    assert body["detail"] is None


@pytest.mark.asyncio
async def test_chat_returns_model_text_and_latency():
    app = make_app()
    fake = FakeLLM("退款一般 3-5 个工作日到账。")
    app.state.llm = fake

    response = await send(
        app,
        "POST",
        "/chat",
        json={"message": "  退款一般多久到账？  "},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "退款一般 3-5 个工作日到账。"
    assert body["latency_ms"] >= 0
    assert body["request_id"]
    assert response.headers["x-request-id"] == body["request_id"]
    assert fake.last_message == "退款一般多久到账？"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["", "   "])
async def test_chat_rejects_empty_message(message):
    app = make_app()

    response = await send(app, "POST", "/chat", json={"message": message})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_chat_requires_configured_key():
    app = make_app(api_key="")
    response = await send(app, "POST", "/chat", json={"message": "你好"})

    assert response.status_code == 503
    body = response.json()
    assert body["error_code"] == "model_not_configured"
    assert body["request_id"]


@pytest.mark.asyncio
async def test_chat_maps_llm_error_without_leaking_exception():
    app = make_app()
    app.state.llm = FakeLLM(
        error=LLMError("模型响应超时，请稍后重试", status_code=503, code="llm_timeout")
    )

    response = await send(app, "POST", "/chat", json={"message": "你好"})

    assert response.status_code == 503
    body = response.json()
    assert body["error_code"] == "llm_timeout"
    assert body["detail"] == "模型响应超时，请稍后重试"
    assert "Traceback" not in response.text


@pytest.mark.asyncio
async def test_llm_timeout_is_mapped_to_503():
    app = make_app()
    assert isinstance(app.state.llm, LLMClient)
    request = httpx.Request("POST", "https://api.test/v1/messages")

    async def fake_create(**kwargs):
        raise anthropic.APITimeoutError(request=request)

    app.state.llm._client.messages.create = fake_create

    response = await send(app, "POST", "/chat", json={"message": "你好"})

    assert response.status_code == 503
    body = response.json()
    assert body["error_code"] == "llm_timeout"
    assert "超时" in body["detail"]


@pytest.mark.asyncio
async def test_llm_auth_failure_is_mapped_to_502():
    app = make_app()
    request = httpx.Request("POST", "https://api.test/v1/messages")

    async def fake_create(**kwargs):
        raise anthropic.AuthenticationError(
            "invalid api key",
            response=httpx.Response(401, request=request),
            body=None,
        )

    app.state.llm._client.messages.create = fake_create

    response = await send(app, "POST", "/chat", json={"message": "你好"})

    assert response.status_code == 502
    body = response.json()
    assert body["error_code"] == "llm_auth_failed"
    assert "鉴权失败" in body["detail"]
    assert "invalid api key" not in response.text


@pytest.mark.asyncio
async def test_logs_contain_request_id_but_not_user_message(caplog):
    caplog.set_level(logging.INFO, logger="commercemind.api")
    app = make_app()
    app.state.llm = FakeLLM("已收到。")

    private_message = "我的手机号 13800138000，退款多久到账？"
    response = await send(app, "POST", "/chat", json={"message": private_message})

    assert response.status_code == 200
    assert response.json()["request_id"] in caplog.text
    assert "13800138000" not in caplog.text
    assert "退款多久到账" not in caplog.text


def test_extract_text_content_ignores_non_text_blocks():
    content = [
        {"type": "text", "text": "第一段"},
        {"type": "tool_use", "id": "tool-1", "input": {}},
        {"type": "text", "text": "第二段"},
    ]

    assert extract_text_content(content) == "第一段\n第二段"
