from types import SimpleNamespace

import pytest

from core.config import MemorySettings
from memory import service


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
        await service.ConversationMemory.start(MemorySettings(enabled=True), SimpleNamespace())

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
