"""应用层记忆门面：组合 Redis store、上下文、压缩和后台维护。

`/chat` 只依赖本模块，不直接拼装 store/compactor/maintainer；
测试可绕过 `start()` 直接注入 fake store。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from core.config import MemorySettings
from memory.compactor import MemoryCompactor
from memory.context import ContextBuilder, MemoryContext
from memory.maintainer import MemoryMaintainer
from memory.models import Turn
from memory.store import RedisMemoryStore

logger = logging.getLogger(__name__)

_HEALTH_TIMEOUT_S = 1.5
_STARTUP_TIMEOUT_S = 3.0


@dataclass(frozen=True, slots=True)
class DebugSnapshot:
    """只读会话调试快照；不触发压缩或后台维护。"""

    session_id: str
    user_id: str
    turns: tuple[Turn, ...]
    state: dict[str, Any]
    summary: str
    working_ttl_s: int
    state_ttl_s: int
    summary_ttl_s: int


@dataclass(frozen=True, slots=True)
class MemoryHealth:
    """供 /health 展示；状态值为 ok / error / disabled。"""

    enabled: bool
    redis: str
    chroma: str
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.redis != "error" and self.chroma != "error"


async def _chroma_heartbeat(host: str, port: int) -> None:
    def check() -> None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        client = chromadb.HttpClient(
            host=host,
            port=port,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        client.heartbeat()

    await asyncio.wait_for(asyncio.to_thread(check), _HEALTH_TIMEOUT_S)


class ConversationMemory:
    """一次应用生命周期内共享的记忆服务。"""

    def __init__(
        self,
        store: RedisMemoryStore,
        *,
        settings: MemorySettings | None = None,
        compactor: MemoryCompactor | None = None,
        maintainer: MemoryMaintainer | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.compactor = compactor
        self.maintainer = maintainer
        self._context = ContextBuilder(store)
        self._closed = False

    @classmethod
    async def start(cls, settings: MemorySettings, llm: Any) -> ConversationMemory:
        """连接 Redis 并启动后台维护；失败由调用方决定是否降级。"""
        store = RedisMemoryStore.from_url(settings.redis_url, ttl_s=settings.ttl_s)
        try:
            await asyncio.wait_for(store.ping(), _STARTUP_TIMEOUT_S)
        except Exception:
            await store.close()
            raise

        compactor = MemoryCompactor(store, llm.summarize)
        maintainer = MemoryMaintainer.from_env(compactor, store=store)
        try:
            maintainer.start()
        except Exception:
            await compactor.close()
            await store.close()
            raise
        logger.info(
            "记忆服务已启动: ttl_s=%d maintainer_workers=%d",
            settings.ttl_s,
            maintainer.workers,
        )
        return cls(store, settings=settings, compactor=compactor, maintainer=maintainer)

    def conversation(self, user_id: str, session_id: str):
        """整轮会话锁；持有期间调用 `commit(..., lock_held=True)`。"""
        return self.store.conversation(user_id, session_id)

    async def get_receipt(
        self, user_id: str, session_id: str, client_request_id: str | None,
    ) -> Turn | None:
        if not client_request_id:
            return None
        return await self.store.get_receipt(user_id, session_id, client_request_id)

    async def resolve_session(
        self,
        user_id: str,
        requested_session_id: str | None,
        client_request_id: str | None,
    ) -> tuple[str, Turn | None]:
        """优先按 client_request_id 回到原会话，避免重试因缺 session_id 另起一轮。"""
        if client_request_id:
            found = await self.store.get_user_receipt(user_id, client_request_id)
            if found is not None:
                return found
        return requested_session_id or uuid4().hex[:16], None

    async def read_context(self, user_id: str, session_id: str) -> MemoryContext:
        return await self._context.get_context(user_id, session_id)

    async def debug_snapshot(self, user_id: str, session_id: str) -> DebugSnapshot:
        snapshot = await self.store.get_snapshot(user_id, session_id)
        working_ttl, state_ttl, summary_ttl = await self.store.get_ttls(
            user_id, session_id,
        )
        return DebugSnapshot(
            session_id=session_id,
            user_id=user_id,
            turns=snapshot.turns,
            state=dict(snapshot.state),
            summary=snapshot.summary,
            working_ttl_s=working_ttl,
            state_ttl_s=state_ttl,
            summary_ttl_s=summary_ttl,
        )

    async def commit(
        self,
        user_id: str,
        session_id: str,
        *,
        request_id: str,
        question: str,
        answer: str,
        succeeded: bool = True,
        client_request_id: str | None = None,
        lock_held: bool = False,
        state: dict[str, Any] | None = None,
    ) -> Turn | None:
        """提交成功后触发后台压缩；不等待摘要，不阻塞聊天。"""
        turn = await self.store.commit_turn(
            user_id,
            session_id,
            request_id=request_id,
            user_message=question,
            assistant_message=answer,
            succeeded=succeeded,
            client_request_id=client_request_id,
            lock_held=lock_held,
            state=state,
        )
        if turn is not None and self.maintainer is not None:
            try:
                self.maintainer.submit(user_id, session_id)
            except Exception:  # 维护失败不影响已保存的问答
                logger.warning("提交记忆维护任务失败", exc_info=True)
        return turn

    async def health(self) -> MemoryHealth:
        detail: list[str] = []

        redis = "ok"
        try:
            await asyncio.wait_for(self.store.ping(), _HEALTH_TIMEOUT_S)
        except Exception as exc:
            redis = "error"
            detail.append(f"redis: {type(exc).__name__}")

        if self.settings is None:
            chroma = "disabled"
        else:
            try:
                await _chroma_heartbeat(self.settings.chroma_host, self.settings.chroma_port)
                chroma = "ok"
            except Exception as exc:
                chroma = "error"
                detail.append(f"chroma: {type(exc).__name__}")

        return MemoryHealth(
            enabled=True,
            redis=redis,
            chroma=chroma,
            detail="; ".join(detail) or None,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.maintainer is not None:
            await self.maintainer.close(drain=False, timeout_s=5)
        if self.compactor is not None:
            await self.compactor.close()
        await self.store.close()
