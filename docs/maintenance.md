# 后台记忆维护队列

`memory/maintainer.py` 提供 `MemoryMaintainer` 工具类。它是调度员，不负责生成摘要内容、拼上下文或提交聊天本身；复用 `MemoryCompactor`、Redis 条件提交和长期记忆待办能力。`/chat` 已通过 `memory/service.py` 的 `ConversationMemory` 在成功轮次后调用 `submit()`；下列代码是等价的直接接入方式。

## 谁调用，什么时候调用

```python
from memory.profile import ProfileUpdater
from memory.longterm import LongTermMemory
from memory.compactor import MemoryCompactor
from memory.maintainer import MemoryMaintainer

# 应用启动：先统一加载环境变量，准备好已有的 store 和 llm。
profiles = ProfileUpdater.from_env(store, llm.extract_profile)
longterm = LongTermMemory.from_env(profile_updater=profiles)
compactor = MemoryCompactor(store, llm.summarize, longterm=longterm)
maintenance = MemoryMaintainer.from_env(compactor, store=store, longterm=longterm)
maintenance.start()  # 在事件循环中调用；启动固定数量任务，重复调用不多建任务。

# 用户请求成功，完整一轮问答已经得到：
turn = await store.commit_turn(
    user_id, session_id, request_id=request_id,
    user_message=question, assistant_message=answer,
)
if turn is not None:
    status = maintenance.submit(user_id, session_id)
    # queued / coalesced / full / stopped；不用等待摘要生成即可返回 answer。

# 应用退出：先停维护，再按原有方式关闭 compactor、Redis、LLM 等依赖。
await maintenance.close(drain=True, timeout_s=5)
await compactor.close()
```

可用 `async with MemoryMaintainer(...) as maintenance:` 代替显式 start/close。不要在这条链路同时调用 `compactor.start()`；它保留给旧调用方，但会绕过统一队列的容量/worker 限制。也不要为每次聊天创建一个新的 maintainer，应在服务实例中共用一个。

## 请求如何排队、合并

`submit()` 是同步、非阻塞方法，只登记 `user_id + session_id`，队列不复制聊天正文。

- 没有待办：在容量允许时进入队列，返回 `queued`。
- 同会话已排队：返回 `coalesced`，不再插一份。worker 开始时会读取最新 Redis 历史。
- 同会话正在运行：返回 `coalesced`，把该会话标记 dirty。此次结束后，将它重新排到队尾再检查一次。多次重复请求只保留一个标记。
- 不同会话超出容量：返回 `full`，立即跳过本次维护请求，不阻塞聊天、不删除已存问答。
- 未 start、关闭中或已关闭：返回 `stopped`。

`max_pending=128` 限制的是**排队中加运行中的不同会话总数**，不是单独的等待长度。例如容量 2、一个会话正在运行、另一个在排队，此时第三个会话返回 full；同一个运行会话仍可以合并请求。这个计数方式为 dirty 再检查预留了位置，不会因队列满而丢掉已合并的后续检查。

每实例默认 2 个 worker，直接 await `compactor.compact(..., flush_pending=False)`；不为每个会话创建一个 Task。dirty 会话重新到队尾，避免持续活跃会话一直霸占同一个 worker。`join()` 等待这些会话工作、重试和 dirty 检查完成，不等待周期性待写扫描。

## 压缩重试和并发保护

每次调用 compact 都重新读取快照、统计 working memory token。阈值仍为 10,800 tokens；最近两轮/四条消息仍不摘要、不裁剪。本步没有改变画像的 episode 摘要来源。

- `failed`、`conflict` 或普通异常：有限重试，默认首次加最多 2 次重试，总共最多 3 次。
- 退避默认 0.25、0.5 秒，继续增加时最多 5 秒；退避占用该 worker，但不持有会话提交锁。
- 成功、低于阈值、没有旧轮次：不再重试。
- 超过重试次数：清理本次队列项；原历史仍保留。以后新提交维护请求可以再次尝试，不会自行无限重试。
- 取消继续向上传播，不当成普通失败重新排队。

单个 maintainer 内，同会话排队/运行最多一份。跨实例/进程可能同时生成摘要，仍由已有 `RedisMemoryStore.commit_compaction()` 的 WATCH/MULTI 保护提交：检查旧 summary 和原历史；已经过时的任务返回 conflict，不重复覆盖。重试会读新快照，不能复用旧模型结果。

