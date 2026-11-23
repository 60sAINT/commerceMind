# CommerceMind 记忆机制：沿一次流式 Chat 读懂代码与存储

## 1. 先看整体流程图

```mermaid
flowchart TD
    A[客户端 POST /chat/stream<br/>message / user_id / session_id / client_request_id] --> B[处理 Skill 指令并选择 Skill]
    B --> C{已有幂等回执？<br/>先 PostgreSQL，后 Redis}
    C -->|有| D[回放 meta / delta / done<br/>不重新调用回答模型]
    C -->|无| E{Redis 记忆服务可用？}
    E -->|否| F[无历史模式流式回答]
    E -->|是| G[获取会话锁，再检查会话回执]
    G --> H[Redis 一致快照<br/>working + summary + state]
    H --> I[working 展开为历史消息<br/>summary 注入 system]
    I --> J[可选：意图识别<br/>更新本轮 state 并注入 system]
    J --> K[LLM 流式生成<br/>内存 full_text 累积，逐段发送 delta]
    K --> L{成功完成、非空<br/>并通过断连检查？}
    L -->|否| X[不提交本轮记忆<br/>异常时发送 error 或取消]
    L -->|是| U[退出当前会话锁作用域<br/>当前实现的并发隐患，见第 13 节]
    U --> P[配置了 DATABASE_URL 时<br/>PostgreSQL 事务保存完整问答]
    F --> P
    P --> R[Redis MULTI/EXEC<br/>LPUSH Turn / 更新 state / 刷新 TTL / 写回执]
    R --> DONE[发送 done<br/>不等待摘要和向量化完成]
    R --> Q[维护器 submit 会话标识<br/>有界进程内队列、固定 worker]
    Q --> T{working tokens ≥ 10800<br/>且超过两轮？}
    T -->|否| N[本次不压缩]
    T -->|是| S[最近两轮之前的原文 → 新增摘要<br/>有旧 summary 时再合并]
    S --> W[WATCH 校验快照，MULTI/EXEC<br/>更新 summary、裁剪旧轮次<br/>符合轮数时登记 episode 待办]
    W --> O[(Redis pending_episodes<br/>当前默认链路到此为止)]
    O -. 显式注入 longterm 后才启用 .-> LT[独立待办扫描<br/>脱敏 → embedding → Chroma upsert]
    LT -. 成功后原子交接 .-> OP[(Redis pending_profiles)]
    OP -. 显式注入 ProfileUpdater .-> PR[LLM 提取画像增量<br/>校验 → 用户锁内读取和合并]
    PR -. 生成语义向量并 upsert .-> PC[(Chroma 用户画像)]
    LT -. 情景 search 已实现<br/>尚未接入 Chat .-> FUT[未来：召回文本后加入回答上下文]
    PC -. 精确读取和向量检索已实现<br/>尚未接入 Chat .-> FUT
```

图中的实线表示当前代码已连接的正常路径；虚线表示模块能力已经实现，但默认应用启动和 Chat 尚未接通的部分。简化了锁内重复请求回放、Redis 读取降级等分支，后文逐一展开。Redis 不可用时 `R` 实际跳过；PostgreSQL 未配置时 `P` 实际跳过；PostgreSQL 提交失败会终止并返回 error，Redis 提交异常则记录日志后仍会发送 done。

**阅读基准**：2026-11-23 当前工作区代码，Git HEAD 为 `ae33d07`。本文依据函数调用和缩进判断实际行为，不把历史说明、环境变量注释或未来规划当成已经接通的功能。示例用户统一使用 `demo-user`，例子中的回答与数据均为教学构造，不代表真实订单状态。

**核心结论**：这套机制通过“每次请求重新加载历史并构造模型输入”实现记忆。当前 Chat 的模型上下文来自 Redis；PostgreSQL 保存完整会话历史；Chroma 情景记忆与画像已有实现，但不会在默认 `/chat/stream` 中自动写入或召回。仅看到 Chroma 健康不等于长期记忆已经参与回答。

## 2. 学习地图：到底有哪几种记忆

| 名称 | 解决什么问题 | 从哪里读、存在哪里 | 当前 Chat 状态 |
| --- | --- | --- | --- |
| 工作记忆 Working Memory | 最近还保留着的完整问答，支持多轮指代 | Redis List：`wm_v1:<会话复合哈希>` | 已读、已写 |
| 会话摘要 Summary | 工作记忆压缩后，保留更早对话的概括 | Redis String：`summary_v1:<会话复合哈希>` | 已读；后台压缩写入 |
| 会话状态 State | 本会话最新的结构化状态，目前主要是意图识别结果 | Redis String，内容是 JSON：`state_v1:<会话复合哈希>` | 已读；意图成功时随完整问答写入 |
| 情景记忆 Episodic Memory | 跨会话回忆“某一段咨询发生了什么” | Redis 待办 → Chroma `episodic_<模型哈希前12位>` | 压缩可产生待办；默认不自动写 Chroma、不召回 |
| 用户画像 User Profile | 跨会话积累稳定偏好、关注商品类别和咨询类别 | Chroma `user_profiles_v2_<模型哈希前12位>` | 模块已实现；默认不更新、不注入 Chat |
| 完整历史 History | 会话列表、消息翻页、持久回放 | PostgreSQL `sessions` / `session_messages` | 已写、用于回执和历史接口；Chat 不从这里恢复上下文 |
| 幂等回执 Receipt | 网络重试时复用成功结果 | Redis 两类 Hash；PostgreSQL 问答记录 | 已接入，属于可靠性配套数据 |

这里把摘要和 state 分开讲，是因为它们的生产者、模型注入位置和更新规则不同。回执、待办和锁不是模型理解意义上的长期记忆，却决定了记忆能否可靠保存，不能省略。

推荐阅读顺序：先看第 3 节的一次请求，再读第 4～8 节各层数据；第 9～11 节提供 Python 操作和调试；最后看可靠性边界与后续接入建议。

### 2.1 源码导航

以下路径均相对于项目根目录 `C:/Users/lixinyi/Desktop/commerceMind`；行号对应本次阅读快照，后续修改可能移动。

| 源文件及核心入口 | 本次行号 | 职责 |
| --- | --- | --- |
| `api/main.py::chat_stream` | 508～689 | SSE、读上下文、调用模型、按顺序提交 |
| `memory/service.py::ConversationMemory` | 71～235 | 门面；组合 store、context、compactor、maintainer |
| `memory/context.py::ContextBuilder` | 34～40 | 把 Redis 快照转换为回答上下文 |
| `memory/models.py::Turn / MemorySnapshot / Episode` | 15～138 | 数据对象、序列化、稳定 ID |
| `memory/store.py::RedisMemoryStore` | 31～353 | Redis 键、事务、回执、压缩条件提交、待办 |
| `memory/compactor.py::MemoryCompactor.compact` | 49～93 | 统计 token、生成和合并摘要、发起压缩提交 |
| `memory/maintainer.py::MemoryMaintainer` | 28～192 | 有界队列、固定 worker、重试、独立待办扫描 |
| `memory/longterm.py::LongTermMemory` | 28～140 | 情景记忆 embedding、Chroma 写入和检索 |
| `memory/profile.py::ProfileUpdater` | 122～321 | 画像抽取、合并、精确读取、向量检索 |
| `memory/embeddings.py` | 7～49 | embedding 后端与向量合法性校验 |
| `memory/tokens.py` / `memory/budget.py` | `TokenCounter` / `TokenBudget` | 本地 token 统计、压缩阈值 |
| `memory/locks.py::redis_lease` | 48～126 | 会话/画像的跨进程租约 |
| `api/sessions.py::SessionRepository` | 15～179 | PostgreSQL 会话、完整消息及幂等 |
| `core/llm.py::_build_messages / stream_chat` | 48～70 / 134～171 | 最终模型消息拼接、上游流式调用 |

## 3. 沿一次 POST /chat/stream 走完整条链路

### 3.1 请求里的四个标识分别干什么

```json
{
  "message": "上次那双运动鞋，我还是想退货",
  "user_id": "demo-user",
  "session_id": "demo-session",
  "client_request_id": "demo-request-002"
}
```

| 字段 | 含义 | 使用要点 |
| --- | --- | --- |
| `user_id` | 用户维度 | Redis 隔离、Chroma 用户过滤、PostgreSQL 归属查询都依赖它 |
| `session_id` | 一段会话 | 同用户不同 session 的 Redis 工作记忆、摘要和状态互不共用 |
| `client_request_id` | 客户端为一次逻辑发送生成的 ID | 重试同一条消息应复用；发送新消息应换 ID |
| `request_id` | 服务端生成的追踪 ID | 首次调用生成；回放成功结果时返回原结果的 ID |

`request_id` 不是客户端请求体中的字段。缺失 `session_id` 时服务器会生成；若提供 `client_request_id` 且已有回执，会尽量回到原 session。客户端应从 `meta` / `done` 保存 session ID，以便后续连续对话。

