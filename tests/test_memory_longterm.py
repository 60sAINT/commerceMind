import asyncio
import json
import math
import os
import uuid

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from memory.budget import TokenBudget
from memory.compactor import MemoryCompactor
from memory.longterm import LongTermMemory, SiliconFlowEmbedding
from memory.models import Episode, Turn, memory_identity
from memory.redaction import redact
from memory.store import RedisMemoryStore
from memory.tokens import TokenCounter


class FakeCollection:
    metadata = {"hnsw:space": "cosine", "embedding_id": "fake:v1"}
    name = "episodic_fake"
    def __init__(self):
        self.rows = {}
        self.response = None
        self.fail = False
    def upsert(self, ids, documents, metadatas, embeddings):
        if self.fail:
            raise RuntimeError("offline")
        for eid, doc, meta, vector in zip(ids, documents, metadatas, embeddings):
            self.rows[eid] = (doc, meta, vector)
    def query(self, **kwargs):
        self.query_args = kwargs
        if self.fail:
            raise RuntimeError("offline")
        if self.response is not None:
            return self.response
        return {"ids": [list(self.rows)], "documents": [[r[0] for r in self.rows.values()]],
                "metadatas": [[r[1] for r in self.rows.values()]], "distances": [[0.1]*len(self.rows)]}


class FakeEmbedding:
    def __init__(self):
        self.inputs = []
    def __call__(self, texts):
        self.inputs.extend(texts)
        return [[1., 0., 0.] for _ in texts]


def episode(user="u", session="old", text="用户咨询运动鞋退货", n=2):
    turns = tuple(Turn(f"r{i}", "问题", "回答") for i in range(n))
    return Episode.from_turns(user, session, turns, text)


def memory(collection=None, embed=None, **kwargs):
    return LongTermMemory(collection or FakeCollection(), embed or FakeEmbedding(), "fake:v1", **kwargs)


@pytest.mark.parametrize("text", ["13800138000", "+86 13800138000", "a.b@example.com", "11010519491231002X", "110105491231002"])
def test_redaction(text):
    assert text not in redact("联系 " + text)
    assert redact(redact(text)) == redact(text)
    assert redact("订单 CM20261110001 已发出") == "订单 CM20261110001 已发出"


@pytest.mark.asyncio
async def test_write_idempotent_and_redacted_before_embedding():
    c, embed = FakeCollection(), FakeEmbedding()
    m = memory(c, embed)
    e = episode(text="手机号13800138000，邮箱a@example.com")
    assert await m.add_episode(e)
    assert await m.add_episode(e)
    assert len(c.rows) == 1
    doc, meta, vector = c.rows[e.episode_id]
    assert "13800138000" not in doc and "a@example.com" not in doc
    assert all("13800138000" not in x for x in embed.inputs)
    assert meta["user_key"] == memory_identity("u") and meta["session_key"] == memory_identity("old")
    assert "user_id" not in meta and vector == [1., 0., 0.]
    assert not await m.add_episode(episode(n=1))


@pytest.mark.asyncio
async def test_filter_threshold_sort_dedup_and_token_budget():
    c, embed = FakeCollection(), FakeEmbedding()
    user, session = memory_identity("u"), memory_identity("now")
    def meta(u=user, s=memory_identity("old")):
        return {"user_key": u, "session_key": s}
    docs = ["其他用户", "当前会话", "低相关", "高相关"*500, "高相关"*500, "边界"]
    c.response = {"ids": [["other", "current", "low", "a", "dup", "edge"]],
                  "documents": [docs], "metadatas": [[meta("other"), meta(s=session), meta(), meta(), meta(), meta()]],
                  "distances": [[0., 0., .9, .1, .1, .45]]}
    m = memory(c, embed, max_tokens=40)
    result = await m.search("u", "now", "电话13800138000")
    assert result and result[0].episode_id == "a"
    assert all(r.episode_id not in ("other", "current", "low", "dup") for r in result)
    assert TokenCounter().count_text("\n\n".join(r.text for r in result)) <= 40
    assert "13800138000" not in embed.inputs[-1]
    assert c.query_args["where"] == {"$and": [{"user_key": {"$eq": user}}, {"session_key": {"$ne": session}}]}


@pytest.mark.asyncio
async def test_score_boundary_topk_and_search_failure():
    c = FakeCollection()
    m = memory(c, top_k=1)
    await m.add_episode(episode())
    c.response = {"ids": [["a", "b"]], "documents": [["第一", "第二"]],
                  "metadatas": [[{"user_key": memory_identity("u"), "session_key": "old"}]*2], "distances": [[.45,.46]]}
    r = await m.search("u", "now", "问题")
    assert len(r) == 1 and r[0].score == pytest.approx(.55)
    c.fail = True
    assert await m.search("u", "now", "问题") == []
    assert await m.search("u", "now", " ") == []


