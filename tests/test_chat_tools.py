"""T11：真实 SSE 路由、工具循环及 Redis 会话提交。"""
import asyncio
import copy
import json
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from starlette.requests import Request

from api.main import ChatRequest, create_app
from core.config import IntentSettings, Settings, SkillSettings, ToolSettings
from core.intent import IntentCategory, IntentResult, IntentSource
from core.llm import LLMError, MessageTurn, ToolUse
from memory.service import ConversationMemory
from memory.store import RedisMemoryStore
from sse_helpers import chat_stream
from tooling import ToolSpec


class FakeLLM:
    def __init__(self, *, direct=False, failure=False, name="compare_amounts", arguments=None):
        self.direct = direct
        self.failure = failure
        self.name = name
        self.arguments = arguments if arguments is not None else {"amount_a": 10, "amount_b": 3}
        self.decisions = []
        self.finals = []
        self.ordinary = []

    async def create_message(self, **kwargs):
        self.decisions.append(copy.deepcopy(kwargs))
        if self.direct:
            return MessageTurn([{"type": "text", "text": "直接回答"}], "直接回答", (), "end_turn")
        call = ToolUse("c1", self.name, self.arguments)
        return MessageTurn(
            [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}],
            "", (call,), "tool_use",
        )

    async def stream_message(self, **kwargs):
        self.finals.append(copy.deepcopy(kwargs))
        if self.failure:
            raise LLMError("secret")
        yield "最终"
        yield "回答"

    async def stream_chat(self, message, **kwargs):
        self.ordinary.append((message, kwargs))
        yield "普通回答"


def make_app(llm, *, memory=None, skills=None, intent=None, **tools):
    return create_app(
        Settings(
            api_key="test", model="test",
            tools=ToolSettings(**({"enabled": True, "trigger": "always", "max_rounds": 1} | tools)),
            skills=skills or SkillSettings(), intent=intent or IntentSettings(),
        ),
        llm=llm, memory=memory,
    )


@pytest_asyncio.fixture
async def memory_env():
    client = FakeRedis(decode_responses=True)
    store = RedisMemoryStore(client, ttl_s=120)
    try:
        yield store, ConversationMemory(store)
    finally:
        await client.aclose()


def body(message="比较两笔金额", request_id=None):
    result = {"message": message, "user_id": "u", "session_id": "s"}
    if request_id:
        result["client_request_id"] = request_id
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [False, True])
async def test_tool_round_trip_emits_final_deltas_and_optional_tool_events(events):
    llm = FakeLLM()
    app = make_app(llm, stream_events=events)
    response = await chat_stream(app, body())
    assert response.error is None
    assert response.text == response.done["response"] == "最终回答"
    assert response.done["success"] and response.done["tools_used"] == ["compare_amounts"]
    assert response.done["tool_calls"] == 1
    names = [name for name, _ in response.events]
    assert names == (["meta", "tool_start", "tool_end", "delta", "delta", "done"] if events
                     else ["meta", "delta", "delta", "done"])
    assert "tool_traces" not in response.done
    assert len(llm.decisions) == len(llm.finals) == 1
    assert not llm.ordinary
    assert len(app.state.tool_manager) == 8


@pytest.mark.asyncio
async def test_direct_answer_does_not_start_an_extra_llm_request():
    llm = FakeLLM(direct=True)
    response = await chat_stream(make_app(llm), body())
    assert response.text == "直接回答" and response.done["tool_calls"] == 0
    assert len(llm.decisions) == 1 and not llm.finals


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"enabled": False}, {"trigger": "off"}])
async def test_disabled_tools_keep_ordinary_chat_unchanged(options):
    llm = FakeLLM()
    app = make_app(llm, **options)
    response = await chat_stream(app, body())
    assert response.text == "普通回答" and not llm.decisions
    assert "tools_used" not in response.done
    if options.get("enabled") is False:
        assert app.state.tool_manager is None


@pytest.mark.asyncio
async def test_auto_routes_without_memory_using_local_patterns():
    llm = FakeLLM()
    app = make_app(llm, trigger="auto")
    matched = await chat_stream(app, body("支付失败，比较金额"))
    normal = await chat_stream(app, body("你好"))
    assert matched.done["tools_used"] == ["compare_amounts"]
    assert normal.text == "普通回答"
    assert len(llm.decisions) == len(llm.ordinary) == 1


@pytest.mark.asyncio
async def test_recognized_entities_are_passed_to_current_request_tools(monkeypatch):
    async def recognize(*args, **kwargs):
        return IntentResult(
            IntentCategory.REFUND, 0.91, IntentSource.LLM, entities={"order_id": ["A1001"]},
        )

    monkeypatch.setattr("api.main._recognize_intent", recognize)
    llm = FakeLLM(name="check_billing_fields", arguments={"payment_channel": "微信"})
    app = make_app(llm, trigger="auto", intent=IntentSettings(enabled=True))
    response = await chat_stream(app, body("退款核验"))
    reply = json.loads(llm.finals[0]["messages"][-1]["content"][0]["content"])
    assert reply["data"]["fields"]["order_id"] is True
    assert reply["data"]["fields"]["payment_channel"] is True
    assert response.done["tools_used"] == ["check_billing_fields"]