因此不保证跨进程只调用一次 LLM，但保护同一旧快照不被重复提交。摘要期间新增轮次可以正常 commit；LTRIM 会保留原快照最近两轮及生成期间新增的全部轮次。dirty 再检查若仍超阈值，可以继续处理新的旧轮次，最新两轮始终受保护。

## episode 和画像如何独立重试

传入 `store + longterm` 后，启动一个独立待写扫描任务，启动时立即处理一次，随后每次扫描结束等待默认 5 秒。默认每批 20 个，单次等待预算 30 秒。配置了 longterm 却没有 store 会报错；不传 longterm 则不启动自动待写扫描。

它调用现有的 `longterm.flush_pending(store, limit=20)`：

```text
Redis episode 待办 → embedding + Chroma upsert
    → 原子交接画像待办
    → 注入了 profile_updater 时继续处理画像
```

即使没有新的聊天、没有达到压缩阈值，也会重试历史待写项。没有新 episode 时也能处理已有画像待办。服务错误或超时后等下一周期再尝试；成功才确认删除 Redis 待办。画像 updater 未注入时，画像任务仍保留，但不会自动执行。

这条扫描与压缩 worker 分开，所以 Chroma 故障不会使已成功的摘要再次生成，也不会占用压缩队列名额。worker 数量只限制此实例的压缩并发；另外最多一个待写扫描任务，画像抽取/embedding 的已有行为由该链路负责。多进程部署会各有一组 worker，待办重复投递依靠既有稳定 ID、upsert、画像锁和回执保护，不宣称全局只有一次投递。

## 方法与状态

| 能力 | 输入 | 输出 / 用途 |
| --- | --- | --- |
| `MemoryMaintainer(...)` / `from_env(...)` | compactor，可选 store、longterm，容量/并发/重试配置 | 未启动的调度工具对象 |
| `start()` | 无 | 启动固定 worker 和可选扫描器，不关闭依赖 |
| `submit(user_id, session_id)` | 已存成功问答对应的用户、会话 | queued/coalesced/full/stopped |
| `join()` | 无 | 等会话队列清空；测试及优雅退出可用 |
| `stats()` | 无 | 计数和当前 pending/queued_now/running，不含用户内容 |
| `close(drain=False, timeout_s=5)` | 是否限时排空 | 停接收、取消并等待任务、清理内存队列；可重复调用 |
| `MemoryCompactor.compact(..., flush_pending=True)` | 原参数及可选开关 | 兼容旧调用；maintainer 显式设 False，待写由独立扫描器负责 |

`stats()` 的事件计数包括 queued、coalesced、full、stopped、压缩状态、retries、retry_exhausted、followups、pending_scans、pending_errors、episodes_flushed 和 drain_timeouts；尚未发生的事件可能没有键，可用 `.get(name, 0)`。

close 的 timeout_s 是**排空等待上限**；实际取消若遇到已有画像写线程，会继续等其安全收尾，因此不是强制杀线程的硬时限。关闭不会关闭调用方拥有的 compactor、store、LLM 或 Chroma 客户端。

会话调度队列是进程内的，重启不会恢复未运行的会话触发；Redis 原始 working memory 仍在，后续提交可再次触发。队列满时被跳过的维护也依赖后续新请求再次触发。本步没有增加全库扫描 working memory 或持久化会话任务队列。episode/画像待办则保存在 Redis，重启后可由扫描器继续处理。

## 配置与测试

环境变量默认值：

```dotenv
COMMERCEMIND_MEMORY_MAINTAIN_MAX_PENDING=128
COMMERCEMIND_MEMORY_MAINTAIN_WORKERS=2
COMMERCEMIND_MEMORY_MAINTAIN_MAX_RETRIES=2
COMMERCEMIND_MEMORY_MAINTAIN_RETRY_DELAY_S=0.25
COMMERCEMIND_MEMORY_MAINTAIN_PENDING_INTERVAL_S=5
```

`retry_max_delay_s=5`、`pending_batch_size=20`、`pending_timeout_s=30` 可通过构造参数覆盖。非法配置会报错，不静默忽略。

```bash
.venv/bin/python -m pytest -q tests/test_memory_maintainer.py
.venv/bin/python -m pytest -q
```

测试用 fake Redis、LLM 和 Chroma，不调用付费服务，覆盖队列满、排队/运行合并、固定并发、失败/冲突重试、摘要期间新增问答、两个维护器并发、无新聊天时 episode/画像重试，以及关闭和取消。
