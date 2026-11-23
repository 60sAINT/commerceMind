"""多轮记忆接入 /chat/stream 的接口测试：fakeredis + 替身 LLM，不访问网络。"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from api.main import create_app
from core.config import IntentSettings, MemorySettings, Settings
from core.intent import IntentCategory, IntentResult, IntentSource
from core.llm import ChatResult, LLMError
from memory.locks import LockBusyError
from memory.service import ConversationMemory
from memory.store import RedisMemoryStore
from sse_helpers import chat_stream

MODEL = "test-model"


class FakeLLM:
    def __init__(self, text: str = "好的，已记录。", error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    async def chat(self, message: str, *, history=None, system=None) -> ChatResult:
        self.calls.append(
            {"message": message, "history": list(history or []), "system": system}
        )
        if self.error is not None:
            raise self.error
        return ChatResult(self.text, input_tokens=10, output_tokens=5)

    async def stream_chat(self, message: str, *, history=None, system=None):
        self.calls.append(
            {"message": message, "history": list(history or []), "system": system}
        )
        if self.error is not None:
            raise self.error
        for chunk in (self.text[: len(self.text) // 2], self.text[len(self.text) // 2 :]):
            if chunk:
                yield chunk


def make_settings(
    *, memory_enabled: bool = True, debug: bool = False, api_key: str = "test-key",
    intent: IntentSettings | None = None,
) -> Settings:
    return Settings(
        api_key=api_key,
        model=MODEL,
        timeout_s=2,
        max_tokens=64,
        memory=MemorySettings(
            enabled=memory_enabled,
            required=False,
            redis_url="redis://localhost:1/0",
            ttl_s=120,
            chroma_port=8001,
            debug=debug,
        ),
        intent=intent or IntentSettings(),
    )


async def send(app, method: str, url: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


def chat_body(
    message: str,
    *,
    user_id: str = "u1",
    session_id: str | None = "c1",
    request_id: str | None = None,
) -> dict:
    body = {"message": message, "user_id": user_id}
    if session_id is not None:
        body["session_id"] = session_id
    if request_id is not None:
        body["client_request_id"] = request_id
    return body


@pytest_asyncio.fixture
async def memory_env():
    client = FakeRedis(decode_responses=True)
    store = RedisMemoryStore(client, ttl_s=120)
    service = ConversationMemory(store)
    try:
        yield store, service
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_second_turn_reads_first_turn(memory_env):
    store, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM("您好，小李。")
    app.state.llm = llm

    first = await chat_stream(
        app, chat_body("我叫小李", request_id="r1"),
    )
    second = await chat_stream(
        app, chat_body("我叫什么？", request_id="r2"),
    )

    assert first.status_code == 200
    assert first.session_id == "c1"
    assert second.status_code == 200
    assert len(llm.calls) == 2
    assert llm.calls[1]["history"] == [
        {"role": "user", "content": "我叫小李"},
        {"role": "assistant", "content": "您好，小李。"},
    ]
    turns = await store.get_turns("u1", "c1")
    assert [turn.user_message for turn in turns] == ["我叫小李", "我叫什么？"]
    assert second.text == "您好，小李。"


@pytest.mark.asyncio
async def test_business_state_fills_pending_changes_orders_and_replays(memory_env):
    store, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM()
    app.state.llm = llm

    first = await chat_stream(app, chat_body("A123 要退款，金额 99 元", request_id="r1"))
    business = (await store.get_state("u1", "c1"))["business"]
    assert business["pending"] == {
        "fields": ["date", "payment_channel"], "asked_request_id": first.done["request_id"],
    }
    assert "请补充交易日期、支付渠道。" in first.text
    assert business["fields"]["amount"] == {
        "value": "99", "source": "user_message", "request_id": first.done["request_id"],
    }

    second = await chat_stream(app, chat_body("微信", request_id="r2"))
    assert "请补充交易日期。" in second.text
    assert '"payment_channel": "微信"' in llm.calls[-1]["system"]
    await chat_stream(app, chat_body("金额记错了，是 69 元", request_id="r3"))
    state = (await store.get_state("u1", "c1"))["business"]
    assert state["subject"] == {"type": "order", "id": "A123"}
    assert state["fields"]["amount"]["value"] == "69"
    assert state["fields"]["payment_channel"]["request_id"] == second.done["request_id"]

    await chat_stream(app, chat_body("换成 B456，查下物流", request_id="r4"))
    changed = (await store.get_state("u1", "c1"))["business"]
    assert changed["task"] == "logistics"
    assert changed["subject"] == {"type": "order", "id": "B456"}
    assert set(changed["fields"]) == {"order_id"}
    assert changed["pending"] is None
    assert "A123" not in llm.calls[-1]["system"]
    call_count = len(llm.calls)
    replay = await chat_stream(app, chat_body("微信", request_id="r2"))
    assert replay.text == second.text
    assert len(llm.calls) == call_count
    assert (await store.get_state("u1", "c1"))["business"] == changed


@pytest.mark.asyncio
async def test_business_state_clarifies_ambiguous_order_without_using_previous_order(memory_env):
    store, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM()
    app.state.llm = llm

    await chat_stream(app, chat_body("查 A123 的物流", request_id="r1"))
    response = await chat_stream(app, chat_body("查订单 123 和 456 的物流", request_id="r2"))
    assert response.text == "请确认这次要处理的唯一订单号。"
    assert len(llm.calls) == 1
    business = (await store.get_state("u1", "c1"))["business"]
    assert business["subject"] is None
    assert business["fields"] == {}
    assert business["pending"]["fields"] == ["order_id"]
    await chat_stream(app, chat_body("456", request_id="r3"))
    business = (await store.get_state("u1", "c1"))["business"]
    assert business["subject"] == {"type": "order", "id": "456"}
    assert business["pending"] is None


@pytest.mark.asyncio
async def test_business_state_technical_followup_does_not_cross_domains_or_commit_on_failure(memory_env):
    store, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM()
    app.state.llm = llm

    await chat_stream(app, chat_body("登录报 401", request_id="r1"))
    await chat_stream(app, chat_body("谢谢", request_id="r2"))
    assert '"error_code": "401"' not in llm.calls[-1]["system"]
    await chat_stream(app, chat_body("重登过了，还是不行", request_id="r3"))
    assert '"error_code": "401"' in llm.calls[-1]["system"]

    saved = await store.get_state("u1", "c1")
    llm.error = LLMError("offline")
    response = await chat_stream(app, chat_body("帮我查下物流", request_id="r4"))
    assert not response.done
    assert await store.get_state("u1", "c1") == saved
    llm.error = None
    await chat_stream(app, chat_body("帮我查下物流", request_id="r4"))
    assert '"error_code": "401"' not in llm.calls[-1]["system"]
    assert (await store.get_state("u1", "c1"))["business"]["fields"] == {}
    await chat_stream(app, chat_body("问题解决了", request_id="r5"))
    assert "business" not in await store.get_state("u1", "c1")


@pytest.mark.asyncio
async def test_chat_stream_persists_intent_atomically_with_successful_turn(
    memory_env, monkeypatch,
):
    store, service = memory_env
    settings = make_settings(intent=IntentSettings(enabled=True))
    app = create_app(settings, memory=service)
    fake_llm = FakeLLM("退款会在 3-5 个工作日到账。")
    app.state.llm = fake_llm

    async def recognize(message, llm, intent_settings, *, history=None):
        assert message == "我的订单什么时候退款？"
        return IntentResult(IntentCategory.REFUND, 0.91, IntentSource.LLM)

    monkeypatch.setattr("api.main._recognize_intent", recognize)
    response = await send(
        app, "POST", "/chat/stream", json=chat_body("我的订单什么时候退款？"),
    )

    assert response.status_code == 200
    assert "event: done" in response.text
    snapshot = await store.get_snapshot("u1", "c1")
    assert len(snapshot.turns) == 1
    assert snapshot.state["intent"]["intent"] == "refund"
    assert snapshot.state["intent"]["confidence"] == 0.91
    assert '"intent": {"intent": "refund"' in fake_llm.calls[0]["system"]
    assert '"intent"' not in response.text


@pytest.mark.asyncio
async def test_chat_stream_failed_generation_does_not_persist_intent(
    memory_env, monkeypatch,
):
    store, service = memory_env
    settings = make_settings(intent=IntentSettings(enabled=True))
    app = create_app(settings, memory=service)
    app.state.llm = FakeLLM(error=LLMError("offline"))

    calls = []

    async def recognize(*args, **kwargs):
        calls.append((args, kwargs))
        return IntentResult(IntentCategory.REFUND, 0.91, IntentSource.LLM)

    monkeypatch.setattr("api.main._recognize_intent", recognize)
    response = await chat_stream(app, chat_body("我要退款"))

    assert response.status_code == 200
    assert response.error is not None
    assert response.error["error_code"] == "llm_unavailable"
    assert len(calls) == 1
    assert await store.get_state("u1", "c1") == {}
    assert await store.get_turns("u1", "c1") == []


@pytest.mark.asyncio
async def test_different_conversations_do_not_share_history(memory_env):
    _, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM()
    app.state.llm = llm

    await chat_stream(app, chat_body("第一段对话"))
    await chat_stream(app, chat_body("第二段对话", session_id="c2"))

    assert llm.calls[1]["history"] == []


@pytest.mark.asyncio
async def test_auto_generated_session_id_is_returned(memory_env):
    _, service = memory_env
    app = create_app(make_settings(), memory=service)
    app.state.llm = FakeLLM()

    response = await chat_stream(app, chat_body("你好", session_id=None))

    assert response.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{16}", response.session_id)


@pytest.mark.asyncio
async def test_failed_turn_is_not_persisted(memory_env):
    store, service = memory_env
    app = create_app(make_settings(), memory=service)
    app.state.llm = FakeLLM(
        error=LLMError("模型响应超时", status_code=503, code="llm_timeout"),
    )

    response = await chat_stream(app, chat_body("查询订单", request_id="r1"))

    assert response.status_code == 200
    assert response.error is not None
    assert response.error["error_code"] == "llm_timeout"
    assert await store.get_turns("u1", "c1") == []
    assert await store.get_receipt("u1", "c1", "r1") is None


@pytest.mark.asyncio
async def test_same_client_request_id_replays_without_calling_llm(memory_env):
    store, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM("第一次回答")
    app.state.llm = llm

    first = await chat_stream(app, chat_body("重复请求", request_id="same"))
    second = await chat_stream(app, chat_body("重复请求", request_id="same"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.text == second.text
    assert first.request_id == second.request_id
    assert len(llm.calls) == 1
    assert len(await store.get_turns("u1", "c1")) == 1


@pytest.mark.asyncio
async def test_same_client_request_id_with_different_message_conflicts(memory_env):
    _, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM()
    app.state.llm = llm

    await chat_stream(app, chat_body("第一句", request_id="same"))
    response = await chat_stream(
        app, chat_body("第二句", request_id="same"),
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "client_request_id_conflict"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_retry_without_session_id_uses_original_session(memory_env):
    store, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM("原会话回答")
    app.state.llm = llm

    first = await chat_stream(
        app, chat_body("重复请求", session_id=None, request_id="same"),
    )
    second = await chat_stream(
        app, chat_body("重复请求", session_id=None, request_id="same"),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.session_id == second.session_id
    assert first.request_id == second.request_id
    assert len(llm.calls) == 1
    assert len(await store.get_turns("u1", first.session_id)) == 1


@pytest.mark.asyncio
async def test_conflict_without_session_id_returns_409(memory_env):
    _, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM()
    app.state.llm = llm

    await chat_stream(
        app, chat_body("第一句", session_id=None, request_id="same"),
    )
    response = await chat_stream(
        app, chat_body("第二句", session_id=None, request_id="same"),
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "client_request_id_conflict"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_summary_is_injected_into_system_not_messages(memory_env):
    store, service = memory_env
    await store.set_summary("u1", "c1", "用户叫小李，正在咨询退款。")
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM()
    app.state.llm = llm

    response = await chat_stream(app, chat_body("退款到哪了？"))

    assert response.status_code == 200
    assert "用户叫小李，正在咨询退款。" in llm.calls[0]["system"]
    assert llm.calls[0]["history"] == []


@pytest.mark.asyncio
async def test_read_failure_degrades_to_no_history(memory_env):
    _, service = memory_env
    app = create_app(make_settings(), memory=service)
    llm = FakeLLM()
    app.state.llm = llm

    async def broken(user_id: str, session_id: str):
        raise ConnectionError("redis down")

    service.read_context = broken

    response = await chat_stream(app, chat_body("你好"))

    assert response.status_code == 200
    assert len(llm.calls) == 1
    assert llm.calls[0]["history"] == []


@pytest.mark.asyncio
async def test_commit_failure_still_returns_answer(memory_env):
    _, service = memory_env
    app = create_app(make_settings(), memory=service)
    app.state.llm = FakeLLM("答案仍然返回")

    async def broken(**kwargs):
        raise ConnectionError("redis down")

    service.commit = broken

    response = await chat_stream(app, chat_body("你好"))

    assert response.status_code == 200
    assert response.text == "答案仍然返回"
    assert response.done["response"] == "答案仍然返回"


@pytest.mark.asyncio
async def test_conversation_busy_is_streamed_as_error_event(memory_env):
    _, service = memory_env
    app = create_app(make_settings(), memory=service)
    app.state.llm = FakeLLM()

    @asynccontextmanager
    async def busy(user_id: str, session_id: str):
        raise LockBusyError("当前会话正在处理请求，请稍后重试")
        yield  # pragma: no cover

    service.conversation = busy

    response = await chat_stream(app, chat_body("你好"))

    assert response.status_code == 200
    assert response.error is not None
    assert response.error["error_code"] == "conversation_busy"
    assert "正在处理" in response.error["detail"]


@pytest.mark.asyncio
async def test_health_reports_memory_status(memory_env):
    _, service = memory_env
    app = create_app(make_settings(), memory=service)

    response = await send(app, "GET", "/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["memory"] == {
        "enabled": True,
        "redis": "ok",
        "chroma": "disabled",
        "detail": None,
    }


@pytest.mark.asyncio
async def test_health_degrades_when_memory_enabled_but_not_started():
    app = create_app(make_settings(memory_enabled=True))

    response = await send(app, "GET", "/health")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "degraded"
    assert body["memory"]["enabled"] is True
    assert body["memory"]["redis"] == "error"


@pytest.mark.asyncio
async def test_memory_debug_route_is_hidden_when_disabled(memory_env):
    _, service = memory_env
    app = create_app(make_settings(debug=False), memory=service)

    response = await send(app, "GET", "/memory/c1", params={"user_id": "u1"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_memory_debug_route_is_hidden_when_memory_not_started():
    app = create_app(make_settings(debug=True))

    response = await send(app, "GET", "/memory/c1", params={"user_id": "u1"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_memory_debug_returns_snapshot(memory_env):
    store, service = memory_env
    await store.commit_turn(
        "u1", "c1",
        request_id="r1",
        user_message="第一问",
        assistant_message="第一答",
    )
    await store.commit_turn(
        "u1", "c1",
        request_id="r2",
        user_message="第二问",
        assistant_message="第二答",
        state={"stage": "refund"},
        summary="用户正在咨询退款。",
    )
    app = create_app(make_settings(debug=True), memory=service)

    response = await send(app, "GET", "/memory/c1", params={"user_id": "u1"})

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "c1"
    assert body["user_id"] == "u1"
    assert body["turn_count"] == 2
    assert [turn["user_message"] for turn in body["turns"]] == ["第一问", "第二问"]
    assert body["turns"][0]["request_id"] == "r1"
    assert body["turns"][0]["created_at"]
    assert body["state"] == {"stage": "refund"}
    assert body["summary"] == "用户正在咨询退款。"
    assert 0 < body["ttl_s"]["working"] <= 120
    assert 0 < body["ttl_s"]["state"] <= 120
    assert 0 < body["ttl_s"]["summary"] <= 120
    # 不暴露内部 key 与回执内容。
    assert "receipt" not in response.text
    assert "wm_v1" not in response.text
    assert "ureceipt_v1" not in response.text


@pytest.mark.asyncio
async def test_memory_debug_is_isolated_by_user_and_missing_session(memory_env):
    store, service = memory_env
    await store.commit_turn(
        "u1", "c1",
        request_id="r1",
        user_message="第一问",
        assistant_message="第一答",
    )
    app = create_app(make_settings(debug=True), memory=service)

    other_user = await send(app, "GET", "/memory/c1", params={"user_id": "u2"})
    missing = await send(app, "GET", "/memory/missing", params={"user_id": "u1"})

    assert other_user.status_code == 200
    assert other_user.json()["turn_count"] == 0
    assert missing.status_code == 200
    assert missing.json()["turn_count"] == 0
    assert missing.json()["ttl_s"]["working"] == -2


@pytest.mark.asyncio
async def test_memory_debug_works_without_model_key(memory_env):
    _, service = memory_env
    app = create_app(make_settings(debug=True, api_key=""), memory=service)

    response = await send(app, "GET", "/memory/c1", params={"user_id": "u1"})

    assert response.status_code == 200
    assert response.json()["turn_count"] == 0


def test_memory_settings_validate_required_and_ttl():
    with pytest.raises(ValueError):
        MemorySettings(enabled=False, required=True)
    with pytest.raises(ValueError):
        MemorySettings(enabled=True, ttl_s=0)
    with pytest.raises(ValueError):
        MemorySettings(enabled=True, chroma_port=70000)


def test_settings_from_env_reads_memory_config(monkeypatch):
    monkeypatch.setenv("COMMERCEMIND_MEMORY_ENABLED", "true")
    monkeypatch.setenv("COMMERCEMIND_MEMORY_REQUIRED", "false")
    monkeypatch.setenv("REDIS_URL", "redis://example:6379/1")
    monkeypatch.setenv("COMMERCEMIND_MEMORY_TTL_SECONDS", "120")
    monkeypatch.setenv("CHROMA_HOST", "chroma")
    monkeypatch.setenv("CHROMA_PORT", "8000")
    monkeypatch.setenv("COMMERCEMIND_MEMORY_DEBUG", "true")

    settings = Settings.from_env()

    assert settings.memory.enabled is True
    assert settings.memory.required is False
    assert settings.memory.redis_url == "redis://example:6379/1"
    assert settings.memory.ttl_s == 120
    assert settings.memory.chroma_host == "chroma"
    assert settings.memory.chroma_port == 8000
    assert settings.memory.debug is True


@pytest.mark.asyncio
async def test_memory_disabled_keeps_single_turn_behavior():
    app = create_app(make_settings(memory_enabled=False))
    llm = FakeLLM()
    app.state.llm = llm

    response = await chat_stream(app, chat_body("你好"))

    assert response.status_code == 200
    assert llm.calls[0]["history"] == []
    assert (await send(app, "GET", "/health")).json()["memory"]["enabled"] is False
