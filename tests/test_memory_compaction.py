"""全量上下文和 EchoMind 式压缩；真实 tokenizer + fake Redis/摘要模型。"""
import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from memory.budget import TokenBudget
from memory.compactor import MemoryCompactor
from memory.context import ContextBuilder
from memory.store import RedisMemoryStore
from memory.tokens import TokenCounter


@pytest_asyncio.fixture
async def store():
    client = FakeRedis(decode_responses=True)
    instance = RedisMemoryStore(client, ttl_s=120)
    try:
        yield instance
    finally:
        await instance.close()


async def seed(store, n=5, text="普通对话"):
    for i in range(n):
        await store.commit_turn(
            "u", "s", request_id=f"r{i}", user_message=f"问题{i}:{text}",
            assistant_message=f"回答{i}:{text}",
        )


class FakeSummary:
    def __init__(self, replies=None):
        self.prompts = []
        self.replies = list(replies if replies is not None else ["新增摘要", "合并摘要"])

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def compactor(store, summarize):
    return MemoryCompactor(store, summarize, budget=TokenBudget(compact_trigger_tokens=1))


@pytest.mark.asyncio
async def test_30_turns_all_in_context_no_hidden_limit(store):
    await seed(store, 30)
    await store.set_summary("u", "s", "之前的摘要")
    await store.set_state("u", "s", {"stage": "pending"})
    before = await store.get_snapshot("u", "s")
    context = await ContextBuilder(store).get_context("u", "s")
    assert len(context.to_messages()) == 60
    assert context.to_messages() == before.to_messages()
    assert len(context.to_messages("新问题")) == 61
    assert context.to_messages("新问题")[-1] == {"role": "user", "content": "新问题"}
    assert context.state == {"stage": "pending"}
    assert "之前的摘要" in context.to_system_text("基础指令")
    assert context.to_system_text("基础指令").startswith("基础指令")
    fake = FakeSummary()
    result = await MemoryCompactor(store, fake).compact("u", "s")
    assert result.status == "below_threshold"
    assert fake.prompts == []
    assert await store.get_snapshot("u", "s") == before


@pytest.mark.asyncio
async def test_empty_context(store):
    context = await ContextBuilder(store).get_context("u", "s")
    assert context.to_messages() == []
    assert context.to_system_text("system") == "system"
    assert context.state == {} and context.summary == ""


@pytest.mark.asyncio
async def test_15_tool_json_turns_only_summarize_before_last_two(store):
    # 当前 Turn 存纯文本；模拟回答中真实占空间的工具 JSON，不伪称原生 tool 链。
    payload = json.dumps({"orders": [{"物流": "包裹已发出，等待签收", "id": i} for i in range(100)]}, ensure_ascii=False)
    await seed(store, 15, payload)
    await store.set_state("u", "s", {"order": "active"})
    await store.set_summary("u", "s", "旧摘要")
    before = await store.get_snapshot("u", "s")
    assert len((await ContextBuilder(store).get_context("u", "s")).to_messages()) == 30
    fake = FakeSummary()
    result = await MemoryCompactor(store, fake).compact("u", "s")
    assert result.working_tokens >= 10800
    assert result.status == "compacted" and result.compressed_turns == 13
    # 首次摘要收到精确的旧消息列表，最近四条完全没有送入模型。
    assert json.loads(fake.prompts[0].split("\n", 1)[1]) == [m for t in before.turns[:-2] for m in t.to_messages()]
    assert json.loads(fake.prompts[1].split("\n", 1)[1]) == {"old_summary": "旧摘要", "new_summary": "新增摘要"}
    after = await store.get_snapshot("u", "s")
    assert after.turns == before.turns[-2:]
    assert after.summary == "合并摘要" and after.state == before.state
    for key in (store.working_key("u", "s"), store.summary_key("u", "s"), store.state_key("u", "s")):
        assert 0 < await store._client.ttl(key) <= 120


@pytest.mark.asyncio
@pytest.mark.parametrize("replies", [[RuntimeError("failed")], [" "], ["新增摘要", RuntimeError("merge failed")], ["新增摘要", ""]])
async def test_generation_or_merge_failure_preserves_all_originals(store, replies):
    await seed(store)
    await store.set_summary("u", "s", "旧摘要")
    before = await store.get_snapshot("u", "s")
    result = await compactor(store, FakeSummary(replies)).compact("u", "s")
    assert result.status == "failed"
    assert await store.get_snapshot("u", "s") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [1, 2])
