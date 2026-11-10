"""有界后台记忆维护：会话合并、有限重试、独立的持久化待办扫描。

队列存 user/session 标识，不存聊天正文。max_pending 包含运行中的会话，
以便为运行中收到的新请求保留一个后续检查位置，不创建每会话后台 Task。
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from memory.compactor import MemoryCompactor

logger = logging.getLogger(__name__)
SubmitStatus = Literal["queued", "coalesced", "full", "stopped"]


@dataclass
class _SessionJob:
    running: bool = False
    dirty: bool = False


class MemoryMaintainer:
    def __init__(self, compactor: MemoryCompactor, *, store=None, longterm=None,
                 max_pending: int = 128, workers: int = 2, max_retries: int = 2,
                 retry_delay_s: float = .25, retry_max_delay_s: float = 5,
                 pending_interval_s: float = 5, pending_batch_size: int = 20,
                 pending_timeout_s: float = 30):
        for name, value in (("max_pending", max_pending), ("workers", workers),
                            ("pending_batch_size", pending_batch_size)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries 必须是非负整数")
        for name, value in (("retry_delay_s", retry_delay_s), ("retry_max_delay_s", retry_max_delay_s),
                            ("pending_interval_s", pending_interval_s), ("pending_timeout_s", pending_timeout_s)):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是有限正数")
        if longterm is not None and store is None:
            raise ValueError("待写重试需要 store")
        self.compactor, self.store, self.longterm = compactor, store, longterm
        self.max_pending, self.workers, self.max_retries = max_pending, workers, max_retries
        self.retry_delay_s, self.retry_max_delay_s = retry_delay_s, retry_max_delay_s
        self.pending_interval_s = pending_interval_s
        self.pending_batch_size, self.pending_timeout_s = pending_batch_size, pending_timeout_s
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=max_pending)
        self._jobs: dict[tuple[str, str], _SessionJob] = {}
        self._tasks: list[asyncio.Task] = []
        self._counts: Counter = Counter()
        self._accepting = self._started = self._stopping = self._closed = False
        self._close_lock = asyncio.Lock()

    @classmethod
    def from_env(cls, compactor, *, store=None, longterm=None):
        return cls(compactor, store=store, longterm=longterm,
            max_pending=int(os.getenv("COMMERCEMIND_MEMORY_MAINTAIN_MAX_PENDING", "128")),
            workers=int(os.getenv("COMMERCEMIND_MEMORY_MAINTAIN_WORKERS", "2")),
            max_retries=int(os.getenv("COMMERCEMIND_MEMORY_MAINTAIN_MAX_RETRIES", "2")),
            retry_delay_s=float(os.getenv("COMMERCEMIND_MEMORY_MAINTAIN_RETRY_DELAY_S", ".25")),
            pending_interval_s=float(os.getenv("COMMERCEMIND_MEMORY_MAINTAIN_PENDING_INTERVAL_S", "5")))

    def start(self) -> None:
        """在运行中的事件循环调用；启动固定数量 worker，重复调用不会多建任务。"""
        if self._closed:
            raise RuntimeError("maintainer 已关闭")
        if self._started:
            return
        asyncio.get_running_loop()
        self._started = self._accepting = True
        self._tasks = [asyncio.create_task(self._worker(), name=f"memory-worker-{i}")
                       for i in range(self.workers)]
        if self.longterm is not None:
            self._tasks.append(asyncio.create_task(self._pending_loop(), name="memory-pending"))

    def submit(self, user_id: str, session_id: str) -> SubmitStatus:
        """成功问答存 Redis 后调用；不等待队列、不阻塞聊天，不负责提交聊天本身。"""
        if not self._accepting:
            self._counts["stopped"] += 1
            return "stopped"
        key = (str(user_id), str(session_id))
        existing = self._jobs.get(key)
        if existing is not None:
            if existing.running:
                existing.dirty = True
            self._counts["coalesced"] += 1
            return "coalesced"
        if len(self._jobs) >= self.max_pending:
            self._counts["full"] += 1
            return "full"
        self._jobs[key] = _SessionJob()
        self._queue.put_nowait(key)
        self._counts["queued"] += 1
        return "queued"

    def stats(self) -> dict[str, int]:
        return {**self._counts, "pending": len(self._jobs), "queued_now": self._queue.qsize(),
                "running": sum(job.running for job in self._jobs.values())}

    async def join(self) -> None:
        """等待已接收的会话工作（含重试/dirty 再检查）；不等待周期待写扫描。"""
        await self._queue.join()

    async def _compact_with_retry(self, key: tuple[str, str]) -> None:
        delay = min(self.retry_delay_s, self.retry_max_delay_s)
        for attempt in range(self.max_retries + 1):
            try:
                # 用当前 worker await，而非 compactor.start()，避免绕开 worker 并发上限。
                # 待写扫描交给独立任务，不能让 Chroma 故障触发重新生成摘要。
                result = await self.compactor.compact(*key, flush_pending=False)
                status = result.status
            except Exception as exc:
                logger.warning("memory_maintenance_failed error_type=%s", type(exc).__name__)
                status = "failed"
            self._counts[status] += 1
            if status not in ("failed", "conflict"):
                return
            if attempt == self.max_retries:
                self._counts["retry_exhausted"] += 1
                return
            self._counts["retries"] += 1
            # 每次重新调用 compact，重新读 Redis 快照；不提交过时的模型结果。
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.retry_max_delay_s)

    async def _worker(self) -> None:
        while True:
            key = await self._queue.get()
            job = self._jobs[key]
            job.running = True
            try:
                await self._compact_with_retry(key)
            finally:
                if job.dirty and not self._stopping:
                    job.dirty = job.running = False
                    # 总 pending 数包含当前工作，因此此处始终有保留的队列位置。
                    self._queue.put_nowait(key)
                    self._counts["followups"] += 1
                else:
                    self._jobs.pop(key, None)
                self._queue.task_done()

    async def _pending_loop(self) -> None:
        # 启动时立即扫描；之后即使没有新聊天/没有压缩，也持续重试持久化待办。
        while True:
            try:
                self._counts["pending_scans"] += 1
                sent = await asyncio.wait_for(
                    self.longterm.flush_pending(self.store, limit=self.pending_batch_size),
                    timeout=self.pending_timeout_s)
                self._counts["episodes_flushed"] += sent
            except Exception as exc:
                self._counts["pending_errors"] += 1
                logger.warning("memory_pending_retry_failed error_type=%s", type(exc).__name__)
            await asyncio.sleep(self.pending_interval_s)

    async def close(self, *, drain: bool = False, timeout_s: float = 5) -> None:
        """停止接收；可限时排空，否则取消。依赖的 compactor/store/LLM 由调用方关闭。"""
        if isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s 必须是有限正数")
        async with self._close_lock:
            if self._closed:
                return
            self._accepting = False
            try:
                if drain and self._started:
                    try:
                        await asyncio.wait_for(self.join(), timeout=timeout_s)
                    except asyncio.TimeoutError:
                        self._counts["drain_timeouts"] += 1
            finally:
                self._stopping = True
                for task in self._tasks:
                    task.cancel()
                await asyncio.gather(*self._tasks, return_exceptions=True)
                while not self._queue.empty():
                    self._queue.get_nowait()
                    self._queue.task_done()
                self._jobs.clear()
                self._tasks.clear()
                self._closed = True

    async def __aenter__(self):
        self.start()
        return self

    async def __aexit__(self, *args):
        await self.close()
