"""脱敏的情景记忆；固定 embedding collection，cosine 分数阈值检索。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Callable

from memory.embeddings import SiliconFlowEmbedding, embedding_from_env, encode_vectors
from memory.models import Episode, memory_identity
from memory.redaction import redact
from memory.tokens import TokenCounter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EpisodeMatch:
    episode_id: str
    text: str
    score: float
    metadata: dict[str, Any]


class LongTermMemory:
    def __init__(self, collection: Any, embed: Callable, embedding_id: str, *,
                 counter: TokenCounter | None = None, top_k: int = 3,
                 min_score: float = 0.55, max_tokens: int = 600, min_turns: int = 2, profile_updater=None) -> None:
        for name, value in (("top_k", top_k), ("max_tokens", max_tokens), ("min_turns", min_turns)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须为正整数")
        if not math.isfinite(min_score) or not 0 <= min_score <= 1:
            raise ValueError("min_score 必须在 0 到 1 之间")
        metadata = collection.metadata or {}
        if metadata.get("hnsw:space") != "cosine" or metadata.get("embedding_id") != embedding_id:
            raise ValueError("collection 的距离或 embedding 配置不匹配")
        self.profile_updater = profile_updater
        self.collection, self._embed, self.embedding_id = collection, embed, embedding_id
        self.counter = counter if counter is not None else TokenCounter()
        self.top_k, self.min_score, self.max_tokens, self.min_turns = top_k, min_score, max_tokens, min_turns

    @classmethod
    def from_env(cls, *, profile_updater=None) -> LongTermMemory:
        import chromadb
        from chromadb.config import Settings
        embedding_id, embed = embedding_from_env()
        # 不在失败时切换后端；不同模型必须使用不同 collection。
        name = "episodic_" + hashlib.sha256(embedding_id.encode()).hexdigest()[:12]
        client = chromadb.HttpClient(host=os.getenv("CHROMA_HOST", "localhost"),
                                    port=int(os.getenv("CHROMA_PORT", "8001")),
                                    settings=Settings(anonymized_telemetry=False))
        from chromadb.errors import InvalidCollectionException
        try:
            collection = client.get_collection(name, embedding_function=None)
        except InvalidCollectionException:
            collection = client.get_or_create_collection(name, embedding_function=None,
                        metadata={"hnsw:space": "cosine", "embedding_id": embedding_id})
        return cls(collection, embed, embedding_id, profile_updater=profile_updater,
                   top_k=int(os.getenv("COMMERCEMIND_MEMORY_RETRIEVAL_TOP_K", "3")),
                   min_score=float(os.getenv("COMMERCEMIND_MEMORY_RETRIEVAL_MIN_SCORE", "0.55")),
                   max_tokens=int(os.getenv("COMMERCEMIND_MEMORY_EPISODE_MAX_TOKENS", "600")),
                   min_turns=int(os.getenv("COMMERCEMIND_MEMORY_EPISODE_MIN_TURNS", "2")))

    def _vectors(self, texts: list[str]) -> list[list[float]]:
        return encode_vectors(self._embed, texts)

    async def add_episode(self, episode: Episode) -> bool:
        if episode.turn_count < self.min_turns:
            return False
        data = episode.to_dict()  # 再次脱敏；embedding 永远不接触原始摘要。
        text = data.pop("summary")
        data.pop("episode_id")
        data["content_hash"] = memory_identity(text)
        data["embedding_id"] = self.embedding_id
        def write():
            self.collection.upsert(ids=[episode.episode_id], documents=[text], metadatas=[data],
                                   embeddings=self._vectors([text]))
        await asyncio.to_thread(write)
        return True

    async def search(self, user_id: str, session_id: str, query: str) -> list[EpisodeMatch]:
        if not query.strip():
            return []
        try:
            user, session = memory_identity(user_id), memory_identity(session_id)
            def read():
                return self.collection.query(query_embeddings=self._vectors([redact(query)]),
                    n_results=max(self.top_k * 4, 12),
                    where={"$and": [{"user_key": {"$eq": user}}, {"session_key": {"$ne": session}}]},
                    include=["documents", "metadatas", "distances"])
            raw = await asyncio.to_thread(read)
            hits, seen, seen_texts, texts = [], set(), set(), []
            for eid, document, metadata, distance in sorted(zip(
                    raw["ids"][0], raw["documents"][0], raw["metadatas"][0], raw["distances"][0]),
                    key=lambda item: item[3]):
                # 防御性二次过滤，不能仅信任向量库或测试替身返回的过滤结果。
                if metadata.get("user_key") != user or metadata.get("session_key") == session:
                    continue
                if not math.isfinite(distance):
                    continue
                score = max(-1.0, min(1.0, 1.0 - distance))  # cosine similarity = 1 - cosine distance
                text = redact(document).strip()
                if score < self.min_score or not text or eid in seen or text in seen_texts:
                    continue
                fitted = self.counter.truncate_text(text, self.max_tokens,
                                                   prefix="\n\n".join(texts) + ("\n\n" if texts else ""))
                if not fitted:
                    continue
                texts.append(fitted)
                seen.add(eid)
                seen_texts.add(text)
                hits.append(EpisodeMatch(eid, fitted, score, metadata))
                if len(hits) >= self.top_k:
                    break
            return hits
        except Exception as exc:
            logger.warning("episode_search_failed error_type=%s", type(exc).__name__)
            return []

    async def flush_pending(self, store: Any, limit: int = 20) -> int:
        """有限批次重试；upsert 成功后才移除，崩溃后重试仍幂等。"""
        sent = 0
        for episode in await store.get_pending_episodes(limit):
            try:
                if not await self.add_episode(episode):
                    continue
                await store.ack_episode(episode.episode_id, profile_episode=episode)
                sent += 1
            except Exception as exc:
                logger.warning("episode_write_pending error_type=%s", type(exc).__name__)
                break
        if self.profile_updater is not None:
            try:
                await self.profile_updater.flush_pending(limit)
            except Exception as exc:
                logger.warning("profile_flush_failed error_type=%s", type(exc).__name__)
        return sent