当前没有在这段接口中把 `user_id` 映射到已验证的登录身份；它是客户端传入的业务标识。哈希隔离不等于鉴权，不能把数据按用户分键误认为已经实现了身份验证。

### 3.2 先做 Skill 选择，再查成功回执

源码：`api/main.py:519～563`。

顺序是：处理 Skill 选择 → PostgreSQL 按用户和客户端请求 ID 查回执 → Redis 用户级回执 → 进入 SSE 流。命中回执后直接发 `meta`、包含完整旧回答的 `delta`、`done`。

因此，“重试不会重新调用回答模型”成立；但不能说“整个请求绝不调用任何模型”：自动 Skill 选择发生在回执检查之前，启用 Skill 自动选择时可能已经进行过选择调用。

### 3.3 锁内读取 Redis 一致快照

下面是 `api/main.py` 的核心原句节选，省略了异常处理和重复回执分支：

```python
async with memory.conversation(req.user_id, session_id):
    context = await memory.read_context(req.user_id, session_id)
    state = dict(context.state)
    history = list(context.recent_messages) or None
    intent_history = list(context.recent_messages[-5:]) or None
```

调用关系：

```text
chat_stream
  → ConversationMemory.read_context
    → ContextBuilder.get_context
      → RedisMemoryStore.get_snapshot
```

`get_snapshot()` 的实际读操作在 `memory/store.py:217～234`：

```python
async with self._client.pipeline(transaction=True) as pipe:
    pipe.lrange(self.working_key(user_id, session_id), 0, -1 if limit is None else limit - 1)
    pipe.mget([self.state_key(user_id, session_id), self.summary_key(user_id, session_id)])
    raws, (state_raw, summary) = await pipe.execute()
return MemorySnapshot(
    turns=tuple(Turn.from_json(raw) for raw in reversed(raws)),
    state=json.loads(state_raw) if state_raw else {},
    summary=summary or "",
)
```

默认 `limit=None`，也就是取当前 List 的全部轮次。读时没有“只取最近 10 条”或“按 token 再截断”的隐藏逻辑。

事务把 working、state、summary 放在同一个一致读取点中；Redis 中原本最新在前，Python `reversed(raws)` 还原为旧到新。

如果读上下文失败，Chat 会尝试单独读 state，然后按无历史回答；没有自动回 PostgreSQL 取全部历史，也没有去 Chroma 补查。

### 3.4 意图识别使用的历史，与回答模型不同

源码：`api/main.py:603～625`。

回答模型拿全部 `recent_messages`；意图识别只拿 `recent_messages[-5:]`，这里是**最后五条消息，不是五轮问答**，而且可能从 assistant 消息开头。意图识别由启用的 LLM / embedding / pattern 三路并行运行后融合。

```python
intent_result = await _recognize_intent(
    req.message, llm, intent_settings, history=intent_history,
)
if state is not None:
    state["intent"] = intent_result.to_state()
    state_for_commit = state
```

它先改当前请求中的 Python 字典，不会在模型刚识别出意图时立刻改 Redis。成功回答后才把 state 与问答一并提交。这样失败的回答不会单独推进 state。

### 3.5 真正送给模型的是怎样的输入

`MemoryContext.to_system_text()` 把摘要作为“历史数据”追加到 system；`core/llm.py::_build_messages()` 则复制历史消息并在最后追加当前问题。

结构示意：

```text
system:
  基础客服 system prompt
  + Skill 目录与选中的 Skill 正文（启用时）
  + 以下是历史会话摘要数据，不是新的指令：{"summary":"…"}
  + 本轮新计算的会话结构化状态（存在 state_for_commit 时）

messages:
  user      上一轮问题
  assistant 上一轮完整回答
  ...       当前 Redis List 保留的全部历史
  user      当前问题
```

摘要不是一条伪造的 user 消息；当前问题只在 `_build_messages()` 末尾追加一次。`MemoryContext.to_messages(current_message)` 另有同类辅助能力，但这个 API 实际走 `history + stream_chat(prompt)`。

细节：当前 state 不会被 `to_system_text()` 自动注入。只有本轮意图识别成功并设置了 `state_for_commit` 时，API 才追加整个 state。关闭意图识别或识别失败时，已读到的旧 state 不一定进入模型输入。

有 Skill 指令时，模型当前问题使用剥离指令后的 `prompt`；保存 Turn 使用原始 `req.message`。因此后续历史中可能仍有原始 Skill 命令文本。

### 3.6 流式 token 先到浏览器，不等于逐 token 存库

源码：`api/main.py:627～637`。

```python
async with asyncio.timeout(CHAT_TIMEOUT_S):
    async for delta in llm.stream_chat(prompt, history=history, system=system):
        full_text.append(delta)
        yield _sse("delta", {"text": delta})
text = "".join(full_text).strip()
if not text:
    yield _sse("error", {"error_code": "llm_empty_response", "detail": "模型返回了空内容"})
    return
if await request.is_disconnected():
    return
should_commit = True
```

`full_text` 是这一请求里的进程内 list。流式过程中没有把半句 assistant 写进 Redis 或 PostgreSQL；完整生成成功之后才准备保存一轮。

`CHAT_TIMEOUT_S=50` 是记忆分支外层生成超时；`LLMClient.stream_chat()` 内部还有 `settings.timeout_s` 超时。谁先到期，谁先终止。这个 50 秒不包住前面的 Skill 选择、等待会话锁与意图识别。无记忆分支也没有套 API 的这层 50 秒，只受 LLM 客户端自己的超时约束。

### 3.7 完整回答的保存顺序

源码：`api/main.py:638～674`、`memory/service.py:168～198`。

```text
完整回答
  → 配置了 PostgreSQL：sessions.commit_turn(...)
  → 存在记忆服务：memory.commit(...)
      → store.commit_turn(...)：Redis 事务提交
      → maintainer.submit(user_id, session_id)：非阻塞排队
  → SSE done
```

PostgreSQL 是先写的，Redis 是后写的，两者没有跨库事务。PostgreSQL 保存失败会发送 `session_commit_failed` 并退出；Redis 提交失败被捕获后仍发 done。即使未配置 PostgreSQL，Redis 提交异常目前也会被吞掉，所以 **done 不能无条件理解成“记忆肯定保存成功”**。

后台维护可能在 done 前已经开始运行，也可能之后才运行；API 不等它结束。

## 4. 工作记忆：一轮一个 Turn，存入 Redis List

### 4.1 键由什么生成

源码：`memory/store.py:52～65`。

```python
raw = json.dumps([str(user_id), str(session_id)], ensure_ascii=True)
digest = hashlib.sha256(raw.encode()).hexdigest()
```

随后使用：

```text
wm_v1:<digest>
state_v1:<digest>
summary_v1:<digest>
receipt_v1:<digest>
```

要复现键，应直接调 `store.working_key(user_id, session_id)`；不要随手改成拼字符串或紧凑 JSON。JSON 默认分隔空格也影响哈希结果。这个 digest 与业务 `session_id` 不同，也与 Chroma 单独对 user/session 做哈希的结果不同。

### 4.2 一个 List item 长什么样

`Turn` 数据结构在 `memory/models.py:15～71`。下面是一个 item 反序列化后的 JSON：

```json
{
  "turn_id": "demo-turn-001",
  "request_id": "demo-server-request-001",
  "user_message": "这双运动鞋我想退货",
  "assistant_message": "请先提供购买时间与商品状态，以便确认退货条件。",
  "created_at": "2026-11-23T02:00:00+00:00",
  "metadata": {}
}
```

示例 turn ID 为便于阅读手写；生产默认值是 `uuid4().hex`。一轮完整的 user + assistant 占一个 Redis List item，item 本身是 JSON 字符串。不是两条 List item，也不是把每个 token 都存一次。

### 4.3 怎样写、怎样读

`memory/store.py:143～170` 的关键指令为：

```python
async with self._client.pipeline(transaction=True) as pipe:
    pipe.lpush(key, payload)
    pipe.expire(key, self._ttl_s)
    # 同一事务还会处理 state、summary 续期和幂等回执。
    await pipe.execute()
```

假设按时间先后保存 T1、T2、T3：

```text
Redis LRANGE 0 -1：      [T3, T2, T1]
Python reversed：       [T1, T2, T3]
MemorySnapshot messages：[T1.user, T1.assistant, T2.user, T2.assistant, ...]
```

`get_turns(limit=2)` 的含义是取最近两轮，再按旧到新返回。Chat 的 `get_snapshot()` 没传 limit，因此取 List 当前全部内容。

### 4.4 “短期”由什么限制

默认 TTL 是 `7*24*60*60=604800` 秒；每次成功提交会刷新 working、已存在的 state 和 summary 的 TTL。仅仅读取不会续期。

