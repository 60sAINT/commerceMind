"""ChromaDB 知识库 collection 的连接与生命周期。"""
from __future__ import annotations

import logging
from typing import Any

from core.config import KnowledgeBaseSettings

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """持有独立的知识库 collection；构造时不做本地库降级。"""

    def __init__(self, settings: KnowledgeBaseSettings, *, client: Any | None = None) -> None:
        if not settings.enabled:
            raise RuntimeError("知识库未启用，请设置 COMMERCEMIND_KNOWLEDGE_ENABLED=true")
        self.settings = settings
        self._client = client
        try:
            if self._client is None:
                import chromadb
                from chromadb.config import Settings as ChromaSettings

                self._client = chromadb.HttpClient(
                    host=settings.chroma_host,
                    port=settings.chroma_port,
                    settings=ChromaSettings(anonymized_telemetry=False),
                )
            self._client.heartbeat()
            self._collection = self._client.get_or_create_collection(
                name=settings.collection_name,
                metadata={"description": "CommerceMind RAG knowledge base"},
            )
        except Exception as exc:
            close = getattr(self._client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("关闭初始化失败的 Chroma 客户端时发生错误", exc_info=True)
            self._client = None
            raise RuntimeError(
                f"无法连接 ChromaDB 知识库 {settings.chroma_host}:{settings.chroma_port} "
                f"或初始化 collection {settings.collection_name}"
            ) from exc
        logger.info("知识库 collection 已就绪: %s", settings.collection_name)

    @classmethod
    def from_settings(cls, settings: KnowledgeBaseSettings) -> "KnowledgeBase":
        """按应用配置创建连接；错误直接上抛，不切换到本地空库。"""
        return cls(settings)

    @property
    def collection(self) -> Any:
        """暴露 collection 给后续导入与检索实现。"""
        return self._collection

    def close(self) -> None:
        """关闭支持显式 close 的客户端；Chroma HTTP 客户端通常由 SDK 管理。"""
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

