"""知识库连接基础与 Chroma 配置契约。"""
from __future__ import annotations

import pytest

from core.config import KnowledgeBaseSettings, Settings
from tooling.knowledge_base import KnowledgeBase


class FakeClient:
    def __init__(self, *, fail_heartbeat: bool = False) -> None:
        self.fail_heartbeat = fail_heartbeat
        self.collections: dict[str, dict] = {}
        self.heartbeats = 0
        self.closed = False

    def heartbeat(self):
        self.heartbeats += 1
        if self.fail_heartbeat:
            raise ConnectionError("unavailable")

    def get_or_create_collection(self, *, name, metadata):
        return self.collections.setdefault(name, {"name": name, "metadata": metadata})

    def close(self):
        self.closed = True


def test_knowledge_settings_default_disabled_and_validates_required():
    assert KnowledgeBaseSettings().enabled is False
    with pytest.raises(ValueError, match="required"):
        KnowledgeBaseSettings(required=True)


def test_knowledge_settings_rejects_invalid_collection_name():
    with pytest.raises(ValueError, match="collection"):
        KnowledgeBaseSettings(collection_name="../memory")


def test_settings_from_env_reads_knowledge_configuration(monkeypatch):
    monkeypatch.setenv("COMMERCEMIND_KNOWLEDGE_ENABLED", "true")
    monkeypatch.setenv("COMMERCEMIND_KNOWLEDGE_REQUIRED", "true")
    monkeypatch.setenv("COMMERCEMIND_KNOWLEDGE_COLLECTION", "commerce_docs")
    monkeypatch.setenv("CHROMA_HOST", "chroma-service")
    monkeypatch.setenv("CHROMA_PORT", "8123")

    settings = Settings.from_env().knowledge

    assert settings.enabled and settings.required
    assert settings.chroma_host == "chroma-service"
    assert settings.chroma_port == 8123
    assert settings.collection_name == "commerce_docs"


def test_knowledge_base_initializes_named_collection_and_closes_client():
    client = FakeClient()
    kb = KnowledgeBase(KnowledgeBaseSettings(enabled=True), client=client)

    assert client.heartbeats == 1
    assert kb.collection["name"] == "knowledge_base"
    assert "episodic" not in client.collections
    kb.close()
    assert client.closed


def test_knowledge_base_fails_when_disabled():
    with pytest.raises(RuntimeError, match="未启用"):
        KnowledgeBase(KnowledgeBaseSettings())


def test_knowledge_base_does_not_fallback_when_chroma_is_unavailable():
    client = FakeClient(fail_heartbeat=True)

    with pytest.raises(RuntimeError, match="无法连接 ChromaDB") as error:
        KnowledgeBase(KnowledgeBaseSettings(enabled=True), client=client)

    assert isinstance(error.value.__cause__, ConnectionError)

