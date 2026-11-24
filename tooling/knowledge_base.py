"""ChromaDB 知识库 collection 的连接与生命周期。"""
from __future__ import annotations

import logging
import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any

from core.config import KnowledgeBaseSettings

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """持有独立的知识库 collection；构造时不做本地库降级。"""

    def __init__(self, settings: KnowledgeBaseSettings, *, client: Any | None = None) -> None:
        if not settings.enabled:
            raise RuntimeError("知识库未启用，请设置 COMMERCEMIND_KNOWLEDGE_ENABLED=true")
        self.settings = settings
        self._write_lock = threading.RLock()
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

    def add_documents(self, documents: list[dict[str, str]]) -> int:
        """导入或替换文档，返回本次写入的片段数。相同身份可安全重复导入。"""
        if not isinstance(documents, list):
            raise ValueError("documents 必须是列表")
        prepared: dict[str, tuple[str, str]] = {}
        for index, document in enumerate(documents):
            if not isinstance(document, dict):
                raise ValueError(f"documents[{index}] 必须是对象")
            title, content = document.get("title"), document.get("content")
            document_id = document.get("document_id")
            if not isinstance(title, str) or not title.strip():
                raise ValueError(f"documents[{index}].title 不能为空")
            if not isinstance(content, str) or not content.strip():
                raise ValueError(f"documents[{index}].content 不能为空")
            if document_id is not None and (not isinstance(document_id, str) or not document_id.strip()):
                raise ValueError(f"documents[{index}].document_id 必须是非空字符串")
            title = title.strip()
            content = content.strip()
            identity = ["id", document_id.strip()] if document_id else ["title", title]
            key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            prepared[key] = (title, content)

        written = 0
        with self._write_lock:
            for document_id, (title, content) in prepared.items():
                version = hashlib.sha256(content.encode("utf-8")).hexdigest()
                chunks = self._chunk_text(content)
                ids = [f"{document_id}:{index}" for index in range(len(chunks))]
                existing = self._collection.get(
                    where={"document_id": document_id}, include=["metadatas"],
                )
                old_ids = set(existing.get("ids", []))
                imported_at = datetime.now(timezone.utc).isoformat()
                for offset in range(0, len(chunks), self.settings.write_batch_size):
                    batch_chunks = chunks[offset:offset + self.settings.write_batch_size]
                    batch_ids = ids[offset:offset + self.settings.write_batch_size]
                    self._collection.upsert(
                        ids=batch_ids,
                        documents=batch_chunks,
                        metadatas=[{
                            "title": title,
                            "document_id": document_id,
                            "version": version,
                            "chunk_index": offset + index,
                            "total_chunks": len(chunks),
                            "imported_at": imported_at,
                        } for index in range(len(batch_chunks))],
                    )
                stale_ids = old_ids - set(ids)
                if stale_ids:
                    self._collection.delete(ids=list(stale_ids))
                written += len(chunks)
        return written

    async def add_documents_async(self, documents: list[dict[str, str]]) -> int:
        """异步 API 适配器；Chroma 客户端为同步实现。"""
        import asyncio

        return await asyncio.to_thread(self.add_documents, documents)

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """按语义相似度检索片段，score 越高越相关。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError("top_k 必须是 1 到 20 的整数")
        count = self._collection.count()
        if count == 0:
            return []
        result = self._collection.query(
            query_texts=[query.strip()],
            n_results=min(top_k, count),
            include=["documents", "metadatas", "distances"],
        )
        documents = (result.get("documents") or [[]])[0] or []
        metadatas = (result.get("metadatas") or [[]])[0] or []
        distances = (result.get("distances") or [[]])[0] or []
        return [
            {
                "title": (metadata or {}).get("title", ""),
                "content": content,
                "score": round(1.0 - float(distance), 4),
                "document_id": (metadata or {}).get("document_id", ""),
                "version": (metadata or {}).get("version", ""),
                "chunk_index": (metadata or {}).get("chunk_index", 0),
                "total_chunks": (metadata or {}).get("total_chunks", 1),
            }
            for content, metadata, distance in zip(documents, metadatas, distances)
        ]

    async def search_async(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """在线程池中调用同步 Chroma 查询。"""
        import asyncio

        return await asyncio.to_thread(self.search, query, top_k)

    def list_documents(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """按文档聚合并分页列出知识库文档。"""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit 必须是 1 到 200 的整数")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset 必须是非负整数")
        total_chunks = self._collection.count()
        if not total_chunks:
            return []
        result = self._collection.get(
            include=["documents", "metadatas"], limit=total_chunks,
        )
        grouped = self._group_chunks(result)
        ordered = sorted(grouped.values(), key=lambda item: (item["title"].casefold(), item["document_id"]))
        return ordered[offset:offset + limit]

    async def list_documents_async(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        import asyncio

        return await asyncio.to_thread(self.list_documents, limit=limit, offset=offset)

    def get_document(self, document_id: str) -> dict[str, Any] | None:
        """读取一个文档的元数据与按序片段；未知 ID 返回 None。"""
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("document_id 不能为空")
        result = self._collection.get(
            where={"document_id": document_id.strip()}, include=["documents", "metadatas"],
        )
        grouped = self._group_chunks(result)
        return grouped.get(document_id.strip())

    async def get_document_async(self, document_id: str) -> dict[str, Any] | None:
        import asyncio

        return await asyncio.to_thread(self.get_document, document_id)

    def delete_document(self, document_id: str) -> bool:
        """删除指定文档的全部片段，返回是否找到并删除文档。"""
        if not isinstance(document_id, str) or not document_id.strip():
            raise ValueError("document_id 不能为空")
        document_id = document_id.strip()
        with self._write_lock:
            result = self._collection.get(
                where={"document_id": document_id}, include=["metadatas"],
            )
            ids = result.get("ids", [])
            if not ids:
                return False
            self._collection.delete(ids=ids)
            return True

    async def delete_document_async(self, document_id: str) -> bool:
        import asyncio

        return await asyncio.to_thread(self.delete_document, document_id)

    def stats(self) -> dict[str, int]:
        """返回文档数和片段数，二者分别统计。"""
        total_chunks = self._collection.count()
        if not total_chunks:
            return {"document_count": 0, "chunk_count": 0}
        result = self._collection.get(
            include=["metadatas"], limit=total_chunks,
        )
        document_ids = {
            metadata.get("document_id")
            for metadata in result.get("metadatas", [])
            if metadata and metadata.get("document_id")
        }
        return {"document_count": len(document_ids), "chunk_count": total_chunks}

    async def stats_async(self) -> dict[str, int]:
        import asyncio

        return await asyncio.to_thread(self.stats)

    @staticmethod
    def _group_chunks(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        for content, metadata in zip(documents, metadatas):
            metadata = metadata or {}
            document_id = metadata.get("document_id")
            if not document_id:
                continue
            entry = grouped.setdefault(document_id, {
                "document_id": document_id,
                "title": metadata.get("title", ""),
                "version": metadata.get("version", ""),
                "imported_at": metadata.get("imported_at", ""),
                "chunk_count": 0,
                "chunks": [],
            })
            entry["chunks"].append({
                "content": content,
                "chunk_index": metadata.get("chunk_index", 0),
                "total_chunks": metadata.get("total_chunks", 1),
            })
            entry["chunk_count"] += 1
        for entry in grouped.values():
            entry["chunks"].sort(key=lambda chunk: chunk["chunk_index"])
        return grouped

    def _chunk_text(self, text: str) -> list[str]:
        """按段落和句子尽量保持语义边界，超长句硬切并支持字符重叠。"""
        size = self.settings.chunk_size
        overlap = self.settings.chunk_overlap
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        paragraphs = [re.sub(r"[ \t]+", " ", part).strip() for part in normalized.split("\n")]
        sentences: list[str] = []
        for paragraph in paragraphs:
            if not paragraph:
                continue
            parts = re.split(r"(?<=[。！？.!?；;])", paragraph)
            for part in parts:
                part = part.strip()
                while len(part) > size:
                    sentences.append(part[:size])
                    part = part[max(0, size - overlap):]
                if part:
                    sentences.append(part)

        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            remaining = sentence
            while remaining:
                separator = " " if current else ""
                available = size - len(current) - len(separator)
                if available <= 0:
                    chunks.append(current)
                    current = current[-overlap:] if overlap else ""
                    continue
                if len(remaining) <= available:
                    current += separator + remaining
                    break
                if current and len(current) > overlap:
                    chunks.append(current)
                    current = current[-overlap:] if overlap else ""
                    continue
                current += separator + remaining[:available]
                remaining = remaining[available:]
                chunks.append(current)
                current = current[-overlap:] if overlap else ""
        if current:
            chunks.append(current)
        return chunks

