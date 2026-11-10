import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from memory.budget import TokenBudget
from memory.compactor import MemoryCompactor
from memory.maintainer import MemoryMaintainer
from memory.models import Episode, Turn
from memory.store import RedisMemoryStore, PENDING_EPISODES_KEY
from memory.longterm import LongTermMemory


@pytest_asyncio.fixture
async def store():
    s = RedisMemoryStore(FakeRedis(decode_responses=True))
    yield s
    await s.close()


async def seed(s, session="s", n=5):
    for i in range(n):
        await s.commit_turn("u", session, request_id=str(i), user_message=f"问题{i}", assistant_message=f"回答{i}")


class StubCompactor:
    def __init__(self, statuses=None):
        self.calls = []
        self.statuses = list(statuses or ["below_threshold"])
    async def compact(self, user, session, *, flush_pending):
        assert flush_pending is False
        self.calls.append((user, session))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if isinstance(status, Exception):
            raise status
        return SimpleNamespace(status=status)


def maintainer(c, **kwargs):
    kwargs.setdefault("retry_delay_s", .001)
    kwargs.setdefault("pending_interval_s", .01)
    return MemoryMaintainer(c, **kwargs)


@pytest.mark.asyncio
async def test_queue_bounded_queued_dedup_and_lifecycle():
    c = StubCompactor(); m = maintainer(c, workers=1, max_pending=2)
    assert m.submit("u", "s") == "stopped"
    m.start(); m.start()
    assert len(m._tasks) == 1
    # 没有 await，worker 尚未拿到任务。
    assert m.submit("u", "s") == "queued"
    for _ in range(100):
        assert m.submit("u", "s") == "coalesced"
    assert m.submit("u", "other") == "queued"
    assert m.submit("u", "overflow") == "full"
    assert m.stats()["pending"] == 2
    await asyncio.wait_for(m.join(), 1)
    assert c.calls == [("u", "s"), ("u", "other")]
    assert m.stats()["pending"] == 0
    await m.close(); await m.close()
    assert m.submit("u", "s") == "stopped"
    with pytest.raises(RuntimeError):
        m.start()


@pytest.mark.asyncio
async def test_running_dedup_dirty_rerun_fair_and_capacity_reserved():
    entered, release = asyncio.Event(), asyncio.Event()
    c = StubCompactor()
    original = c.compact
    async def compact(*args, **kwargs):
        if not c.calls:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)
    c.compact = compact
    async with maintainer(c, workers=1, max_pending=2) as m:
        assert m.submit("u", "s") == "queued"
        await asyncio.wait_for(entered.wait(), 1)
        assert m.submit("u", "other") == "queued"
        assert m.submit("u", "overflow") == "full"
        for _ in range(50):
            assert m.submit("u", "s") == "coalesced"
        release.set()
        await asyncio.wait_for(m.join(), 1)
        assert c.calls == [("u", "s"), ("u", "other"), ("u", "s")]
        assert m.stats()["followups"] == 1 and m.stats()["pending"] == 0


@pytest.mark.asyncio
async def test_fixed_worker_concurrency_no_per_session_tasks():
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    class C:
        async def compact(self, *args, **kwargs):
            nonlocal active, peak
            active += 1; peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
                return SimpleNamespace(status="below_threshold")
            finally:
                active -= 1
    async with maintainer(C(), workers=2, max_pending=6) as m:
        for i in range(6):
            assert m.submit("u", str(i)) == "queued"
        await asyncio.wait_for(entered.wait(), 1)
        assert len(m._tasks) == 2 and m.stats()["pending"] == 6
        assert m.submit("u", "full") == "full"
        release.set()
        await asyncio.wait_for(m.join(), 1)
        assert peak == 2


@pytest.mark.parametrize("statuses,expected", [
    (["failed", "conflict", "compacted"], 3),
    ([RuntimeError("private 13800138000"), "below_threshold"], 2),
    (["failed"], 3), (["no_old_turns"], 1)])
@pytest.mark.asyncio
async def test_retry_bounded_exceptions_and_logs(statuses, expected, caplog):
    c = StubCompactor(statuses)
    async with maintainer(c, workers=1, max_retries=2) as m:
        m.submit("u", "s")
        await asyncio.wait_for(m.join(), 1)
        assert len(c.calls) == expected
        assert m.stats().get("retries", 0) == expected - 1
        assert "13800138000" not in caplog.text
        if statuses == ["failed"]:
            assert m.stats()["retry_exhausted"] == 1


