"""按 user_id + session_id 存储的 Redis 短期记忆。

- 一轮一个 Redis list item，item 内包含 user 和 assistant 两条消息；
- Redis 中最新在前，读取时还原为时间顺序；
- commit 在 MULTI/EXEC 中同时写 working memory、state 和 summary，并统一刷新 TTL；
- 为 compactor 提供一致快照及条件提交；本模块不调用模型生成摘要。
"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import redis.asyncio as redis
from redis.exceptions import WatchError

from memory.locks import KeyedLocks, redis_lease
from memory.models import Episode, MemorySnapshot, Turn

DEFAULT_TTL_S = 7 * 24 * 60 * 60
PENDING_EPISODES_KEY = "memory:pending_episodes:v1"
PENDING_PROFILES_KEY = "memory:pending_profiles:v1"
APPLIED_PROFILES_KEY = "memory:applied_profiles:v1"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class RedisMemoryStore:
    """一个会话一条 key 空间；不同用户/会话之间完全隔离。"""

    def __init__(self, client: redis.Redis, *, ttl_s: int = DEFAULT_TTL_S) -> None:
        if type(ttl_s) is not int or ttl_s <= 0:
            raise ValueError("ttl_s 必须是正整数")
        self._client = client
        self._ttl_s = ttl_s
        self._process_locks = KeyedLocks()

    @classmethod
    def from_url(cls, url: str, *, ttl_s: int = DEFAULT_TTL_S) -> RedisMemoryStore:
        # 短超时避免记忆依赖不可用时拖住启动和健康检查。
        client = redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=5,
        )
        return cls(client, ttl_s=ttl_s)

    @staticmethod
    def session_key_id(user_id: str, session_id: str) -> str:
        """生成 user_id + session_id 的哈希键标识，不是新的业务 session_id。"""
        raw = json.dumps([str(user_id), str(session_id)], ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def working_key(self, user_id: str, session_id: str) -> str:
        return f"wm_v1:{self.session_key_id(user_id, session_id)}"

    def state_key(self, user_id: str, session_id: str) -> str:
        return f"state_v1:{self.session_key_id(user_id, session_id)}"

    def summary_key(self, user_id: str, session_id: str) -> str:
        return f"summary_v1:{self.session_key_id(user_id, session_id)}"

    def receipt_key(self, user_id: str, session_id: str) -> str:
        """幂等回执 Hash：client_request_id -> 已提交轮次。"""
        return f"receipt_v1:{self.session_key_id(user_id, session_id)}"

    @asynccontextmanager
    async def _session_lock(self, user_id: str, session_id: str) -> AsyncIterator[None]:
        digest = self.session_key_id(user_id, session_id)
        async with self._process_locks.hold(digest):
            # 保留已使用的 Redis 锁前缀，确保重命名后仍与旧实例互斥。
            async with redis_lease(self._client, f"lock:conversation:{digest}"):
                yield

    @asynccontextmanager
    async def conversation(self, user_id: str, session_id: str) -> AsyncIterator[None]:
        """整轮聊天的会话锁；持有期间调用 `commit_turn(lock_held=True)`。"""
        async with self._session_lock(user_id, session_id):
            yield

    @asynccontextmanager
    async def _maybe_session_lock(
        self, user_id: str, session_id: str, lock_held: bool,
    ) -> AsyncIterator[None]:
        if lock_held:
            yield
        else:
            async with self._session_lock(user_id, session_id):
                yield

    async def commit_turn(
        self,
        user_id: str,
        session_id: str,
        *,
        request_id: str,
        user_message: str,
        assistant_message: str,
        succeeded: bool = True,
        state: dict[str, Any] | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        client_request_id: str | None = None,
        lock_held: bool = False,
    ) -> Turn | None:
        """原子提交一轮问答；失败轮次直接返回 None，不写入任何 key。

        `lock_held=True` 表示调用方已持有 `conversation()`，避免锁重入自锁。
        `client_request_id` 会写入同会话回执 Hash，供重复请求回放。
        """
        if not succeeded:
            return None
        if state is not None and not isinstance(state, dict):
            raise TypeError("state 必须是字典")
        if summary is not None and not isinstance(summary, str):
            raise TypeError("summary 必须是字符串")
        if client_request_id is not None:
            if not isinstance(client_request_id, str) or not client_request_id.strip():
                raise ValueError("client_request_id 必须是非空字符串")

        turn = Turn(
            request_id=request_id,
            user_message=user_message,
            assistant_message=assistant_message,
            metadata=dict(metadata or {}),
        )
        # 先完成序列化，JSON 失败时不会留下半轮数据。
        payload = turn.to_json()
        state_payload = None if state is None else _json(state)

        async with self._maybe_session_lock(user_id, session_id, lock_held):
            key = self.working_key(user_id, session_id)
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.lpush(key, payload)
                pipe.expire(key, self._ttl_s)
                state_key = self.state_key(user_id, session_id)
                summary_key = self.summary_key(user_id, session_id)
                if state_payload is not None:
                    pipe.setex(state_key, self._ttl_s, state_payload)
                else:
                    # 没有新 state 时也续期，避免 state 比 working memory 先过期。
                    pipe.expire(state_key, self._ttl_s)
                if summary is not None:
                    pipe.setex(summary_key, self._ttl_s, summary)
                else:
                    pipe.expire(summary_key, self._ttl_s)
                if client_request_id is not None:
                    receipt_key = self.receipt_key(user_id, session_id)
                    pipe.hset(receipt_key, client_request_id, payload)
                    pipe.expire(receipt_key, self._ttl_s)
                await pipe.execute()
        return turn

    async def get_receipt(
        self, user_id: str, session_id: str, client_request_id: str,
    ) -> Turn | None:
        """读取同一 client_request_id 已提交的轮次；没有则返回 None。"""
        if not isinstance(client_request_id, str) or not client_request_id.strip():
            raise ValueError("client_request_id 必须是非空字符串")
        raw = await self._client.hget(self.receipt_key(user_id, session_id), client_request_id)
        return Turn.from_json(raw) if raw else None

    async def get_turns(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[Turn]:
        """按时间顺序（旧 -> 新）返回轮次。"""
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("limit 必须是正整数")
        stop = -1 if limit is None else limit - 1
        raws = await self._client.lrange(self.working_key(user_id, session_id), 0, stop)
        return [Turn.from_json(raw) for raw in reversed(raws)]

    async def get_snapshot(
        self,
        user_id: str,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> MemorySnapshot:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("limit 必须是正整数")
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.lrange(self.working_key(user_id, session_id), 0, -1 if limit is None else limit - 1)
            pipe.mget([self.state_key(user_id, session_id), self.summary_key(user_id, session_id)])
            raws, (state_raw, summary) = await pipe.execute()
        return MemorySnapshot(
            turns=tuple(Turn.from_json(raw) for raw in reversed(raws)),
            state=json.loads(state_raw) if state_raw else {},
            summary=summary or "",
        )

    async def commit_compaction(
        self, user_id: str, session_id: str, *, snapshot: MemorySnapshot, summary: str,
        episode: Episode | None = None,
    ) -> bool:
        """条件提交摘要，保留快照最近两轮及生成期间新增的所有轮次。

        snapshot 必须来自未设 limit 的 get_snapshot。不持有长时间会话锁；
        WATCH 保护检查和提交之间的变化，过期/被裁剪/摘要变化则放弃。
        """
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary 必须是非空字符串")
        size = len(snapshot.turns)
        if size <= 2:
            return False
        # 在裁剪前序列化好脱敏记录；与摘要/裁剪一起提交，向量库失败也不丢 episode。
        episode_payload = None if episode is None else _json(episode.to_dict())
        key = self.working_key(user_id, session_id)
        skey = self.summary_key(user_id, session_id)
        for _ in range(3):
            async with self._client.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key, skey)
                    current = await pipe.lrange(key, 0, -1)
                    old_summary = await pipe.get(skey) or ""
                    if len(current) < size or old_summary != snapshot.summary:
                        return False
                    original = tuple(Turn.from_json(raw) for raw in reversed(current[-size:]))
                    if original != snapshot.turns:
                        return False
                    keep_count = len(current) - size + 2
                    pipe.multi()
                    if episode_payload is not None:
                        pipe.hset(PENDING_EPISODES_KEY, episode.episode_id, episode_payload)
                    pipe.setex(skey, self._ttl_s, summary)
                    pipe.ltrim(key, 0, keep_count - 1)
                    pipe.expire(key, self._ttl_s)
                    pipe.expire(self.state_key(user_id, session_id), self._ttl_s)
                    await pipe.execute()
                    return True
                except WatchError:
                    continue
        return False

    async def get_pending_episodes(self, limit: int = 20) -> list[Episode]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须为正整数")
        episodes = []
        async for _, payload in self._client.hscan_iter(PENDING_EPISODES_KEY):
            episodes.append(Episode(**json.loads(payload)))
            if len(episodes) >= limit:
                break
        return episodes

    async def ack_episode(self, episode_id: str, *, profile_episode: Episode | None = None) -> None:
        """仅向量写成功后调用；移除 episode 待办与画像交接在同一事务完成。"""
        if profile_episode is not None and profile_episode.episode_id != episode_id:
            raise ValueError("episode ID 不一致")
        payload = None if profile_episode is None else _json(profile_episode.to_dict())
        async with self._client.pipeline(transaction=True) as pipe:
            if payload is not None:
                pipe.hset(PENDING_PROFILES_KEY, episode_id, payload)
            pipe.hdel(PENDING_EPISODES_KEY, episode_id)
            await pipe.execute()

    @asynccontextmanager
    async def profile_lock(self, user_key: str) -> AsyncIterator[None]:
        if len(user_key) != 64 or any(c not in '0123456789abcdef' for c in user_key):
            raise ValueError("需要用户哈希")
        async with self._process_locks.hold("profile:" + user_key):
            async with redis_lease(self._client, "lock:profile:" + user_key):
                yield

    async def get_pending_profile(self, episode_id: str) -> Episode | None:
        raw = await self._client.hget(PENDING_PROFILES_KEY, episode_id)
        return Episode(**json.loads(raw)) if raw else None

    async def get_pending_profiles(self, limit: int = 20) -> list[Episode]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit 必须为正整数")
        result = []
        async for _, raw in self._client.hscan_iter(PENDING_PROFILES_KEY):
            result.append(Episode(**json.loads(raw)))
            if len(result) >= limit:
                break
        return result

    async def profile_applied(self, episode_id: str) -> bool:
        return bool(await self._client.hexists(APPLIED_PROFILES_KEY, episode_id))

    async def ack_profile(self, episode_id: str) -> None:
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.hset(APPLIED_PROFILES_KEY, episode_id, "1")
            pipe.hdel(PENDING_PROFILES_KEY, episode_id)
            await pipe.execute()

    async def get_state(self, user_id: str, session_id: str) -> dict[str, Any]:
        raw = await self._client.get(self.state_key(user_id, session_id))
        return json.loads(raw) if raw else {}

    async def set_state(self, user_id: str, session_id: str, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise TypeError("state 必须是字典")
        await self._client.setex(self.state_key(user_id, session_id), self._ttl_s, _json(state))

    async def get_summary(self, user_id: str, session_id: str) -> str:
        return await self._client.get(self.summary_key(user_id, session_id)) or ""

    async def set_summary(self, user_id: str, session_id: str, summary: str) -> None:
        if not isinstance(summary, str):
            raise TypeError("summary 必须是字符串")
        await self._client.setex(self.summary_key(user_id, session_id), self._ttl_s, summary)

    async def ping(self) -> bool:
        """健康检查；连接失败时由调用方捕获异常。"""
        return bool(await self._client.ping())

    async def close(self) -> None:
        await self._client.aclose()