@pytest.mark.parametrize("kwargs", [{"top_k": 0}, {"max_tokens": True}, {"min_score": float("nan")}, {"min_turns": -1}])
def test_invalid_config(kwargs):
    with pytest.raises(ValueError):
        memory(**kwargs)


def test_embedding_collection_mismatch():
    with pytest.raises(ValueError):
        LongTermMemory(FakeCollection(), FakeEmbedding(), "different")


@pytest.mark.parametrize("vector", [[[float("nan")]], [[]], [[0.,0.]], []])
def test_invalid_vectors(vector):
    with pytest.raises(ValueError):
        memory(embed=lambda texts: vector)._vectors(["text"])


def test_token_truncation_including_separator():
    counter = TokenCounter()
    text = "退款流程，订单ABC123。" * 100
    out = counter.truncate_text(text, 30, prefix="前文\n\n")
    assert text.startswith(out) and counter.count_text("前文\n\n" + out) <= 30


@pytest_asyncio.fixture
async def store():
    client = FakeRedis(decode_responses=True)
    s = RedisMemoryStore(client)
    try:
        yield s
    finally:
        await s.close()


async def seed(s):
    for i in range(5):
        await s.commit_turn("u", "s", request_id=str(i), user_message=f"问题{i}", assistant_message=f"回答{i}")
    await s.set_summary("u", "s", "旧摘要")


@pytest.mark.asyncio
async def test_compaction_outbox_retry_uses_new_summary_not_merged(store):
    await seed(store)
    c = FakeCollection(); c.fail = True
    m = memory(c)
    replies = iter(["本次手机号13800138000咨询退货", "合并后的累计摘要"])
    async def summarize(prompt):
        return next(replies)
    worker = MemoryCompactor(store, summarize, budget=TokenBudget(1), longterm=m)
    assert (await worker.compact("u", "s")).status == "compacted"
    pending = await store.get_pending_episodes()
    assert len(pending) == 1 and "13800138000" not in pending[0].summary
    assert pending[0].summary != "合并后的累计摘要"
    assert len(await store.get_turns("u", "s")) == 2
    assert await store.get_summary("u", "s") == "合并后的累计摘要"
    c.fail = False
    assert await m.flush_pending(store) == 1
    assert await store.get_pending_episodes() == []
    assert len(c.rows) == 1


@pytest.mark.asyncio
async def test_failed_or_conflicted_compaction_never_enqueues_episode(store):
    await seed(store)
    async def fail(prompt):
        raise RuntimeError("model error")
    assert (await MemoryCompactor(store, fail, budget=TokenBudget(1)).compact("u", "s")).status == "failed"
    assert not await store.get_pending_episodes()
    calls = 0
    async def conflict(prompt):
        nonlocal calls
        calls += 1
        if calls == 2:
            await store.set_summary("u", "s", "其他摘要")
        return "摘要"
    assert (await MemoryCompactor(store, conflict, budget=TokenBudget(1)).compact("u", "s")).status == "conflict"
    assert not await store.get_pending_episodes()


@pytest.mark.asyncio
async def test_successful_outbox_without_llm_or_redis_server(store):
    await seed(store)
    async def summarize(prompt):
        return "摘要"
    m = memory()
    result = await MemoryCompactor(store, summarize, budget=TokenBudget(1), longterm=m).compact("u", "s")
    assert result.status == "compacted"
    assert not await store.get_pending_episodes()
    assert len(m.collection.rows) == 1


@pytest.mark.asyncio
async def test_real_chroma_optional():
    if os.getenv("CHROMA_INTEGRATION") != "1":
        pytest.skip("设置 CHROMA_INTEGRATION=1，连接真实 Chroma；使用 fake embedding 不下载模型")
    import chromadb
    client = chromadb.HttpClient(host=os.getenv("CHROMA_HOST", "127.0.0.1"), port=int(os.getenv("CHROMA_PORT", "8001")))
    name = "episodic_test_" + uuid.uuid4().hex
    c = client.create_collection(name, metadata={"hnsw:space": "cosine", "embedding_id": "fake:v1"}, embedding_function=None)
    try:
        m = memory(c)
        await m.add_episode(episode())
        assert c.count() == 1
        assert len(await m.search("u", "new-session", "退货")) == 1
    finally:
        client.delete_collection(name)


def test_siliconflow_request_and_response_order(monkeypatch):
    import httpx
    def handle(request):
        assert request.url == "https://api.siliconflow.cn/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload == {"model": "BAAI/bge-m3", "input": ["脱敏文本一", "脱敏文本二"], "encoding_format": "float"}
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]},
        ]})
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    embed = SiliconFlowEmbedding("test-key", "BAAI/bge-m3", "https://api.siliconflow.cn/v1/embeddings")
    assert embed(["脱敏文本一", "脱敏文本二"]) == [[1., 0.], [0., 1.]]
