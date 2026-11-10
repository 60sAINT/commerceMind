import asyncio
import json

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from pydantic import ValidationError

from memory.longterm import LongTermMemory
from memory.models import Episode, Turn, memory_identity
from memory.profile import (Entities, ProfileUpdater, UserProfile, MAX_RESPONSE_CHARS,
                            merge_profile, parse_profile, profile_to_embedding_text)
from memory.store import RedisMemoryStore, PENDING_PROFILES_KEY, PENDING_EPISODES_KEY


class Collection:
    metadata = {"profile_schema": "v2", "hnsw:space": "cosine", "embedding_id": "profile-fake:v1"}
    def __init__(self):
        self.rows = {}
        self.fail = False
        self.writes = 0
    def get(self, ids, include):
        if self.fail:
            raise RuntimeError("private exception 13800138000")
        return {"documents": [self.rows[i][0] for i in ids if i in self.rows]}
    def upsert(self, ids, documents, metadatas, embeddings):
        if self.fail:
            raise RuntimeError("private exception 13800138000")
        self.writes += 1
        for i, d, m, v in zip(ids, documents, metadatas, embeddings):
            self.rows[i] = (d, m, v)


class Embedding:
    def __init__(self):
        self.inputs = []
        self.fail = False
    def __call__(self, texts):
        self.inputs.extend(texts)
        if self.fail:
            raise RuntimeError("embedding offline 13800138000")
        return [[1., 0., 0.] for _ in texts]


def make_updater(store, collection, extract, **kwargs):
    kwargs.setdefault("embed", Embedding())
    kwargs.setdefault("embedding_id", "profile-fake:v1")
    return ProfileUpdater(store, collection, extract, **kwargs)


def episode(user="u", session="s", summary="用户偏好宽松版型"):
    return Episode.from_turns(user, session,
        tuple(Turn(str(i), "问", "答") for i in range(2)), summary)


@pytest_asyncio.fixture
async def store():
    s = RedisMemoryStore(FakeRedis(decode_responses=True))
    yield s
    await s.close()


async def enqueue(store, e):
    # 模拟 Chroma 写成功后的事务交接；真正调用点由下面的 longterm 联动测试覆盖。
    await store.ack_episode(e.episode_id, profile_episode=e)


@pytest.mark.parametrize("raw", ['{}', '{"preferences":[" 宽松 "]}',
    '```json\n{"entities":{"products":["运动鞋"]}}\n```'])
def test_valid_json(raw):
    assert isinstance(parse_profile(raw), UserProfile)


@pytest.mark.parametrize("raw", ['not json', '说明：{"preferences":[]}', '[]', 'null',
    '{"preferences":"宽松"}', '{"preferences":[1]}', '{"name":"小明"}',
    '{"entities":{"address":"家"}}', '{"preferences":["' + '长'*81 + '"]}',
    json.dumps({"preferences": [str(i) for i in range(11)]}),
    '{"preferences":[],"preferences":[]}', ' '* (MAX_RESPONSE_CHARS+1)])
def test_invalid_json_and_limits(raw):
    with pytest.raises((ValueError, ValidationError)):
        parse_profile(raw)


def test_merge_dedup_limit_empty_and_immutability():
    old = UserProfile(preferences=["旧"+str(i) for i in range(10)])
    patch = UserProfile(preferences=[" 新 ", "NEW", "new"])
    result = merge_profile(old, patch)
    assert result.preferences == ["新", "NEW"] + old.preferences[:8]
    assert merge_profile(result, patch) == result
    assert merge_profile(result, UserProfile()) == result
    assert len(old.preferences) == 10 and patch.preferences[0] == " 新 "
    entity = merge_profile(UserProfile(entities=Entities(products=["鞋"])),
        UserProfile(entities=Entities(products=["鞋", "包"], issue_types=["退货"])))
    assert entity.entities.products == ["鞋", "包"]
    assert entity.entities.issue_types == ["退货"]


@pytest.mark.parametrize("secret", ["13800138000", "a@example.com", "11010519491231002X",
    "１３８００１３８０００", "姓名小明", "详细地址北京某路1号", "银行卡6222000000000000",
    "password secret", "sk-abcdefgh12345", "邮箱[邮箱]"])