每轮提交本身没有 `LTRIM` 固定窗口，List 主要靠后台压缩裁剪。维护失败、队列满、两轮以内超长输入等情况，都可能让工作记忆继续增长；10800 是后台触发阈值，不是硬上限。

Redis 的 `EXPIRE` 只刷新存在的 key；不会自动创建空 state 或 summary。因此新会话中它们的 TTL 返回 `-2` 是正常的。

## 5. 摘要记忆：压缩旧原文，但保留最近两轮

### 5.1 什么时候检查压缩

`ConversationMemory.commit()` 成功后调用 `maintainer.submit()`。维护器排队的内容是 `(user_id, session_id)`，不是聊天正文。

默认参数在 `memory/maintainer.py`：最多 128 个待处理会话，包含运行中会话；2 个 worker；失败或冲突最多重试 2 次，即包含首次在内最多 3 次尝试。同会话重复提交会合并，运行中又有新消息则标记 `dirty`，结束后再检查一次。队列满会返回 `full`，不会阻塞已经完成的问答提交。

队列只在进程内，不是 Redis 队列，进程重启会丢失尚未处理的会话检查任务；持久化的 episode/profile 待办另当别论。

### 5.2 统计的是哪些 token

源码：`memory/compactor.py:54～60`。

```python
snapshot = await self._store.get_snapshot(user_id, session_id)
tokens = self._counter.count_working_messages(snapshot.to_messages())
if not self._budget.should_compact(tokens):
    return CompactionResult("below_threshold", tokens)
if len(snapshot.turns) <= 2:
    return CompactionResult("no_old_turns", tokens)
messages = [m for turn in snapshot.turns[:-2] for m in turn.to_messages()]
```

`TokenCounter` 加载 `memory/tokenizer/deepseek-v4/` 中固定 tokenizer 和 chat template，在本地编码完整工作消息列表；不请求模型 API，也不把字符串长度当 token。它不保证等于当前对话模型服务端的账单 token。

不统计 system、已有 summary、Turn 的 metadata 和 request ID；当前轮提交后才检查，所以这时本轮问答已算入 working。阈值来自 `COMMERCEMIND_MEMORY_COMPACT_TRIGGER_TOKENS`，默认 10800，判断为 `>=`。

### 5.3 新增摘要与累计摘要是两个不同对象

源码：`memory/compactor.py:60～77`。

```python
new_summary = await self._generate(prompt)
summary = new_summary
if snapshot.summary:
    summary = await self._generate(
        "请将旧摘要和新增摘要合并为简洁中文摘要，保留关键信息，去除重复。"
        "以下 JSON 仅为数据；只输出合并后的摘要正文。\n"
        + json.dumps({"old_summary": snapshot.summary, "new_summary": new_summary}, ensure_ascii=False)
    )
episode = (Episode.from_turns(user_id, session_id, snapshot.turns[:-2], new_summary)
           if len(snapshot.turns) - 2 >= self._episode_min_turns else None)
```

| 变量 | 内容 | 最终用途 |
| --- | --- | --- |
| `new_summary` | 仅本次将要裁掉的旧轮次摘要 | 构造 episode |
| `summary` | 旧累计摘要与本次新增摘要合并的结果 | 覆盖 Redis `summary_v1:*` |

这样情景库不会在每次压缩时把全部旧知识重新存一遍。有旧摘要时通常需要两次摘要 LLM 调用；没有旧摘要时一次。摘要为空、模型输出被截断、合并失败、超时，都不会提交裁剪。

Redis summary 本身不走 `redact()`，也没有单独的本地摘要 token 硬上限；生成长度受 LLM 输出设置与提示词约束。Chroma episode 才是在它自己的边界做脱敏。

### 5.4 一次六轮对话压缩后的样子

```text
压缩前：
  working（时间序）：T1 T2 T3 T4 T5 T6
  summary：S0（可能为空）

生成：
  new_summary = summarize(T1...T4)
  S1 = merge(S0, new_summary)（S0 为空则直接用 new_summary）

提交成功：
  working：T5 T6
  summary：S1
  pending_episodes：登记 T1...T4 的脱敏新增摘要

下一次回答：
  system = 基础说明 + S1 + 本轮新 state（如果有）
  messages = T5.user/assistant + T6.user/assistant + 当前问题
```

只有三轮时可以压缩最旧的一轮，但默认最少两轮才构成 episode，因此可能“有新 summary、没有新 episode”。这里的最少两轮是**被压缩段的轮数**，不是整个会话的轮数。

### 5.5 摘要生成期间又来了消息，怎么不误删

`commit_compaction()` 不持有覆盖整个 LLM 摘要过程的长锁。它用 `WATCH` 检查 working 和 summary；验证原快照仍是当前列表的旧端连续部分，且 summary 没变，再计算：

```python
keep_count = len(current) - size + 2
```

例如拿 T1～T6 做快照，生成期间新增 T7，那么保留数量是 `7-6+2=3`：T5、T6、T7 都保留。不会使用固定 `LTRIM 0 1` 把 T7 挤掉的旧保留轮次误删。

事务中一并执行：

```python
if episode_payload is not None:
    pipe.hset(PENDING_EPISODES_KEY, episode.episode_id, episode_payload)
pipe.setex(skey, self._ttl_s, summary)
pipe.ltrim(key, 0, keep_count - 1)
pipe.expire(key, self._ttl_s)
pipe.expire(self.state_key(user_id, session_id), self._ttl_s)
```

发生 WatchError 会有限重试；快照过期、旧段已裁掉或 summary 已变化，会放弃本次结果，维护器可重新读取和生成。这里保证的是 Redis 条件提交；不是 Redis 和 Chroma 的跨库原子提交。

## 6. 情景记忆：从压缩段到 Chroma 向量记录

### 6.1 当前默认链路到底做到哪一步

实际启动代码 `memory/service.py:99～100` 是：

```python
compactor = MemoryCompactor(store, llm.summarize)
maintainer = MemoryMaintainer.from_env(compactor, store=store)
```

没有创建 `LongTermMemory`，也没有传 `longterm=`。压缩仍然可以把 episode 放进 Redis，但没有负责自动发送的 longterm 实例。

`MemoryMaintainer.start()` 只有 `self.longterm is not None` 才启动 `_pending_loop()`，而 worker 调用 `compact(..., flush_pending=False)`。所以目前不能靠“再聊一轮”期待默认应用自动清空长期待办。

### 6.2 Episode 里存什么

`Episode.from_turns()` 输入本次被压缩的 Turn 元组与 `new_summary`，输出：

```text
episode_id = SHA256(JSON([用户哈希, 会话哈希, 此段全部原始 turn_id]))
user_key   = SHA256(user_id)
session_key= SHA256(session_id)
turn_start = SHA256(此段第一轮 turn_id)
turn_end   = SHA256(此段最后一轮 turn_id)
turn_count = 此段轮数
summary    = redact(new_summary)
created_at = 此段最后一轮的 created_at
```

`created_at` 不是写 Chroma 的时间；episode ID 也不是摘要内容的哈希。同一段轮次重试会得到同一 ID，即使模型再次生成的摘要文字不同。

脱敏函数当前只识别已知格式的手机号、邮箱和身份证号，替换为 `[手机号]`、`[邮箱]`、`[身份证]`。这是规则处理，不能保证识别姓名、地址、所有国际电话或混淆个人信息。稳定哈希用于标识，也不等于完整匿名化。

### 6.3 先写 Redis 待办，再写 Chroma

待办键 `memory:pending_episodes:v1` 是 Redis Hash：

```text
field：episode_id（64 位 SHA-256）
value：完整 Episode JSON 字符串（summary 已脱敏）
TTL：代码不设置过期时间
```

它和摘要更新、旧原文裁剪一起提交。这样即使向量服务故障，至少还有可重试的脱敏摘要。这里的“持久化待办”仍然依赖 Redis 自身的持久化与部署可靠性，不表示停电、数据卷丢失时绝不会丢数据。

`LongTermMemory.flush_pending()` 的语义是：有限批次读待办 → `add_episode()` → 成功后 `ack_episode(..., profile_episode=episode)`；发送失败会保留待办，并在本批中停止继续发送 episode。

### 6.4 怎样从 Python 写入 Chroma

源码：`memory/longterm.py:70～82`。

```python
data = episode.to_dict()  # 再次脱敏；embedding 永远不接触原始摘要。
text = data.pop("summary")
data.pop("episode_id")
data["content_hash"] = memory_identity(text)
data["embedding_id"] = self.embedding_id
def write():
    self.collection.upsert(ids=[episode.episode_id], documents=[text], metadatas=[data],
                           embeddings=self._vectors([text]))
await asyncio.to_thread(write)
```

同一 ID 用 `upsert` 更新，便于“Chroma 写成功、Redis 尚未确认就崩溃”之后重试。Chroma Python 客户端和这里的 embedding 函数都是同步操作，代码用 `asyncio.to_thread()` 放到线程里执行，避免直接堵住 FastAPI 事件循环。