@pytest.mark.asyncio
async def test_billing_tool_reads_current_order_fields_and_drops_them_on_order_change(memory_env):
    _, memory = memory_env
    llm = FakeLLM(name="check_billing_fields", arguments={})
    app = make_app(llm, memory=memory)

    await chat_stream(app, body("A123 要退款，金额 99 元，昨天用微信付的", "r1"))
    await chat_stream(app, body("继续处理这笔退款", "r2"))
    reply = json.loads(llm.finals[-1]["messages"][-1]["content"][0]["content"])
    assert all(reply["data"]["fields"].values())
    assert reply["data"]["can_confirm_refund"] is False

    await chat_stream(app, body("换成 B456，查退款", "r3"))
    reply = json.loads(llm.finals[-1]["messages"][-1]["content"][0]["content"])
    assert reply["data"]["fields"] == {
        "order_id": True, "amount": False, "date": False, "payment_channel": False,
    }


@pytest.mark.asyncio
async def test_only_final_answer_is_persisted_and_replay_skips_tools(memory_env):
    store, memory = memory_env
    llm = FakeLLM()
    app = make_app(llm, memory=memory)
    first = await chat_stream(app, body(request_id="r1"))
    replay = await chat_stream(app, body(request_id="r1"))
    await chat_stream(app, body("下一轮", request_id="r2"))
    turns = await store.get_turns("u", "s")
    assert len(turns) == 2
    assert turns[0].assistant_message == "最终回答"
    assert replay.text == first.text and replay.request_id == first.request_id
    assert len(llm.decisions) == 2
    assert llm.decisions[1]["messages"][:2] == [
        {"role": "user", "content": "比较两笔金额"},
        {"role": "assistant", "content": "最终回答"},
    ]
    assert app.state.tool_manager.get_stats()["compare_amounts"]["total"] == 2


@pytest.mark.asyncio
async def test_failed_final_generation_returns_safe_done_without_persisting(memory_env):
    store, memory = memory_env
    llm = FakeLLM(failure=True)
    app = make_app(llm, memory=memory)
    response = await chat_stream(app, body(request_id="failed"))
    assert response.done["status"] == "failed" and not response.done["success"]
    assert response.done["error_code"] == "tool_answer_failed"
    assert "无法确认" in response.text and "secret" not in response.text
    assert await store.get_turns("u", "s") == []
    assert await store.get_receipt("u", "s", "failed") is None


@pytest.mark.asyncio
async def test_debug_trace_excludes_model_inputs_and_tool_data():
    response = await chat_stream(make_app(FakeLLM(), debug=True), body())
    assert response.done["tool_traces"][0]["name"] == "compare_amounts"
    assert "input" not in response.done["tool_traces"][0]
    assert "data" not in response.done["tool_traces"][0]


@pytest.mark.asyncio
async def test_selected_skill_is_injected_into_tool_and_final_requests(tmp_path):
    directory = tmp_path / "billing"
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        "---\nname: 账单核验\ndescription: 核验账单\n---\n\n正文_MARKER", encoding="utf-8",
    )
    llm = FakeLLM()
    app = make_app(llm, skills=SkillSettings(enabled=True, root_dir=str(tmp_path)))
    response = await chat_stream(app, {**body(), "skill_ids": ["billing"]})
    assert response.error is None
    assert "正文_MARKER" in llm.decisions[0]["system"]
    assert "正文_MARKER" in llm.finals[0]["system"]


@pytest.mark.asyncio
async def test_registry_initialization_failure_keeps_chat_available(monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("secret")

    monkeypatch.setattr("api.main.build_commerce_tools", fail)
    llm = FakeLLM()
    app = make_app(llm)
    assert app.state.tool_manager is None
    response = await chat_stream(app, body())
    assert response.text == "普通回答" and not llm.decisions


@pytest.mark.asyncio
async def test_cancellation_during_tool_does_not_commit_and_releases_conversation_lock(memory_env):
    store, memory = memory_env
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(params, context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    llm = FakeLLM(name="wait_tool", arguments={})
    app = make_app(llm, memory=memory)
    app.state.tool_manager.register(ToolSpec(
        "wait_tool", "等待", {"type": "object", "properties": {}}, handler,
    ))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/chat/stream")

    async def receive():
        await asyncio.Event().wait()

    request = Request({"type": "http", "app": app}, receive)
    response = await endpoint(ChatRequest(**body()), request)

    async def consume():
        async for _ in response.body_iterator:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and not llm.finals
    assert await store.get_turns("u", "s") == []
    async with asyncio.timeout(1):
        async with memory.conversation("u", "s"):
            pass


@pytest.mark.asyncio
async def test_generation_and_commit_hold_the_same_conversation_lock(memory_env):
    _, memory = memory_env
    held = False
    original_conversation, original_commit = memory.conversation, memory.commit

    @asynccontextmanager
    async def conversation(user, session):
        nonlocal held
        async with original_conversation(user, session):
            held = True
            try:
                yield
            finally:
                held = False

    async def commit(*args, **kwargs):
        assert held and kwargs["lock_held"]
        return await original_commit(*args, **kwargs)

    memory.conversation, memory.commit = conversation, commit
    response = await chat_stream(make_app(FakeLLM(), memory=memory), body())
    assert response.done["success"] and not held


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_postgres_gets_only_complete_successful_answer(failure):
    class Sessions:
        def __init__(self):
            self.writes = []

        async def find_receipt(self, *args):
            return None

        async def commit_turn(self, user, session, client_id, request_id, question, answer, state):
            self.writes.append((question, answer, state))
            return {"response": answer, "request_id": request_id}

    llm = FakeLLM(failure=failure)
    app = make_app(llm)
    sessions = app.state.sessions = Sessions()
    response = await chat_stream(app, body(request_id="db-r"))
    assert response.done["success"] is not failure
    assert sessions.writes == ([] if failure else [("比较两笔金额", "最终回答", None)])