def test_sensitive_items_removed(secret):
    value = parse_profile(json.dumps({"preferences": ["宽松", secret],
        "entities": {"products": [secret], "issue_types": [secret]}}, ensure_ascii=False))
    assert value == UserProfile(preferences=["宽松"])


@pytest.mark.asyncio
async def test_update_only_after_episode_and_no_model_for_read(store):
    calls = []
    async def extract(prompt):
        calls.append(prompt)
        return '{"preferences":["宽松"]}'
    c = Collection(); worker = make_updater(store, c, extract)
    e = episode()
    assert await worker.update_from_episode(e) == "not_pending"
    assert not calls and not c.rows
    assert await worker.get_profile("u") == UserProfile()
    await enqueue(store, e)
    assert await worker.flush_pending() == 1
    assert (await worker.get_profile("u")).preferences == ["宽松"]
    assert await worker.get_profile("other") == UserProfile()
    assert len(calls) == 1
    assert not await store.get_pending_profiles()
    assert await store.profile_applied(e.episode_id)
    await enqueue(store, e)  # episode 重复交接也不能再次调用模型或写画像。
    assert await worker.flush_pending() == 1
    assert len(calls) == 1 and c.writes == 1
    row = c.rows["profile:" + memory_identity("u")]
    assert row[2] == [1., 0., 0.] and "user_id" not in row[1]


@pytest.mark.asyncio
async def test_failure_retry_and_private_data_never_persisted_or_logged(store, caplog):
    prompts = []
    async def extract(prompt):
        prompts.append(prompt)
        return '{"preferences":["宽松","手机号13800138000","邮箱a@example.com"]}'
    c = Collection(); worker = make_updater(store, c, extract)
    e = episode(summary="用户喜欢宽松，手机号13800138000，邮箱a@example.com")
    await enqueue(store, e)
    pending_json = await store._client.hget(PENDING_PROFILES_KEY, e.episode_id)
    assert "13800138000" not in pending_json and "a@example.com" not in pending_json
    c.fail = True
    assert await worker.update_from_episode(e) == "failed"
    assert await store.get_pending_profile(e.episode_id)
    assert not await store.profile_applied(e.episode_id)
    c.fail = False
    assert await worker.update_from_episode(e) == "updated"
    values = json.dumps(c.rows, ensure_ascii=False) + caplog.text + ''.join(prompts)
    for secret in ["13800138000", "a@example.com"]:
        assert secret not in values
        assert secret not in (await store._client.hget(PENDING_PROFILES_KEY, e.episode_id) or "")


@pytest.mark.asyncio
async def test_invalid_model_timeout_and_corrupt_storage_preserve_previous(store):
    async def valid(prompt):
        return '{"preferences":["宽松"]}'
    c = Collection(); worker = make_updater(store, c, valid)
    e = episode(); await enqueue(store, e); await worker.flush_pending()
    before = dict(c.rows)
    second = episode(session="s2"); await enqueue(store, second)
    async def invalid(prompt):
        return '{"preferences":"错误"}'
    worker.extract = invalid
    assert await worker.update_from_episode(second) == "failed"
    assert c.rows == before and await store.get_pending_profile(second.episode_id)
    async def slow(prompt):
        await asyncio.sleep(1)
        return '{}'
    worker.extract = slow; worker.timeout_s = .001
    assert await worker.update_from_episode(second) == "failed"
    assert c.rows == before
    worker.extract = valid
    key = next(iter(c.rows)); c.rows[key] = ('{"bad":true}', {}, [1.0])
    assert await worker.update_from_episode(second) == "failed"
    assert c.rows[key][0] == '{"bad":true}'