注意：客户端通过 HTTP 连接 Chroma 服务，并不是每个 API 进程自己开一个本地 PersistentClient。项目显式传向量，并把 collection 的 `embedding_function` 设为 `None`；向量由应用端编码。

### 6.5 Chroma 的一条 episode 记录长什么样

| 部分 | 类型 | 存放的内容 |
| --- | --- | --- |
| `id` | str | 稳定 episode ID |
| `document` | str | 本次压缩段的脱敏摘要 |
| `metadata` | 扁平 dict | 身份/轮次哈希、轮数、来源时间、内容哈希、embedding 标识 |
| `embedding` | list[float] | 对脱敏摘要编码得到的向量，维度取决于模型 |

`get()` 返回形状示意，尖括号字符串是教学占位，向量仅展示三个元素，不是真实完整维度：

```json
{
  "ids": ["<64位episode_id>"],
  "documents": ["用户咨询运动鞋退货，已说明退货所需信息，联系电话[手机号]。"],
  "metadatas": [{
    "user_key": "<SHA256(user_id)>",
    "session_key": "<SHA256(session_id)>",
    "turn_start": "<SHA256(first_turn_id)>",
    "turn_end": "<SHA256(last_turn_id)>",
    "turn_count": 4,
    "created_at": "2026-11-23T02:00:00+00:00",
    "content_hash": "<SHA256(redacted_summary)>",
    "embedding_id": "siliconflow:BAAI/bge-m3"
  }],
  "embeddings": [[0.12, -0.08, 0.31]]
}
```

这些数组按索引对应同一条记录。document 在这里是自然语言摘要字符串，不是 JSON 对象。后面画像的 document 则是 JSON 字符串，两种用途不同。

### 6.6 跨会话怎么读

接口为 `await longterm.search(user_id, session_id, query)`，**目前 Chat 没有调用它**。

`memory/longterm.py:84～121` 的核心查询：

```python
return self.collection.query(query_embeddings=self._vectors([redact(query)]),
    n_results=max(self.top_k * 4, 12),
    where={"$and": [{"user_key": {"$eq": user}}, {"session_key": {"$ne": session}}]},
    include=["documents", "metadatas", "distances"])
```

这里 `user` 和 `session` 都经过 `memory_identity()`。只找同用户、其他会话的记忆，不召回当前 session；也就是说，即使当前会话早先的 episode 已入 Chroma，也刻意不在本会话内重新召回。

后处理依次进行：

1. 对 query 先脱敏，再生成向量。
2. 多取候选，默认 `max(3*4,12)=12` 条，按 distance 升序排。
3. Python 再验证用户、排除当前 session、丢弃非有限 distance。
4. 使用 `score=clamp(1-distance,-1,1)`，默认 `score>=0.55` 才保留。
5. 对返回正文再次脱敏，按 ID 和正文去重。
6. 截短返回副本，使所有正文用空行拼接后合计不超过默认 600 tokens。
7. 最多返回默认 3 条 `EpisodeMatch`；异常记录类型后返回空列表。

0.55 是相关性筛选分数，不是事实正确率或概率。600 是所有返回正文的总预算，既不是每条 600，也不包含将来调用方添加的标签、分数和标题等包装。

## 7. 用户画像：episode 成功入库后才提取

### 7.1 画像与 state 有什么不同

| 对比项 | 会话 state | 用户画像 |
| --- | --- | --- |
| 作用域 | 用户 + session | 用户，跨 session |
| 典型内容 | 当前意图 `refund`、置信度、识别来源 | 偏好宽松版型、关注运动鞋、经常咨询退换货 |
| 存储 | Redis JSON String | Chroma 画像记录 |
| 更新时机 | 本轮生成成功后 | episode 成功写 Chroma 后，提取并合并 |
| 默认 Chat 接入 | 已接入，但注入有条件 | 尚未接入 |

“问过运动鞋”只能说明关注商品类别，不能自动推出“喜欢运动鞋”。画像抽取提示词明确要求不把助手建议当用户偏好、不推测身份。

### 7.2 上游为什么是 pending_profiles

`store.ack_episode()` 在同一个 Redis 事务里做两件事：

```python
if payload is not None:
    pipe.hset(PENDING_PROFILES_KEY, episode_id, payload)
pipe.hdel(PENDING_EPISODES_KEY, episode_id)
await pipe.execute()
```

只有 episode 已成功 upsert 到 Chroma 才调用它，保证画像来源是已确认落库的 episode。即使当前没有注入画像更新器，也会留下画像待办，后续可补处理。

`memory:pending_profiles:v1` 的 field 为 episode ID、value 为脱敏 Episode JSON，无 TTL。`memory:applied_profiles:v1` 则是处理回执：field 为 episode ID、value 为字符串 `"1"`，也没有 TTL。

### 7.3 从摘要中抽取什么 JSON

`memory/profile.py:29～46` 的允许结构为：

```json
{
  "preferences": ["偏好宽松版型"],
  "entities": {
    "products": ["运动鞋"],
    "issue_types": ["退换货"]
  }
}
```

`extract_profile()` 把本次脱敏 `episode.summary` 作为历史数据发给 LLM，默认等待 15 秒。它不重新读取 working memory，不处理保留的最近两轮，也不拿 Redis 累计摘要反复抽取。

解析和清洗规则：只接收完整 JSON 或完整 JSON 代码块；Pydantic 严格类型并禁止额外字段；拒绝重复 JSON 键；模型响应最多 16000 字符；每列表最多 10 项，每项 1～80 字符。NFKC 规范化、strip、casefold 去重；已知敏感模式或身份/密码标签整项丢弃。

### 7.4 怎么合并与保存

`update_from_episode()` 在 `memory/profile.py:285～313` 中依次做：

```text
确认 pending Episode 与输入一致
  → 检查已处理回执
  → 用户锁外调用 LLM 提取 patch
  → 获取用户级锁
  → 再次检查回执
  → 读取最新旧画像
  → 必要时补齐上一份画像来源的回执
  → merge_profile(old, patch)
  → 对合并后的完整画像生成 semantic_text 与 embedding
  → Chroma upsert
  → Redis 写 applied_profiles、删 pending_profiles
```

合并核心是 `merge_profile()` 中的：

```python
def merge(a, b):
    return _safe_items(b + a)[:MAX_ITEMS]
```

新信息优先、旧信息随后、规范化精确去重、最多 10 项；空 patch 不清空旧值。它没有实现语义矛盾判断，所以“喜欢修身”和“现在喜欢宽松”不保证自动互相取代，也没有删除偏好协议。

慢 LLM 在锁外，读最新画像和合并写入在用户锁内，从而避免同用户两个 session 各自拿旧画像覆盖对方增量。Chroma 写线程启动后若协程取消，代码会等线程结束再释放用户锁，避免迟到写入覆盖后续更新。

### 7.5 Chroma 画像 document 与 embedding 分别是什么

每个用户一条，记录 ID 是 `profile:<SHA256(user_id)>`，与 session 无关。document 为 `ProfileRecord.model_dump_json()` 的结果，**是序列化 JSON 字符串**，解析后如下：

```json
{
  "profile": {
    "preferences": ["偏好宽松版型"],
    "entities": {
      "products": ["运动鞋"],
      "issue_types": ["退换货"]
    }
  },
  "last_episode_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "semantic_text": "长期偏好：偏好宽松版型\n关注商品类别：运动鞋\n咨询问题类别：退换货"
}
```

metadata 是扁平字典：

```json
{
  "user_key": "<用户SHA256>",
  "source_episode_id": "<来源episode_id>",
  "source_created_at": "2026-11-23T02:00:00+00:00",
  "updated_at": "2026-11-23T02:05:00+00:00",
  "embedding_id": "siliconflow:BAAI/bge-m3"
}
```

embedding 编码的是清洗并合并后的完整画像的 `semantic_text`，不是 patch，不是整个 JSON，也不是用户哈希或时间戳。`profile_to_embedding_text()` 对每类列表稳定排序后生成三类标签文本。空画像不会创建向量记录，但对应待办仍可被确认。

### 7.6 三种读取能力

| 方法 | 如何读 | 返回内容 | 当前 Chat 调用了吗 |
| --- | --- | --- | --- |
| `get_profile(user_id)` | 按 `profile:<用户哈希>` 精确 `get()` | 一份 `UserProfile`；不生成 query embedding | 没有 |
| `search(user_id, query)` | 对当前问题编码，过滤当前用户，最多一条 | `ProfileMatch` 列表，带相关性分数 | 没有 |
| `find_similar_users(user_id)` | 用当前完整画像编码，过滤其他用户 | 其他用户的画像列表，供显式后台分析 | 没有，也不应当作当前用户记忆注入 |