async def test_only_recent_rounds_even_if_oversized_never_compressed(store, n):
    await seed(store, n, "汉" * 12000)
    before = await store.get_snapshot("u", "s")
    fake = FakeSummary()
    result = await MemoryCompactor(store, fake).compact("u", "s")
    assert result.status == "no_old_turns"
    assert fake.prompts == []
    assert await store.get_snapshot("u", "s") == before
    assert (await ContextBuilder(store).get_context("u", "s")).to_messages() == before.to_messages()


@pytest.mark.asyncio
async def test_background_append_during_summary_keeps_new_turn(store):
    await seed(store)
    before = await store.get_snapshot("u", "s")
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def summarize(prompt):
        entered.set()
        await proceed.wait()
        return "摘要"

    worker = compactor(store, summarize)
    task = worker.start("u", "s")
    assert worker.start("u", "s") is task
    await asyncio.wait_for(entered.wait(), 2)
    # 证明摘要过程中没有持有提交锁；新消息可以立即落库。
    new = await asyncio.wait_for(store.commit_turn(
        "u", "s", request_id="new", user_message="新增问题", assistant_message="新增回答",
    ), 2)
    proceed.set()
    assert (await task).status == "compacted"
    after = await store.get_snapshot("u", "s")
    assert after.turns == before.turns[-2:] + (new,)
    await worker.close()


@pytest.mark.asyncio
async def test_parallel_compactors_only_one_applies(store):
    await seed(store)
    entered = 0
    both_ready = asyncio.Event()

    async def summarize(prompt):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_ready.set()
        await both_ready.wait()
        return "摘要"

    results = await asyncio.gather(compactor(store, summarize).compact("u", "s"), compactor(store, summarize).compact("u", "s"))
    assert sorted(r.status for r in results) == ["compacted", "conflict"]
    assert len(await store.get_turns("u", "s")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["summary", "expired", "history"])
async def test_changed_or_expired_snapshot_not_overwritten(store, mutation):
    await seed(store)

    async def summarize(prompt):
        if mutation == "summary":
            await store.set_summary("u", "s", "其他任务摘要")
        elif mutation == "expired":
            await store._client.delete(store.working_key("u", "s"), store.summary_key("u", "s"))
        else:
            await store._client.ltrim(store.working_key("u", "s"), 0, 2)
        return "不应提交"

    result = await compactor(store, summarize).compact("u", "s")
    assert result.status == "conflict"
    assert await store.get_summary("u", "s") != "不应提交"
    if mutation == "expired":
        assert await store.get_turns("u", "s") == []


@pytest.mark.asyncio
async def test_timeout_and_shutdown_leave_originals(store):
    await seed(store)
    before = await store.get_snapshot("u", "s")
    entered = asyncio.Event()

    async def slow(prompt):
        entered.set()
        await asyncio.Event().wait()

    worker = MemoryCompactor(store, slow, budget=TokenBudget(1), timeout_s=0.05)
    assert (await worker.compact("u", "s")).status == "failed"
    entered.clear()
    task = worker.start("u", "s")
    await asyncio.wait_for(entered.wait(), 2)
    await worker.close()
    assert task.cancelled()
    assert await store.get_snapshot("u", "s") == before
    with pytest.raises(RuntimeError):
        worker.start("u", "s")


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
async def test_real_llm_adapter_rejects_truncated_summary(stop_reason):
    from core.config import Settings
    from core.llm import LLMClient, LLMError
    llm = LLMClient(Settings(api_key="test-key"))
    async def fake_create(**kwargs):
        assert kwargs["system"] != ""
        return SimpleNamespace(content=[{"type": "text", "text": "摘要"}], stop_reason=stop_reason)
    llm._client.messages.create = fake_create
    try:
        if stop_reason == "max_tokens":
            with pytest.raises(LLMError, match="截断"):
                await llm.summarize("旧消息")
        else:
            assert await llm.summarize("旧消息") == "摘要"
    finally:
        await llm._client.close()