@pytest.mark.asyncio
async def test_concurrent_sessions_read_latest_under_user_lock(store):
    c = Collection()
    async def extract(prompt):
        await asyncio.sleep(.01)
        return json.dumps({"preferences": ["宽松" if "宽松" in prompt else "棉质"]})
    # 两个实例共用 Redis，但本地锁不同，验证跨实例 Redis 用户锁。
    other_store = RedisMemoryStore(store._client)
    a, b = make_updater(store, c, extract), make_updater(other_store, c, extract)
    ea, eb = episode(), episode(session="s2", summary="用户偏好棉质")
    await enqueue(store, ea); await enqueue(store, eb)
    assert await asyncio.gather(a.update_from_episode(ea), b.update_from_episode(eb)) == ["updated", "updated"]
    assert set((await a.get_profile("u")).preferences) == {"宽松", "棉质"}
    assert len(c.rows) == 1
    ec = episode(user="other"); await enqueue(store, ec)
    assert await a.update_from_episode(ec) == "updated"
    assert len(c.rows) == 2


@pytest.mark.asyncio
async def test_chroma_success_then_redis_ack_failure_recovers_without_second_write(store, monkeypatch):
    async def extract(prompt):
        return '{"preferences":["宽松"]}'
    c = Collection(); worker = make_updater(store, c, extract)
    e = episode(); await enqueue(store, e)
    ack = store.ack_profile
    async def fail(eid):
        raise RuntimeError("offline")
    monkeypatch.setattr(store, "ack_profile", fail)
    assert await worker.update_from_episode(e) == "failed"
    assert c.writes == 1 and await store.get_pending_profile(e.episode_id)
    monkeypatch.setattr(store, "ack_profile", ack)
    assert await worker.update_from_episode(e) == "already_applied"
    assert c.writes == 1 and not await store.get_pending_profiles()


@pytest.mark.asyncio
async def test_longterm_handoff_only_on_success_and_retry_without_new_episode(store):
    class EpisodeCollection(Collection):
        metadata = {"hnsw:space": "cosine", "embedding_id": "fake"}
    ec, pc = EpisodeCollection(), Collection()
    replies = iter(['bad json', '{"preferences":["宽松"]}'])
    async def extract(prompt):
        return next(replies)
    updater = make_updater(store, pc, extract)
    m = LongTermMemory(ec, lambda texts: [[1.0] for _ in texts], "fake", profile_updater=updater)
    e = episode()
    await store._client.hset(PENDING_EPISODES_KEY, e.episode_id, json.dumps(e.to_dict()))
    ec.fail = True
    assert await m.flush_pending(store) == 0
    assert not await store.get_pending_profiles() and not pc.rows
    ec.fail = False
    assert await m.flush_pending(store) == 1
    assert not await store.get_pending_episodes()
    assert await store.get_pending_profile(e.episode_id) and not pc.rows
    assert await m.flush_pending(store) == 0  # 没有新 episode 仍重试画像待办。
    assert (await updater.get_profile("u")).preferences == ["宽松"]


@pytest.mark.asyncio
async def test_cancel_keeps_pending(store):
    async def cancel(prompt):
        raise asyncio.CancelledError
    worker = make_updater(store, Collection(), cancel)
    e = episode(); await enqueue(store, e)
    with pytest.raises(asyncio.CancelledError):
        await worker.update_from_episode(e)
    assert await store.get_pending_profile(e.episode_id)


@pytest.mark.asyncio
async def test_previous_receipt_repaired_before_another_episode_overwrites_profile(store, monkeypatch):
    async def extract(prompt):
        return '{"preferences":["宽松"]}'
    c = Collection(); worker = make_updater(store, c, extract)
    ea, eb = episode(), episode(session="second")
    await enqueue(store, ea); await enqueue(store, eb)
    ack = store.ack_profile
    async def fail(eid):
        raise RuntimeError("offline")
    monkeypatch.setattr(store, "ack_profile", fail)
    assert await worker.update_from_episode(ea) == "failed"
    monkeypatch.setattr(store, "ack_profile", ack)
    assert await worker.update_from_episode(eb) == "unchanged"
    assert await store.profile_applied(ea.episode_id)
    assert await store.profile_applied(eb.episode_id)
    assert not await store.get_pending_profiles()