精确读取无记录时返回空画像；数据库失败、记录损坏时抛异常，避免伪装成空画像覆盖旧数据。向量 search 的服务异常则降级为空列表。

`get_profile()` 优先读当前模型 v2 collection，缺失时可回退配置的旧 collection，默认 `user_profiles_v1`。向量检索只查当前 collection，不混入旧占位向量。迁移使用 `reindex_user()`，重新生成向量而不重跑 LLM；迁移不会删除旧记录。

## 8. 完整历史、state、回执：容易混淆的配套数据

### 8.1 State 的 Redis 数据形状

Redis 类型是 String，里面放序列化 JSON，不是 Redis Hash。`state["intent"]` 的值来自 `IntentResult.to_state()`：

```json
{
  "intent": {
    "intent": "refund",
    "confidence": 0.91,
    "source": "llm",
    "source_scores": {"llm": 0.91},
    "entities": {},
    "reasoning": "用户明确提出退款咨询"
  }
}
```

两层 `intent` 是代码实际设计：外层是 state 的命名空间，内层才是分类值。API 更新外层字段时保留其他 state 字段，并不自动维护所有订单业务实体；这里只展示模型允许的结构。

`ConversationMemory.persist_intent()` 也提供独立加锁保存能力，但当前 Chat 正常路径采用 `state_for_commit` 随完整问答一起写，不能把辅助方法存在误读成 API 每轮提前保存意图。

### 8.2 PostgreSQL 为什么还要留完整历史

Redis 会过期和压缩，不适合直接承担“始终可翻页查看全部消息”的角色。配置 `DATABASE_URL` 后，`api/sessions.py::connect()` 建两张表：

| 表 | 关键列 | 存什么 |
| --- | --- | --- |
| `sessions` | `session_id` 主键、`user_id`、title、创建/更新时间、message_count | 会话目录 |
| `session_messages` | 自增 message_id、session_id、user_id、client_request_id、role、content、status、created_at、metadata JSONB | 每条完整 user 或 assistant 消息 |

一轮问答在 PostgreSQL 中是两行消息，和 Redis 一轮一个 JSON item 不同。assistant 行的 metadata 保存 `{"request_id":"…","state":{…}}`。

`commit_turn()` 在事务中锁定 session 行、验证归属、检查本会话已有回执、插入两行消息、更新 title 和 message_count。回执复用时直接返回旧结果。

相关 HTTP 接口：`GET /sessions`、`GET /sessions/{session_id}/messages` 用它展示历史；`/chat/stream` 用它查成功回执、保存结果。

**目前没有“Redis 缺失 → 从 PostgreSQL 恢复 summary/state/working”的自动重建代码。** 所以页面能看到很久以前的消息，不代表模型这次还收到了这些消息；Chat 甚至在 Redis 记忆服务缺失时仍可继续把新回答保存到 PostgreSQL。

### 8.3 两类 Redis 回执

```text
receipt_v1:<会话复合哈希>
  类型 Hash
  field = client_request_id
  value = Turn JSON

ureceipt_v1:<SHA256(user_id)>
  类型 Hash
  field = client_request_id
  value = {"session_id":"…","turn":{完整Turn}}
```

第二类让“第一次没带 session ID，重试仍没带 session ID”的请求找回原会话。它们仅在传了 `client_request_id` 时随 Turn 写入，设置会话 TTL。TTL 针对整个 Hash，不针对单个 field；用户级回执的新写入可能顺带延长该用户其他旧 field 的存活时间。

API 查到同一 ID 对应不同原始 message 会返回冲突；同消息则回放。回执里保留完整 Turn，工作记忆裁剪后回执并不会同步删除，所以“压缩”不等于全 Redis 原文都被清理。

需要区分：Redis `commit_turn()` 自身不查回执去重，只写回执；检查在 API/上层。PostgreSQL 的唯一索引是 `(user_id,session_id,client_request_id,role)`，不是全用户跨会话唯一约束。它们支持当前路径的顺序重试，但不构成所有并发场景下的全局 exactly-once。

## 9. Python 怎样操作 Redis：从原生命令到项目封装

本节和下一节按仓库固定依赖说明：`redis==5.2.1`、`chromadb==0.5.23`。所有完整示例都应从项目根目录、已装好项目依赖的 Python 环境执行。异步片段需要放入 `async def` 中。

### 9.1 只读查看一个会话，完整可运行示例

保存为临时脚本或交互环境执行；脚本不写业务数据：

```python
import asyncio
import json
import os

import redis.asyncio as redis
from dotenv import load_dotenv

from memory.store import RedisMemoryStore


async def main():
    load_dotenv()
    client = redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=5,
    )
    store = RedisMemoryStore(client)
    user_id, session_id = "demo-user", "demo-session"
    try:
        key = store.working_key(user_id, session_id)
        print("working key:", key)
        print("Redis type:", await client.type(key))
        print("List 长度（轮数）:", await client.llen(key))
        print("TTL:", await client.ttl(key))

        # 底层原始返回：最新在前，每个元素都是 JSON 字符串。
        raws = await client.lrange(key, 0, -1)
        print("raw newest first:", [json.loads(x) for x in raws])

        # 项目封装返回：旧到新，并转换成数据对象。
        snapshot = await store.get_snapshot(user_id, session_id)
        print("history:", snapshot.to_messages())
        print("summary:", snapshot.summary)
        print("state:", snapshot.state)
        print("ttls:", await store.get_ttls(user_id, session_id))

        for pending_key in (
            "memory:pending_episodes:v1",
            "memory:pending_profiles:v1",
            "memory:applied_profiles:v1",
        ):
            print(pending_key, await client.hlen(pending_key))
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
```

`decode_responses=True` 让读取结果直接是 Python str；默认不解码时可能拿到 bytes。`lrange` 的 stop 是包含边界的；`-1` 代表最后一个。`get_ttls()` 中 `-2` 表示 key 不存在，`-1` 表示没有过期时间。

如果宿主机运行 Python，而 `.env` 的 `REDIS_URL` 仍是容器地址 `redis:6379`，要先在当前终端把它改为实际可达的 `127.0.0.1:6379` 地址；密码从已有本地配置读取，不要硬编码到教程脚本。`load_dotenv()` 默认不覆盖已设进程环境变量。

### 9.2 手动保存一轮，用项目封装而非自己拼事务

下面片段会真实写入教学会话，只在明确需要手动验证时执行；传入现有 `store`：

```python
await store.commit_turn(
    "demo-user",
    "demo-session",
    request_id="demo-server-request-001",
    client_request_id="demo-client-request-001",
    user_message="我更喜欢宽松版型",
    assistant_message="后续推荐时会参考您本次说明的版型偏好。",
    state={"intent": {"intent": "other", "source": "fallback"}},
)
```

直接调用 store 重复执行会再追加 Turn，因为 store 层没有先查回执的逻辑；若做业务调用，应走 Chat 的幂等路径。这里也不会触发 maintainer：只有 `ConversationMemory.commit()` 才负责在 store 提交后 `submit()`。

原生 Redis 与封装对应关系：

| 目的 | Redis 命令 / Python 方法 | 项目位置 |
| --- | --- | --- |
| 追加新轮次 | `LPUSH` / `await client.lpush(key,payload)` | `commit_turn()` 事务 |
| 读工作记忆 | `LRANGE` / `await client.lrange(key,0,-1)` | `get_turns()` / `get_snapshot()` |
| 读 state、summary | `GET/MGET` | `get_state()` / `get_summary()` / `get_snapshot()` |
| 写 String 并设置 TTL | `SETEX` | state、summary 更新 |
| 刷新存在的 key | `EXPIRE` | 完整提交、压缩提交 |
| 读回执 | `HGET` | `get_receipt()` / `get_user_receipt()` |
| 写待办、写回执 | `HSET` | 各事务中的登记/确认 |
| 扫待办 | `HSCAN` / `async for ... in client.hscan_iter(key)` | `get_pending_episodes/profiles()` |
| 确认待办 | `HDEL` | `ack_episode()` / `ack_profile()` |
| 条件裁剪 | `WATCH` → `MULTI` → `LTRIM/SETEX/HSET` → `EXEC` | `commit_compaction()` |

HSCAN 是扫描 Hash，而非 FIFO 消费队列，不能假设待办天然按发生时间顺序处理。Redis 事务把命令作为一个隔离执行单元，但不是 SQL 那种任意执行错误均自动回滚的事务；这里依赖受控 key 类型和预先序列化，避免常见部分错误。不得让其他组件把相同 key 写成不同类型。

### 9.3 Redis 键与生命周期总表

