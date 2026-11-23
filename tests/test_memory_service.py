"""记忆服务：会话锁边界，以及长期记忆/画像的启动接线。"""
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from core.config import MemorySettings
from memory import service
from memory.service import ConversationMemory
from memory.store import RedisMemoryStore


@pytest_asyncio.fixture
async def store():
    s = RedisMemoryStore(FakeRedis(decode_responses=True))
    yield s
    await s.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("lock_held", [False, True])
@pytest.mark.parametrize("submit_fails", [False, True])
async def test_lock_covers_save_and_maintenance_submission(store, monkeypatch, lock_held, submit_fails):
    events = []
    held = False
    original_conversation = store.conversation
    original_commit = store.commit_turn

    @asynccontextmanager
    async def conversation(user, session):
        nonlocal held
        async with original_conversation(user, session):
            held = True
            events.append("locked")
            try:
                yield
            finally:
                held = False
                events.append("unlocked")

    async def commit_turn(*args, **kwargs):
        assert held and kwargs["lock_held"]
        turn = await original_commit(*args, **kwargs)
        assert (await store.get_turns("u", "s")) == [turn]
        assert (await store.get_state("u", "s")) == {"intent": "test"}
        assert (await store.get_receipt("u", "s", "client")) == turn
        events.append("saved")
        return turn

    def submit(user, session):
        # submit 的异常会被服务捕获，故在外部断言此时确实持锁。
        events.append(("submitted", held, user, session))
        if submit_fails:
            raise RuntimeError("maintenance unavailable")

    monkeypatch.setattr(store, "conversation", conversation)
    monkeypatch.setattr(store, "commit_turn", commit_turn)
    memory = ConversationMemory(store, maintainer=SimpleNamespace(submit=submit))
    async with memory.conversation("u", "s") if lock_held else nullcontext():
        turn = await memory.commit(
            "u", "s", request_id="r", question="问", answer="答",
            client_request_id="client", state={"intent": "test"}, lock_held=lock_held,
        )
    assert turn is not None
    assert events == ["locked", "saved", ("submitted", True, "u", "s"), "unlocked"]
    async with memory.conversation("u", "s"):
        pass


@pytest.mark.asyncio
async def test_failed_turn_does_not_lock_or_submit(store, monkeypatch):
    def unexpected(*args):
        pytest.fail("失败轮次不应加锁或提交维护")

    monkeypatch.setattr(store, "conversation", unexpected)
    memory = ConversationMemory(store, maintainer=SimpleNamespace(submit=unexpected))
    assert await memory.commit(
        "u", "s", request_id="r", question="问", answer="答", succeeded=False,
    ) is None
    assert await store.get_turns("u", "s") == []


@pytest.mark.asyncio
async def test_save_failure_releases_lock_without_submitting(store, monkeypatch):
    def unexpected(*args):
        pytest.fail("保存失败不应提交维护")

    memory = ConversationMemory(store, maintainer=SimpleNamespace(submit=unexpected))
    with pytest.raises(TypeError):
        await memory.commit("u", "s", request_id="r", question="问", answer="答", state=[])
    assert await store.get_turns("u", "s") == []
    async with memory.conversation("u", "s"):
        pass


class FakeStore:
    def __init__(self):
        self.closed = False

    async def ping(self):
        return True

    async def close(self):
        self.closed = True


class FakeCompactor:
    def __init__(self, store, summarize, *, longterm):
        self.store = store
        self.summarize = summarize
        self.longterm = longterm
        self.closed = False

    async def close(self):
        self.closed = True


class FakeMaintainer:
    def __init__(self, compactor, *, store, longterm):
        self.compactor = compactor
        self.store = store
        self.longterm = longterm
        self.workers = 2
        self.started = False

    def start(self):
        self.started = True


@pytest.mark.asyncio
async def test_start_wires_episode_and_profile_outbox_to_maintainer(monkeypatch):
    store = FakeStore()
    calls = {}

    def profile_from_env(actual_store, extract):
        calls["profile"] = (actual_store, extract)
        return "profile-updater"

    def longterm_from_env(*, profile_updater):
        calls["longterm_profile"] = profile_updater
        return "longterm-memory"

    monkeypatch.setattr(service.RedisMemoryStore, "from_url", lambda *args, **kwargs: store)
    monkeypatch.setattr(service.ProfileUpdater, "from_env", staticmethod(profile_from_env))
    monkeypatch.setattr(service.LongTermMemory, "from_env", staticmethod(longterm_from_env))
    monkeypatch.setattr(service, "MemoryCompactor", FakeCompactor)
    monkeypatch.setattr(
        service.MemoryMaintainer,
        "from_env",
        staticmethod(lambda compactor, **kwargs: FakeMaintainer(compactor, **kwargs)),
    )

    llm = SimpleNamespace(summarize=object(), extract_profile=object())
    memory = await service.ConversationMemory.start(MemorySettings(enabled=True), llm)

    assert calls["profile"] == (store, llm.extract_profile)
    assert calls["longterm_profile"] == "profile-updater"
    assert memory.compactor.longterm == "longterm-memory"
    assert memory.maintainer.longterm == "longterm-memory"
    assert memory.maintainer.store is store
    assert memory.maintainer.started
    assert not store.closed


@pytest.mark.asyncio
async def test_start_cleans_up_if_longterm_setup_fails(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(service.RedisMemoryStore, "from_url", lambda *args, **kwargs: store)
    monkeypatch.setattr(service.ProfileUpdater, "from_env", staticmethod(lambda *args, **kwargs: object()))

    def fail_setup(**kwargs):
        raise RuntimeError("Chroma setup failed")

    monkeypatch.setattr(service.LongTermMemory, "from_env", staticmethod(fail_setup))

    with pytest.raises(RuntimeError, match="Chroma setup failed"):
        await service.ConversationMemory.start(
            MemorySettings(enabled=True), SimpleNamespace(extract_profile=object()),
        )

    assert store.closed


@pytest.mark.asyncio
async def test_start_closes_created_compactor_if_maintainer_start_fails(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(service.RedisMemoryStore, "from_url", lambda *args, **kwargs: store)
    monkeypatch.setattr(service.ProfileUpdater, "from_env", staticmethod(lambda *args, **kwargs: object()))
    monkeypatch.setattr(service.LongTermMemory, "from_env", staticmethod(lambda **kwargs: object()))
    monkeypatch.setattr(service, "MemoryCompactor", FakeCompactor)

    class FailingMaintainer(FakeMaintainer):
        def start(self):
            raise RuntimeError("maintainer start failed")

    monkeypatch.setattr(
        service.MemoryMaintainer,
        "from_env",
        staticmethod(lambda compactor, **kwargs: FailingMaintainer(compactor, **kwargs)),
    )

    with pytest.raises(RuntimeError, match="maintainer start failed"):
        await service.ConversationMemory.start(
            MemorySettings(enabled=True),
            SimpleNamespace(summarize=object(), extract_profile=object()),
        )

    assert store.closed