@pytest.mark.asyncio
async def test_profile_llm_json_system_and_truncated_response():
    from types import SimpleNamespace
    from core.config import Settings
    from core.llm import LLMClient, LLMError
    llm = LLMClient(Settings(api_key="test-key", model="fake"))
    async def create(**kwargs):
        assert "JSON" in kwargs["system"] and "摘要正文" not in kwargs["system"]
        return SimpleNamespace(content=[{"type": "text", "text": '{}'}], stop_reason="max_tokens")
    llm._client.messages.create = create
    try:
        with pytest.raises(LLMError):
            await llm.extract_profile("test")
    finally:
        await llm._client.close()


@pytest.mark.asyncio
async def test_real_profile_chroma_optional(store):
    import os
    if os.getenv("CHROMA_INTEGRATION") != "1":
        pytest.skip("设置 CHROMA_INTEGRATION=1，使用临时 collection 验证真实 Chroma")
    import chromadb
    import uuid
    from chromadb.config import Settings
    client = chromadb.HttpClient(host=os.getenv("CHROMA_HOST", "127.0.0.1"),
        port=int(os.getenv("CHROMA_PORT", "8001")), settings=Settings(anonymized_telemetry=False))
    name = "profile_test_" + uuid.uuid4().hex
    c = client.create_collection(name, metadata=Collection.metadata, embedding_function=None)
    async def extract(prompt):
        return '{"preferences":["宽松","手机号13800138000"]}'
    try:
        worker = make_updater(store, c, extract)
        e = episode(); await enqueue(store, e)
        assert await worker.flush_pending() == 1
        assert (await worker.get_profile("u")).preferences == ["宽松"]
        await enqueue(store, e)
        await worker.flush_pending()
        assert c.count() == 1
        raw = c.get(include=["documents", "metadatas", "embeddings"])
        assert raw["embeddings"].tolist() == [[1., 0., 0.]]
        assert "13800138000" not in json.dumps(raw["documents"])
        own = await worker.search("u", "宽松")
        assert len(own) == 1 and own[0].user_key == memory_identity("u")
        other = episode(user="other"); await enqueue(store, other)
        await worker.flush_pending()
        matches = await worker.find_similar_users("u", top_k=1)
        assert len(matches) == 1 and matches[0].user_key == memory_identity("other")
        assert matches[0].score == pytest.approx(1.)
        # 实测旧一维 collection 与新三维 collection 的显式迁移。
        legacy_name = "profile_legacy_test_" + uuid.uuid4().hex
        legacy = client.create_collection(legacy_name, metadata={"profile_schema": "v1"}, embedding_function=None)
        try:
            old = episode(user="legacy-user")
            legacy.upsert(ids=["profile:" + old.user_key], documents=[json.dumps({
                "profile": {"preferences": ["棉质"]}, "last_episode_id": old.episode_id})],
                metadatas=[{"user_key": old.user_key}], embeddings=[[1.0]])
            worker.legacy_collection = legacy
            assert await worker.reindex_user("legacy-user")
            assert (await worker.get_profile("legacy-user")).preferences == ["棉质"]
            assert legacy.count() == 1 and c.count() == 3
        finally:
            client.delete_collection(legacy_name)
    finally:
        client.delete_collection(name)


@pytest.mark.asyncio
async def test_cancel_during_write_waits_for_thread_before_unlock(store):
    import threading
    started, release = threading.Event(), threading.Event()
    class SlowCollection(Collection):
        def upsert(self, **kwargs):
            started.set()
            release.wait(2)
            super().upsert(**kwargs)
    async def extract(prompt):
        return '{"preferences":["宽松"]}'
    c = SlowCollection(); worker = make_updater(store, c, extract)
    e = episode(); await enqueue(store, e)
    task = asyncio.create_task(worker.update_from_episode(e))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await store.get_pending_profile(e.episode_id)
    assert await worker.update_from_episode(e) == "already_applied"
    assert c.writes == 1


def test_semantic_text_only_business_fields_stable_order_and_pii_filter():
    p = UserProfile(preferences=["宽松", "棉质", "13800138000"],
                    entities=Entities(products=["运动鞋"], issue_types=["退换货"]))
    text = profile_to_embedding_text(p)
    assert text == "长期偏好：宽松；棉质\n关注商品类别：运动鞋\n咨询问题类别：退换货"
    assert profile_to_embedding_text(UserProfile(preferences=["棉质", "宽松"], entities=p.entities)) == text
    assert profile_to_embedding_text(UserProfile()) == ""
    assert "13800138000" not in text