| Key | Redis 类型 | 值 | 过期/增长策略 |
| --- | --- | --- | --- |
| `wm_v1:<session_digest>` | List | Turn JSON，每 item 一轮 | 默认 7 天，提交刷新，压缩裁剪 |
| `state_v1:<session_digest>` | String | state JSON | 默认 7 天，写入/提交/压缩刷新 |
| `summary_v1:<session_digest>` | String | 累计摘要纯文本 | 默认 7 天，压缩写入、提交刷新 |
| `receipt_v1:<session_digest>` | Hash | 请求 ID → Turn JSON | 写本会话回执时刷新整个 Hash TTL |
| `ureceipt_v1:<user_digest>` | Hash | 请求 ID → session + Turn JSON | 写用户回执时刷新整个 Hash TTL |
| `memory:pending_episodes:v1` | Hash | episode ID → Episode JSON | 无 TTL，Chroma 成功后 HDEL |
| `memory:pending_profiles:v1` | Hash | episode ID → Episode JSON | 无 TTL，画像成功后 HDEL |
| `memory:applied_profiles:v1` | Hash | episode ID → `"1"` | 无 TTL，当前没有自动清理 |
| `lock:conversation:<session_digest>` | String | 随机持有者 token | 默认租期 120 秒，续租，退出释放 |
| `lock:profile:<user_digest>` | String | 随机持有者 token | 同类租约机制 |

前三类 key 可有可无；不是所有会话都会同时存在所有 key。压缩刷新 state TTL 不会改变 state 内容；也不会同步刷新回执 TTL。

## 10. Python 怎样操作 ChromaDB

### 10.1 建立连接、选择 collection、显式编码向量

`LongTermMemory.from_env()` 做的关键事如下，代码为按实际逻辑整理的等价示意：

```python
import hashlib
import os

import chromadb
from chromadb.config import Settings

from memory.embeddings import embedding_from_env

embedding_id, embed = embedding_from_env()
name = "episodic_" + hashlib.sha256(embedding_id.encode()).hexdigest()[:12]
client = chromadb.HttpClient(
    host=os.getenv("CHROMA_HOST", "localhost"),
    port=int(os.getenv("CHROMA_PORT", "8001")),
    settings=Settings(anonymized_telemetry=False),
)
# 教学展示创建过程；纯查看时使用 get_collection，避免创建空 collection。
collection = client.get_or_create_collection(
    name=name,
    embedding_function=None,
    metadata={"hnsw:space": "cosine", "embedding_id": embedding_id},
)
```

源码还会检查已有 collection metadata 中距离空间和 embedding ID 是否匹配；不匹配就失败，不静默复用。画像 collection 额外检查 `profile_schema="v2"`。

collection 名称根据 embedding 标识确定，不按用户或 session 建 collection；用户隔离靠 metadata 过滤或精确 ID。不要使用 `collection.query(query_texts=...)` 期待这里自动选出同一模型：项目把自动 embedding 关闭，使用 `query_embeddings`。

### 10.2 embedding 是哪里来的

`memory/embeddings.py::embedding_from_env()` 的分支：

| 配置 | 实际编码方式 | embedding_id |
| --- | --- | --- |
| `auto` 且有 `SILICONFLOW_API_KEY` | SiliconFlow HTTP 请求 | `siliconflow:<模型名>`，默认 `BAAI/bge-m3` |
| `auto` 且无该 Key | Chroma 默认本地 embedding function | `chroma:all-MiniLM-L6-v2` |
| `siliconflow` | 强制远程服务，无 Key 报错 | 同上远程格式 |
| `default` | 强制默认本地编码 | 同上本地格式 |

远程核心请求：

```python
response = client.post(self.url, headers={"Authorization": f"Bearer {self.key}"},
                       json={"model": self.model, "input": texts, "encoding_format": "float"})
response.raise_for_status()
data = sorted(response.json()["data"], key=lambda item: item["index"])
```

它校验返回 index 是否与输入一一对应。`encode_vectors()` 随后验证数量、维度一致、非空、无 NaN/Inf、非全零，并转换为 Python float。

需要区分三件事：对话 LLM 生成文本，tokenizer 计数或截短文本，embedding 模型把文本变成检索向量。三者可以使用不同模型，不互相替代。默认本地 embedding 首次使用可能要下载模型；把模型名设成 bge-m3 不会自动把它安装到本地。

切换 embedding 模型会切换 collection，不会自动迁移旧数据；不能把旧模型向量直接拿给新模型查询。失败时没有临时切换后端的降级逻辑。

### 10.3 get 与 query 的返回形状

只读精确取记录：

```python
raw = collection.get(
    ids=[episode_id],
    include=["documents", "metadatas", "embeddings"],
)
```

```text
raw["ids"][0]
raw["documents"][0]
raw["metadatas"][0]
raw["embeddings"][0]   ← 同一条记录的向量
```

向量检索：

```python
from memory.embeddings import encode_vectors
from memory.models import memory_identity
from memory.redaction import redact

raw = collection.query(
    query_embeddings=encode_vectors(embed, [redact("运动鞋退货")]),
    n_results=12,
    where={"$and": [
        {"user_key": {"$eq": memory_identity("demo-user")}},
        {"session_key": {"$ne": memory_identity("demo-new-session")}},
    ]},
    include=["documents", "metadatas", "distances"],
)
```

```text
raw["ids"][0][0]          ← 第一个查询的第一个结果
raw["documents"][0][0]
raw["metadatas"][0][0]
raw["distances"][0][0]
```

`query` 支持多条查询输入，所以多一层 list。业务代码优先调用 `LongTermMemory.search()`，因为它还包含二次隔离校验、阈值、去重、脱敏和总 token 控制。上面的底层示例仅用于解释存储 API，未做完整后处理。异步 API 中调用同步 Chroma/embedding 时要像项目一样放入 `asyncio.to_thread()`。

### 10.4 只读查看当前 Chroma，不触发 embedding

项目已有标准库工具，从项目根目录执行：

```powershell
python -m tooling.inspect_episodes --host 127.0.0.1 --port 8001 --limit 3
```

这条命令只查看 `episodic_` 开头的 collection，不会创建 collection、下载模型或调用 embedding。需要具体画像 collection 时可显式指定实际名称：

```powershell
python -m tooling.inspect_episodes --host 127.0.0.1 --port 8001 --collection user_profiles_v2_实际模型后缀 --limit 1
```

这个通用底层读取工具也能通过显式名称查看画像，虽然其默认说明使用 episode 术语。加 `--embeddings` 可查看完整向量。这里的 `python` 指已经可用的 Python 解释器；若使用虚拟环境，替换为该环境的可执行路径。

直接使用 SDK 只读查看所有 collection 的完整示例：

```python
import json

import chromadb
from chromadb.config import Settings

client = chromadb.HttpClient(
    host="127.0.0.1",
    port=8001,
    settings=Settings(anonymized_telemetry=False),
)
for item in client.list_collections():  # 本项目固定的 Chroma 0.5.23
    if not item.name.startswith(("episodic_", "user_profiles_v2_")):
        continue
    collection = client.get_collection(item.name, embedding_function=None)
    print("collection:", collection.name)
    print("metadata:", collection.metadata)
    print("count:", collection.count())
    raw = collection.get(limit=3, include=["documents", "metadatas"])
    print(json.dumps(raw, ensure_ascii=False, indent=2))
```

不要将这些 SDK 示例直接当成其他大版本的 API 说明；本教程以仓库固定版本与现有调用方式为准。

## 11. 存在服务内存里，还是最终落到磁盘哪里

`docker-compose.yml` 给出的部署结构：

| 组件 | 宿主机访问 | 容器内访问 | 容器持久化目录 | Docker 命名卷 |
| --- | --- | --- | --- | --- |
| Redis | `127.0.0.1:6379` | `redis:6379` | `/data`，启用 `--appendonly yes` | `redis_data` |
| Chroma | `127.0.0.1:8001` | `chromadb:8000` | `/chroma/chroma`，开启持久化 | `chroma_data` |
| PostgreSQL | `127.0.0.1:5432` | `postgres:5432` | `/var/lib/postgresql/data` | `postgres_data` |
| FastAPI | `127.0.0.1:8000` | 容器端口 8000 | Chat 临时 list 与维护器队列在进程内 | 不用于保存向量库 |

Redis 在线数据主要在内存中，Compose 用 AOF 与命名卷提供重启恢复基础。Chroma 通过 HTTP 服务把自身数据持久化到它的 volume；应用客户端并不指定每条向量的物理文件位置。不要直接修改数据库数据目录文件。

`.env.example` 中的 `CHROMA_PERSIST_DIRECTORY=/app/data/chroma` 不是当前 `HttpClient` 调用读取的参数。真实服务目录由 Compose 的 `PERSIST_DIRECTORY` 和 volume 挂载决定，不能据此认为数据存在 FastAPI 容器的 `/app/data/chroma`。

Docker 实际卷名通常还带项目名前缀；用 `docker volume ls` 找实际名字，再 `docker volume inspect 实际卷名`。不要把文档中的逻辑卷名当作 Windows 工作区下的文件夹路径。