@pytest.mark.asyncio
async def test_new_turn_during_llm_kept_and_dirty_recheck(store):
    await seed(store)
    before = await store.get_snapshot("u", "s")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def summarize(prompt):
        nonlocal calls
        calls += 1
        entered.set(); await release.wait()
        return "本次摘要"
    c = MemoryCompactor(store, summarize, budget=TokenBudget(1))
    async with maintainer(c, workers=1) as m:
        m.submit("u", "s")
        await asyncio.wait_for(entered.wait(), 1)
        new = await asyncio.wait_for(store.commit_turn("u", "s", request_id="new",
            user_message="新问题", assistant_message="新回答"), 1)
        assert m.submit("u", "s") == "coalesced"
        release.set()
        await asyncio.wait_for(m.join(), 2)
        # 第一次保留最后两轮 + new；dirty 检查后又压缩其中一轮，最终仍保留 new。
        after = await store.get_snapshot("u", "s")
        assert new in after.turns and after.turns == (before.turns[-1], new)
        assert m.stats()["followups"] == 1
        assert m.stats()["compacted"] == 2


@pytest.mark.asyncio
async def test_two_maintainers_watch_multi_prevents_duplicate_commit(store):
    await seed(store)
    both = asyncio.Event(); calls = 0
    async def summarize(prompt):
        nonlocal calls
        calls += 1
        if calls >= 2:
            both.set()
        await both.wait()
        return "摘要"
    a = maintainer(MemoryCompactor(store, summarize, budget=TokenBudget(1)), workers=1)
    b = maintainer(MemoryCompactor(store, summarize, budget=TokenBudget(1)), workers=1)
    async with a, b:
        a.submit("u", "s"); b.submit("u", "s")
        await asyncio.wait_for(asyncio.gather(a.join(), b.join()), 2)
        assert a.stats().get("compacted", 0) + b.stats().get("compacted", 0) == 1
        assert a.stats().get("conflict", 0) + b.stats().get("conflict", 0) == 1
        assert len(await store.get_turns("u", "s")) == 2
        assert len(await store.get_pending_episodes()) == 1


@pytest.mark.asyncio
async def test_full_skip_does_not_change_chat_data(store):
    await seed(store)
    before = await store.get_snapshot("u", "s")
    async with maintainer(StubCompactor(), workers=1, max_pending=1) as m:
        m.submit("u", "other")
        assert m.submit("u", "s") == "full"
        assert await store.get_snapshot("u", "s") == before
        await m.join()
        assert m.submit("u", "s") == "queued"
        await m.join()


@pytest.mark.asyncio
async def test_periodic_outbox_without_chat_survives_failure_timeout():
    done = asyncio.Event()
    class Longterm:
        calls = 0
        async def flush_pending(self, store, limit):
            assert limit == 2
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("private 13800138000")
            if self.calls == 2:
                await asyncio.Event().wait()
            done.set()
            return 1
    l = Longterm(); c = StubCompactor()
    m = maintainer(c, store=object(), longterm=l, pending_batch_size=2, pending_timeout_s=.01)
    async with m:
        await asyncio.wait_for(done.wait(), 1)
        assert l.calls >= 3 and not c.calls
        assert m.stats()["pending_errors"] == 2
    assert not m._tasks


@pytest.mark.asyncio
async def test_outbox_pipeline_retry_does_not_regenerate_summary(store):
    import json
    from memory.profile import ProfileUpdater
    class Episodes:
        metadata = {"hnsw:space": "cosine", "embedding_id": "fake"}
        fail = True
        def __init__(self): self.rows = {}
        def upsert(self, ids, documents, **kwargs):
            if self.fail: raise RuntimeError("offline")
            self.rows.update(zip(ids, documents))
    class Profiles:
        metadata = {"profile_schema": "v2", "hnsw:space": "cosine", "embedding_id": "fake"}
        def __init__(self): self.rows = {}
        def get(self, ids, **kwargs): return {"documents": [self.rows[i] for i in ids if i in self.rows]}
        def upsert(self, ids, documents, **kwargs): self.rows.update(zip(ids, documents))
    ec, pc = Episodes(), Profiles()
    extracted = asyncio.Event()
    async def extract(prompt):
        extracted.set()
        return '{"preferences":["宽松"]}'
    profiles = ProfileUpdater(store, pc, extract, embed=lambda texts: [[1., 0.] for _ in texts], embedding_id="fake")
    longterm = LongTermMemory(ec, lambda texts: [[1., 0.] for _ in texts], "fake", profile_updater=profiles)
    e = Episode.from_turns("u", "s", tuple(Turn(str(i), "问", "答") for i in range(2)), "用户偏好宽松")
    await store._client.hset(PENDING_EPISODES_KEY, e.episode_id, json.dumps(e.to_dict()))
    c = StubCompactor()
    async with maintainer(c, store=store, longterm=longterm) as m:
        # 先保证启动扫描遇到了故障，随后恢复；无需任何新聊天或摘要调用。
        while not m.stats().get("pending_scans"):
            await asyncio.sleep(.001)
        assert await store.get_pending_episodes()
        ec.fail = False
        await asyncio.wait_for(extracted.wait(), 1)
        for _ in range(100):
            if not await store.get_pending_profiles(): break
            await asyncio.sleep(.005)
        assert not await store.get_pending_episodes() and not await store.get_pending_profiles()
        assert len(ec.rows) == len(pc.rows) == 1 and not c.calls