@pytest.mark.asyncio
async def test_embedding_uses_merged_profile_not_patch_or_metadata(store):
    embed, c = Embedding(), Collection()
    replies = iter(['{"preferences":["宽松"]}', '{"entities":{"products":["运动鞋"]}}'])
    async def extract(prompt):
        return next(replies)
    worker = make_updater(store, c, extract, embed=embed)
    for e in [episode(), episode(session="second")]:
        await enqueue(store, e)
        assert await worker.update_from_episode(e) == "updated"
    assert embed.inputs == ["长期偏好：宽松", "长期偏好：宽松\n关注商品类别：运动鞋"]
    assert all("user_key" not in t and "episode" not in t and "updated_at" not in t for t in embed.inputs)
    doc, meta, vector = c.rows["profile:" + memory_identity("u")]
    assert json.loads(doc)["semantic_text"] == embed.inputs[-1]
    assert meta["embedding_id"] == "profile-fake:v1"
    assert vector == [1., 0., 0.]


@pytest.mark.asyncio
async def test_embedding_failure_keeps_old_and_pending_and_no_secrets_logged(store, caplog):
    embed, c = Embedding(), Collection()
    async def extract(prompt):
        return '{"preferences":["宽松","手机号13800138000"]}'
    worker = make_updater(store, c, extract, embed=embed)
    first = episode(); await enqueue(store, first); await worker.flush_pending()
    before = dict(c.rows)
    second = episode(session="another"); await enqueue(store, second)
    embed.fail = True
    assert await worker.update_from_episode(second) == "failed"
    assert c.rows == before and await store.get_pending_profile(second.episode_id)
    assert not await store.profile_applied(second.episode_id)
    assert "13800138000" not in caplog.text + "".join(embed.inputs)
    embed.fail = False
    assert await worker.update_from_episode(second) == "unchanged"


@pytest.mark.asyncio
async def test_empty_profile_no_vector_no_row_but_acknowledged(store):
    embed, c = Embedding(), Collection()
    async def extract(prompt):
        return '{}'
    worker = make_updater(store, c, extract, embed=embed)
    e = episode(); await enqueue(store, e)
    assert await worker.update_from_episode(e) == "unchanged"
    assert not embed.inputs and not c.rows
    assert await store.profile_applied(e.episode_id)


@pytest.mark.parametrize("vectors", [[[0., 0.]], [[float("nan")]], [[]], []])
@pytest.mark.asyncio
async def test_bad_profile_vectors_not_written(store, vectors):
    async def extract(prompt):
        return '{"preferences":["宽松"]}'
    c = Collection(); worker = make_updater(store, c, extract, embed=lambda texts: vectors)
    e = episode(); await enqueue(store, e)
    assert await worker.update_from_episode(e) == "failed"
    assert not c.rows and await store.get_pending_profile(e.episode_id)


def test_profile_model_namespace_mismatch_rejected():
    async def extract(prompt):
        return '{}'
    with pytest.raises(ValueError):
        make_updater(None, Collection(), extract, embedding_id="other")
    c = Collection(); c.metadata = {"profile_schema": "v1"}
    with pytest.raises(ValueError):
        make_updater(None, c, extract)