## 12. 跟着做：从两轮聊天观察记忆

### 12.1 不改默认配置的观察步骤

前提：应用和依赖已启动、LLM 已正确配置、记忆开关已打开。如果仅想读代码，可以跳过实际请求。

1. 调用 `/chat/stream`，用 `demo-user`、`demo-session`、请求 ID `demo-request-001` 发送“我更喜欢宽松版型”。
2. 等待 done，再用同 user/session、新请求 ID `demo-request-002` 问“我刚才说喜欢什么版型？”。
3. 用第 9.1 节脚本读 Redis，应看到两个完整 Turn，顺序为第一轮、第二轮。
4. 用原请求 ID 重试第一条原始 message，应回放原结果，正常顺序请求不会再追加一轮。
5. 换成 `demo-other-session` 问同样的问题，Redis 历史为空；当前 Chat 不会自动通过 Chroma 回忆前一个 session。

两轮请求通常不会触发默认 10800 token 阈值；即使人为降低阈值，只有两轮也不会压缩。要在隔离开发环境演示压缩，需至少三轮；想默认生成 episode，则至少四轮且实际被压缩段达到两轮。异步调度下不能只根据总共发送了几轮断言段的大小，应检查当次快照和结果。

### 12.2 开发调试接口

设置 `COMMERCEMIND_MEMORY_DEBUG=true` 并重启应用后，可调用：

```text
GET /memory/demo-session?user_id=demo-user
```

响应包含 `turn_count`、`turns`、`summary`、`state` 和三类 TTL；这是只读视图，不触发摘要维护。关闭 debug 时路由根本不注册，404 是预期行为。接口只反映 Redis，不能据此判断 Chroma 或 PostgreSQL 数据数量。

### 12.3 如何判断是哪一层没生效

| 现象 | 优先检查 |
| --- | --- |
| 第二轮不记得第一轮 | user/session 是否相同；是否收到 done；Redis working 是否实际存在；是否发生读/写降级 |
| 页面有历史但模型不记得 | PostgreSQL 历史展示与 Redis 模型上下文是两条链路；Redis 过期不会自动重建 |
| working 一直变长 | 是否达到阈值、是否超过两轮、维护器是否启动；队列是否 full；LLM 摘要是否失败 |
| working 少了、summary 多了 | 通常是后台压缩生效，非历史丢失；完整历史仍看 PostgreSQL（若配置） |
| Chroma 健康却没有 episode | 当前默认启动未注入 longterm；看 Redis `pending_episodes` 是否积压 |
| episode 有了但没有画像 | 是否注入 ProfileUpdater；是否有 pending_profiles；抽取是否合法；画像是否为空 |
| 换模型后查不到旧情景 | collection 按 embedding_id 隔离，旧向量没有自动迁移 |
| summary 有了但无新 episode | 被压缩段可能只有一轮，未达到默认最少两轮 |
| done 后仍无新增记忆 | Redis 提交异常被捕获；若有 PG，回答可能已存 PG；查应用错误日志 |

## 13. 可靠性边界：按实际代码理解，不扩大保证

### 13.1 当前会话锁在提交前已经退出

这是本次代码阅读中最需要注意的实现问题，定位 `api/main.py:579～670`。按当前缩进关系，可抽象成：

```python
# 当前结构示意，不是修复后的代码。
async with memory.conversation(req.user_id, session_id):
    # 检查回执、读上下文、识别意图、流式生成。
    pass

# 这里已经退出 async with，会话锁已经释放。
if should_commit and sessions is not None:
    # PostgreSQL commit。
    pass
if should_commit and memory is not None:
    await memory.commit(
        req.user_id, session_id,
        request_id=active_request_id,
        question=req.message,
        answer=text,
        client_request_id=req.client_request_id,
        lock_held=True,
        state=state_for_commit,
    )
```

`lock_held=True` 告诉 store 不再获取锁，但外层锁此时实际已经退出。另一个请求可能在“本轮已生成、尚未提交”的间隙进入，读取缺少本轮的新历史；同请求 ID 的并发重试也可能重复生成甚至重复追加 Redis Turn。

所以 `ConversationMemory.conversation()` 的“整轮锁”是接口意图，当前路由实现没有完全兑现。建议后续修复时把读、生成、持久提交保持在同一锁作用域内，并加并发 API 回归测试。仅把 `lock_held` 改 false 不能解决读生成提交之间的整个竞态窗口。本教程只解释与记录问题，没有顺手修改业务代码。

### 13.2 跨库不具有一个大事务

| 故障点 | 当前行为 | 不能推导出的保证 |
| --- | --- | --- |
| LLM 流式中断/报错 | 未完成正常提交阶段，不保存本轮 Turn/state | 浏览器已显示部分文本不代表已落库 |
| Redis 上下文读失败 | 尝试单读 state，回答降级为无历史 | 不会自动从 PG/Chroma 恢复上下文 |
| PG 提交失败 | 发 error，不继续正常 Redis 提交流程 | 不能宣称此时 done 成功 |
| PG 成功、Redis 失败 | 记录错误，仍发 done | Redis 不会自动补回；PG 回执命中也直接回放 |
| Redis 压缩生成失败 | 不裁剪旧原文，维护器可重试 | 不会通过截掉消息强行满足预算 |
| Redis 压缩成功、Chroma 失败 | 只有显式接入发送器后才会尝试发送；失败保留 episode 待办 | 两库不是原子提交，待办需有人处理 |
| episode 写入成功、画像失败 | 已存 episode 保留，画像待办保留 | 不回滚 summary、裁剪或 episode |
| 客户端在提交附近断开 | 可能取消，也可能已有 PG/Redis 写入 | 不能把所有“用户没收到 done”都当成“未写库” |

Chat 在生成后检查过一次断连，但不是把后续存储包在可整体回滚的事务里；时序竞态仍需幂等协议处理。

### 13.3 其他值得补齐的地方

- **长期链路默认断开**：仅配环境变量、启动 Chroma、开启健康检查，都不会自动创建 longterm/profile 实例。
- **持久化待办会积压**：默认压缩能登记 episode，却没有默认消费者；无 TTL 的 Hash 需监控数量和处理失败率。
- **上下文缺少统一硬预算**：10800 只管 working 的后台触发，不覆盖 system、Skill、累计 summary、当前问题和未来检索内容。
- **压缩不等于清除所有原文**：Redis 回执、PostgreSQL 历史仍可保留完整问答；原始 working/state/summary 也不走统一脱敏。
- **删除会话没有级联清理全部记忆**：当前 `delete_session()` 删除 PostgreSQL session 并通过外键清消息；没有同步删 Redis、Chroma episode、画像或待办。后续应明确数据删除语义。
- **健康状态不是功能接通证明**：health 会单独尝试 Chroma heartbeat；启动 `ConversationMemory.start()` 本身先 ping Redis，并未按 `.env.example` 注释所暗示的那样强制连接 Chroma。`required=true` 不等于当前实现已经验证了 Chroma 可用。
- **用户隔离不是认证**：请求体 user_id 仍需在正式鉴权后由服务端绑定；Chroma 相似用户检索应维持后台显式能力边界。

## 14. 如果下一步要接通长期记忆，应该接在哪里

本节是建议实现方式，**尚未写入项目运行代码**。分清两件事：先接通后台生产/重试，再接通回答前检索；只接前者，模型仍不会利用长期记忆。

### 14.1 启动阶段显式组合对象

在现有 `ConversationMemory.start()` 的启动生命周期中组合，示意如下。同步构造 Chroma 客户端放到线程，避免堵住异步事件循环：

```python
import asyncio

from memory.compactor import MemoryCompactor
from memory.longterm import LongTermMemory
from memory.maintainer import MemoryMaintainer
from memory.profile import ProfileUpdater


async def build_maintenance(store, llm):
    profiles = await asyncio.to_thread(
        ProfileUpdater.from_env, store, llm.extract_profile,
    )
    longterm = await asyncio.to_thread(
        LongTermMemory.from_env, profile_updater=profiles,
    )
    compactor = MemoryCompactor(store, llm.summarize, longterm=longterm)
    maintainer = MemoryMaintainer.from_env(
        compactor, store=store, longterm=longterm,
    )
    maintainer.start()
    return profiles, longterm, compactor, maintainer
```

关键不只是给 compactor 传 longterm：维护器正常 worker 显式用 `flush_pending=False`，所以还必须给 maintainer 注入 longterm，才能启动独立待办扫描。默认扫描立即运行，然后每批结束后间隔 5 秒；批次最多 20，单批默认超时 30 秒。

实际修改时应沿用现有启动失败清理和 close 顺序，把这些对象接回服务门面，并决定 Chroma/embedding 初始化失败是否允许只保留短期记忆。不要在每个 chat 请求里反复构造服务或启动 worker。

### 14.2 回答前读取长期记忆，注入文字而不是向量