@pytest.mark.asyncio
async def test_close_cancel_and_drain_timeout_preserve_history(store):
    await seed(store)
    before = await store.get_snapshot("u", "s")
    entered = asyncio.Event()
    async def slow(prompt):
        entered.set(); await asyncio.Event().wait()
    c = MemoryCompactor(store, slow, budget=TokenBudget(1))
    m = maintainer(c, workers=1); m.start(); m.submit("u", "s")
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(m.close(drain=True, timeout_s=.01), 1)
    await asyncio.wait_for(m.join(), 1)
    assert m.stats()["drain_timeouts"] == 1 and m.stats()["pending"] == 0
    assert await store.get_snapshot("u", "s") == before


@pytest.mark.asyncio
async def test_graceful_drain_runs_dirty_followup_and_keeps_dependencies_open():
    entered, release = asyncio.Event(), asyncio.Event()
    c = StubCompactor()
    old = c.compact
    async def block(*args, **kwargs):
        entered.set(); await release.wait()
        return await old(*args, **kwargs)
    c.compact = block
    m = maintainer(c, workers=1); m.start(); m.submit("u", "s")
    await asyncio.wait_for(entered.wait(), 1)
    m.submit("u", "s")
    close = asyncio.create_task(m.close(drain=True))
    await asyncio.sleep(0)
    assert m.submit("u", "another") == "stopped"
    release.set(); await asyncio.wait_for(close, 1)
    assert len(c.calls) == 2 and not m._jobs


@pytest.mark.parametrize("kwargs", [{"max_pending": 0}, {"workers": True}, {"max_retries": -1},
    {"retry_delay_s": 0}, {"pending_timeout_s": float("nan")}, {"pending_interval_s": -1},
    {"longterm": object()}, {"pending_batch_size": 0}])
def test_bad_config(kwargs):
    with pytest.raises(ValueError):
        MemoryMaintainer(StubCompactor(), **kwargs)


def test_from_env_configuration(monkeypatch):
    monkeypatch.setenv("COMMERCEMIND_MEMORY_MAINTAIN_MAX_PENDING", "7")
    monkeypatch.setenv("COMMERCEMIND_MEMORY_MAINTAIN_WORKERS", "3")
    monkeypatch.setenv("COMMERCEMIND_MEMORY_MAINTAIN_MAX_RETRIES", "0")
    monkeypatch.setenv("COMMERCEMIND_MEMORY_MAINTAIN_RETRY_DELAY_S", ".1")
    monkeypatch.setenv("COMMERCEMIND_MEMORY_MAINTAIN_PENDING_INTERVAL_S", "9")
    m = MemoryMaintainer.from_env(StubCompactor())
    assert (m.max_pending, m.workers, m.max_retries, m.retry_delay_s, m.pending_interval_s) == (7, 3, 0, .1, 9)


@pytest.mark.asyncio
async def test_retry_zero_means_one_attempt_and_can_resubmit():
    c = StubCompactor(["failed"])
    async with maintainer(c, max_retries=0) as m:
        m.submit("u", "s"); await m.join()
        assert len(c.calls) == 1 and not m._jobs
        m.submit("u", "s"); await m.join()
        assert len(c.calls) == 2


@pytest.mark.asyncio
async def test_close_cancels_pending_loop_and_can_close_before_start():
    entered, cancelled = asyncio.Event(), asyncio.Event()
    class Longterm:
        async def flush_pending(self, *args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    m = maintainer(StubCompactor(), store=object(), longterm=Longterm())
    m.start()
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(m.close(), 1)
    assert cancelled.is_set() and not m._tasks
    unstarted = maintainer(StubCompactor())
    await unstarted.close()
    with pytest.raises(RuntimeError):
        unstarted.start()
