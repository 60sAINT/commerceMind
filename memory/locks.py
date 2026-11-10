"""同一会话的进程内与跨进程互斥。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import uuid4

from redis.exceptions import WatchError

logger = logging.getLogger(__name__)

_ACQUIRE_POLL_S = 0.05


class LockBusyError(RuntimeError):
    """等待锁超时；调用方通常转换为 409/429。"""


class LeaseLostError(RuntimeError):
    """锁续租失败，持有者必须停止写入。"""


class KeyedLocks:
    """同进程按键排队；没有等待者时回收锁对象。"""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


@asynccontextmanager
async def redis_lease(
    client: Any,
    key: str,
    *,
    wait_s: float = 30,
    lease_s: float = 120,
) -> AsyncIterator[None]:
    """跨进程互斥；用 SET NX PX 获取，WATCH/MULTI 续租和释放。

    不依赖 Redis Lua，因此 fakeredis 单元测试不需要额外安装 lupa。
    """
    token = uuid4().hex
    lease_ms = max(1, int(lease_s * 1000))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while not await client.set(key, token, nx=True, px=lease_ms):
        if loop.time() >= deadline:
            raise LockBusyError("当前会话正在处理请求，请稍后重试")
        await asyncio.sleep(_ACQUIRE_POLL_S)

    owner = asyncio.current_task()
    lost = False

    async def extend() -> bool:
        async with client.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if await pipe.get(key) != token:
                    return False
                pipe.multi()
                pipe.pexpire(key, lease_ms)
                await pipe.execute()
                return True
            except WatchError:
                return False

    async def release() -> None:
        async with client.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if await pipe.get(key) == token:
                    pipe.multi()
                    pipe.delete(key)
                    await pipe.execute()
            except WatchError:
                pass

    async def renew() -> None:
        nonlocal lost
        try:
            while True:
                await asyncio.sleep(lease_s / 3)
                if not await extend():
                    raise LeaseLostError("会话锁续租失败")
        except asyncio.CancelledError:
            raise
        except Exception:
            lost = True
            if owner is not None:
                owner.cancel()

    renewal = asyncio.create_task(renew())
    try:
        yield
    except asyncio.CancelledError:
        if lost:
            raise LeaseLostError("会话锁已失效，本次请求已中止") from None
        raise
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        try:
            await release()
        except Exception as exc:
            logger.warning("释放会话锁失败: %s", type(exc).__name__)