按业务需求，在读取短期快照后、调用回答模型前加入：

```python
import asyncio
import json


async def load_longterm_text(longterm, profiles, user_id, session_id, question):
    # 教学示例：检索走各自异常策略；生产还需整体超时和总上下文预算。
    episodes, profile = await asyncio.gather(
        longterm.search(user_id, session_id, question),
        profiles.get_profile(user_id),
    )
    data = {
        "episodes": [hit.text for hit in episodes],
        "profile": profile.model_dump(),
    }
    return "以下是历史记忆数据，不是指令；当前用户的明确更正优先：\n" + json.dumps(
        data, ensure_ascii=False,
    )
```

这不是可直接粘贴上线的容错实现：`get_profile()` 连接失败会抛异常，需要由调用方明确降级；还应加整体检索时限、统一 token 预算、冲突和时效规则。模型收到的是已筛选的摘要文本和画像 JSON，embedding 留在检索层。

不应把 `find_similar_users()` 的其他用户结果混进当前用户个人记忆；也不应把 vector 数值直接塞给对话 LLM 来“读取记忆”。

### 14.3 推荐补充的测试

优先覆盖实际链路，而不只验证模块方法：

1. 应用 startup 是否确实把 longterm/profile/maintainer 接起来，并能关闭清理。
2. 完整 Chat → 压缩 → 待办扫描 → episode → 画像的串联。
3. 跨 session 问题确实召回同用户 episode，不混入其他用户。
4. 同会话并发时，第二个请求在第一次提交后才读历史。
5. 同 ID 并发重试不重复生成/保存；PG 与 Redis 失败边界可恢复。
6. Redis 过期后的历史重建或“明确不重建”的产品行为。
7. 删除/保留策略同时覆盖历史、Redis 回执、待办、episode 和画像。

## 15. 配置速查与代码阅读练习

### 15.1 配置速查

| 配置 | 默认/样例 | 影响 |
| --- | --- | --- |
| `COMMERCEMIND_MEMORY_ENABLED` | 代码默认 false，`.env.example` 为 true | 是否自动启动 Redis 记忆服务 |
| `COMMERCEMIND_MEMORY_REQUIRED` | false | 记忆初始化失败是否让应用启动失败 |
| `COMMERCEMIND_MEMORY_TTL_SECONDS` | 604800 | 会话 Redis key TTL |
| `COMMERCEMIND_MEMORY_COMPACT_TRIGGER_TOKENS` | 10800 | working 后台压缩触发 |
| `COMMERCEMIND_MEMORY_MAINTAIN_MAX_PENDING` | 128 | 包含运行中会话的任务容量 |
| `COMMERCEMIND_MEMORY_MAINTAIN_WORKERS` | 2 | 固定压缩 worker 数 |
| `COMMERCEMIND_MEMORY_MAINTAIN_MAX_RETRIES` | 2 | 初次之后的最多重试次数 |
| `COMMERCEMIND_MEMORY_MAINTAIN_PENDING_INTERVAL_S` | 5 | 显式接入 longterm 后的待办批次间隔 |
| `COMMERCEMIND_MEMORY_EPISODE_MIN_TURNS` | 2 | 被压缩段至少几轮才形成 episode |
| `COMMERCEMIND_MEMORY_RETRIEVAL_TOP_K` | 3 | 情景召回最大条数 |
| `COMMERCEMIND_MEMORY_RETRIEVAL_MIN_SCORE` | 0.55 | cosine 相似度筛选下限 |
| `COMMERCEMIND_MEMORY_EPISODE_MAX_TOKENS` | 600 | 所有情景召回正文拼接后的总预算 |
| `COMMERCEMIND_MEMORY_EMBEDDING_PROVIDER` | auto | 本地/远程编码后端选择 |
| `COMMERCEMIND_MEMORY_EMBEDDING_MODEL` | 远程分支默认 BAAI/bge-m3 | 远程 embedding 模型 |
| `COMMERCEMIND_MEMORY_PROFILE_SOURCE_COLLECTION` | user_profiles_v1 | 画像精确读取回退与显式迁移来源 |
| `COMMERCEMIND_MEMORY_DEBUG` | false | 是否注册 Redis 只读调试接口 |
| `COMMERCEMIND_INTENT_ENABLED` | false | 是否识别意图并更新本轮 state |
| `DATABASE_URL` | 无则不启动 SessionRepository | PostgreSQL 完整历史与持久回执 |

注意阈值、检索、画像、维护参数有些由各模块直接 `os.getenv()` 读取，并非全部集中在 `MemorySettings`。看配置对象时不能以为已经看全所有记忆配置。

### 15.2 用现有测试印证理解

以下是代码阅读索引；本教程没有把“存在测试”当作“当前环境已全部运行通过”。

| 想验证的行为 | 现有测试 |
| --- | --- |
| 第二轮 history 包含第一轮 | `tests/test_chat_memory.py::test_second_turn_reads_first_turn` |
| 意图与成功问答一起保存 | `test_chat_stream_persists_intent_atomically_with_successful_turn` |
| 摘要在 system、不在 messages | `test_summary_is_injected_into_system_not_messages` |
| 读失败无历史降级、写失败仍回答 | `test_read_failure_degrades_to_no_history` / `test_commit_failure_still_returns_answer` |
| 一轮一个 List item、顺序、TTL | `tests/test_memory_store.py` |
| 30 轮也不在读取阶段偷偷裁剪 | `tests/test_memory_compaction.py::test_30_turns_all_in_context_no_hidden_limit` |
| 摘要生成期间新增问答被保留 | `test_background_append_during_summary_keeps_new_turn` |
| 两个压缩器仅一个成功提交 | `test_parallel_compactors_only_one_applies` |
| episode 用新增摘要，而非累计摘要 | `tests/test_memory_longterm.py::test_compaction_outbox_retry_uses_new_summary_not_merged` |
| 失败重试不重新摘要 | `tests/test_memory_maintainer.py::test_outbox_pipeline_retry_does_not_regenerate_summary` |
| 画像向量来自合并后完整画像 | `tests/test_memory_profile.py::test_embedding_uses_merged_profile_not_patch_or_metadata` |
| 个人检索与相似用户分析分离 | `test_personal_search_and_similar_users_are_separate_and_defensive` |

安装好开发依赖后，可在项目根目录运行下面的非真实服务测试；环境变量只对当前终端设置：

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
python -m pytest -p pytest_asyncio.plugin -q tests/test_chat_memory.py tests/test_memory_store.py tests/test_memory_compaction.py tests/test_memory_longterm.py tests/test_memory_profile.py tests/test_memory_maintainer.py tests/test_memory_tokens.py tests/test_memory_budget.py
```

真实 Chroma 验证默认需要显式开启 `CHROMA_INTEGRATION=1`；阅读测试代码后再决定是否连接实际服务。默认单测主要使用 fakeredis、替身 LLM 和 fake embedding。

### 15.3 三个自测问题

**问题一：已经保存了用户画像，为什么下一轮模型还不知道偏好？** 先检查是否只调用了画像写入，没有在 Chat 读取和注入；当前默认 API 正是这种未接通状态，光有 collection 不会让 LLM 自动读取数据库。

**问题二：压缩后 working 只剩两轮，为什么 Redis 里还能找到很早的原文？** 回执中保存完整 Turn，裁剪 working 不会同步裁剪回执；此外 PostgreSQL 本来就是完整历史。

**问题三：改成另一个 embedding 模型后，为什么 collection count 看似归零？** collection 名称取决于 embedding_id，实际选择了另一个命名空间；旧数据仍在旧 collection，需要显式迁移或重建向量。

## 16. 延伸阅读与本次核对范围

项目已有专题说明：`docs/episodes.md`、`docs/profiles.md`、`docs/maintenance.md`、`docs/episode-storage-example.json`。本文补足它们与当前流式 API 的实际连接关系；如果说明与当前代码不同，优先检查 `ConversationMemory.start()`、`chat_stream()` 及真实部署版本。

本次通过静态读取调用链、数据模型、存储命令、Compose 配置和现有测试核对教程；没有连接真实 Redis、Chroma、PostgreSQL 或真实模型，也没有自动插入演示数据。运行时是否已部署同一代码版本、数据库里是否已有历史数据，需要按第 9～12 节现场验证。

本次实际完成的本地核对：25 段 Python 示例/节选均通过编译语法检查，7 段 JSON 均可解析；调用真实 `Turn` / `MemorySnapshot` / `Episode` 纯数据模型，验证了六轮序列化还原、消息展开、四轮压缩段、稳定 episode ID 和邮箱脱敏；通过 AST 确认默认启动未注入 longterm，并核对引用的测试函数存在。当前可用 Python 环境缺少 pytest、redis、fakeredis、chromadb 等项目依赖，因此未运行完整项目测试；Mermaid 源码已提供，但本机未安装 Mermaid 渲染器，未声称已渲染验收。