@pytest.mark.asyncio
async def test_personal_search_and_similar_users_are_separate_and_defensive(store):
    class QueryCollection(Collection):
        response = None
        def query(self, **kwargs):
            self.query_args = kwargs
            if self.fail:
                raise RuntimeError("private 13800138000")
            return self.response
    c, embed = QueryCollection(), Embedding()
    async def extract(prompt):
        return '{"preferences":["宽松"]}'
    worker = make_updater(store, c, extract, embed=embed)
    current, other, low = memory_identity("u"), memory_identity("other"), memory_identity("low")
    e = episode(); await enqueue(store, e); await worker.flush_pending()
    doc = c.rows["profile:" + current][0]
    def meta(key, model="profile-fake:v1"):
        return {"user_key": key, "embedding_id": model}
    c.response = {"ids": [["profile:" + key for key in [other, current, low, other]]],
                  "documents": [[doc]*4], "metadatas": [[meta(other), meta(current), meta(low), meta(other)]],
                  "distances": [[.1, .2, .9, .1]]}
    own = await worker.search("u", "电话13800138000，宽松版型")
    assert [hit.user_key for hit in own] == [current]
    assert c.query_args["where"] == {"user_key": {"$eq": current}}
    assert "13800138000" not in embed.inputs[-1]
    similar = await worker.find_similar_users("u")
    assert [hit.user_key for hit in similar] == [other]
    assert c.query_args["where"] == {"user_key": {"$ne": current}}
    assert similar[0].score == pytest.approx(.9)
    assert "长期偏好：宽松" == embed.inputs[-1]
    c.fail = True
    assert await worker.search("u", "宽松") == []
    assert await worker.search("u", " ") == []
    with pytest.raises(ValueError):
        await worker.find_similar_users("u", top_k=0)


@pytest.mark.asyncio
async def test_legacy_read_and_reindex_do_not_call_llm_or_overwrite_new(store):
    old, new, embed = Collection(), Collection(), Embedding()
    e = episode(); key = "profile:" + e.user_key
    old.rows[key] = (json.dumps({"profile": {"preferences": ["宽松", "手机号13800138000"]},
                               "last_episode_id": e.episode_id}), {}, [1.0])
    async def no_llm(prompt):
        raise AssertionError("migration must not call LLM")
    worker = make_updater(store, new, no_llm, embed=embed, legacy_collection=old)
    assert (await worker.get_profile("u")).preferences == ["宽松"]
    assert not new.rows
    assert await worker.reindex_user("u")
    assert new.rows[key][2] == [1., 0., 0.] and old.rows[key][2] == [1.0]
    assert embed.inputs == ["长期偏好：宽松"]
    assert not await worker.reindex_user("u")
    assert not await worker.reindex_user("missing")
    # 迁移不修改 episode 已处理回执，模型切换后的重建仍可独立进行。
    assert not await store.profile_applied(e.episode_id)


@pytest.mark.asyncio
async def test_personal_search_rejects_wrong_model_corrupt_doc_and_nan_distance(store):
    class QueryCollection(Collection):
        def query(self, **kwargs):
            key = memory_identity("u")
            valid = json.dumps({"profile": {"preferences": ["宽松"]}, "last_episode_id": "a"*64})
            return {"ids": [["profile:"+key]*3], "documents": [[valid, 'bad', valid]],
                    "metadatas": [[{"user_key": key, "embedding_id": "other"},
                                   {"user_key": key, "embedding_id": "profile-fake:v1"},
                                   {"user_key": key, "embedding_id": "profile-fake:v1"}]],
                    "distances": [[0., .1, float("nan")]]}
    async def extract(prompt):
        return '{}'
    worker = make_updater(store, QueryCollection(), extract)
    assert await worker.search("u", "宽松") == []


def test_shared_embedding_configuration(monkeypatch):
    from memory.embeddings import embedding_from_env, SiliconFlowEmbedding
    monkeypatch.setenv("COMMERCEMIND_MEMORY_EMBEDDING_PROVIDER", "auto")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "fake-test-key")
    monkeypatch.setenv("COMMERCEMIND_MEMORY_EMBEDDING_MODEL", "BAAI/bge-m3")
    name, embed = embedding_from_env()
    assert name == "siliconflow:BAAI/bge-m3" and isinstance(embed, SiliconFlowEmbedding)
    monkeypatch.setenv("COMMERCEMIND_MEMORY_EMBEDDING_PROVIDER", "siliconflow")
    monkeypatch.delenv("SILICONFLOW_API_KEY")
    with pytest.raises(ValueError):
        embedding_from_env()
    monkeypatch.setenv("COMMERCEMIND_MEMORY_EMBEDDING_PROVIDER", "unknown")
    with pytest.raises(ValueError):
        embedding_from_env()
