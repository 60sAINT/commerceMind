# 用户画像：episode 后提取、校验、合并

画像是每个用户的一份结构化长期信息，跨 session 共用。它与“某次发生了什么”的 episode 分开保存；目前没有接入 `/chat`。

## 使用方式

在应用启动层加载环境变量后创建工具，注入已有的 LLM 客户端：

```python
from memory.profile import ProfileUpdater
from memory.longterm import LongTermMemory
from memory.compactor import MemoryCompactor

profiles = ProfileUpdater.from_env(store, llm.extract_profile)
longterm = LongTermMemory.from_env(profile_updater=profiles)
compactor = MemoryCompactor(store, llm.summarize, longterm=longterm)

# 成功问答提交之后，由调用方启动已有后台维护：
compactor.start(user_id, session_id)

# 将来在组装回答上下文时按用户读取：
profile = await profiles.get_profile(user_id)
profile_data = profile.model_dump()

# 维护器或人工重试，不要求再产生一个新 episode：
await longterm.flush_pending(store)  # 重试 episode，也会处理已有画像待办
await profiles.flush_pending()     # 仅重试画像
```

这些是明确的接入示例，不表示当前 API 已自动调用。LLM 与 Redis 连接由调用方拥有、关闭；ProfileUpdater 不单独创建后台调度器。未注入 updater 时，成功 episode 仍登记画像待办，留给之后处理。每批默认最多 20 条。

## 何时更新

```text
working memory 达阈值
  → 摘要 + Redis 条件提交（最近两轮不参与）
  → episode 待办
  → episode 成功 upsert 到 Chroma
  → Redis 同一事务：删除 episode 待办、登记画像待办
  → LLM 从本次脱敏 episode 提取 JSON 增量
  → Pydantic 校验 + 敏感项过滤
  → 用户级锁内读取最新画像、合并、生成真实向量、保存
  → Redis 同一事务：记录处理回执、删除画像待办
```

没产生 episode、不足最少轮次、压缩失败/冲突或 episode 写失败，都不会登记画像待办。画像不重新读 working memory，不处理保留的最近四条消息，也不拿累计 summary 反复抽取。画像失败不回滚已存 episode、summary 或历史裁剪。

## 文件与方法

| 模块 / 能力 | 输入 | 输出 / 下游 |
| --- | --- | --- |
| `profile.py`：`UserProfile` / `Entities` | 偏好、商品类别、问题类别 | Pydantic 数据结构，不连接数据库 |
| `parse_profile(raw)` | 模型响应字符串 | 清洗后的 UserProfile；非法格式抛错 |
| `sanitize_profile(profile)` | 画像对象 | 去空白、规范化、去重、过滤已知敏感项后的新对象 |
| `profile_to_embedding_text(profile)` | 合并清洗后的画像 | 固定顺序的语义文本，不含身份、时间及来源编号 |
| `merge_profile(old, patch)` | 旧画像、合法增量 | 有界、去重的新画像；纯函数，不调用 LLM |
| `ProfileUpdater.from_env(store, extract)` | Redis store、异步抽取函数及 Chroma 环境变量 | 连接按模型区分的 `user_profiles_v2_<模型哈希>` collection 的工具对象 |
| `extract_profile(episode)` | 本次 episode | LLM 提取并校验的增量；默认超时 15 秒；自己不写库 |
| `update_from_episode(episode)` | 已登记待办的 episode | `updated / unchanged / already_applied / not_pending / failed` |
| `get_profile(user_id)` | 原始用户 ID | 按哈希 ID 精确读取；无记录返回空画像；坏数据/连接失败抛错，不假装为空覆盖 |
| `flush_pending(limit=20)` | 可选批次大小 | 完成的任务数量；失败保留待办，不妨碍其他任务 |
| `store.py`：`ack_episode(..., profile_episode=...)` | 成功写入的 episode | 原子交接两个 Redis Hash |
| `profile_lock(user_key)` | 用户 SHA-256 | 进程内锁 + Redis 租约保护“读最新画像→合并→写回” |
| `get_pending_profile(s)`、`profile_applied`、`ack_profile` | episode ID / 批次 | 读取待办、判断回执、原子确认任务 |
| `core/llm.py`：`LLMClient.extract_profile(prompt)` | 抽取提示词 | 原始 JSON 文本；独立 system 指令，模型输出截断时拒绝 |

## 校验与合并规则

- 只接受完整 JSON 对象或完整的一层 JSON 代码块；拒绝额外字段、重复 JSON 字段、错误类型及超大响应（超过 16,000 字符）。
- 每个列表最多 10 项，每项 1～80 字符；模型增量超限直接失败，不把非法半成品写入。
- NFKC 规范化、去首尾空白，按 casefold 精确去重；不进行语义聚类。
- 新条目及本次再次确认的条目优先，然后保留旧条目；合并后最多 10 项。
- 空增量不清空已有字段；相同增量直接重复合并，结果不变。
- 没有实现语义矛盾识别或删除偏好的指令协议。模型只允许提取明确支持的信息，但摘要本身若已误写，后续抽取不能保证恢复事实来源。

## 存储结构

Chroma collection：`user_profiles_v2_` 加 embedding ID 的 SHA-256 前 12 位，collection metadata 包含 `profile_schema=v2`、`embedding_id` 和 `hnsw:space=cosine`。旧的 `user_profiles_v1` 不修改。每个用户一条，记录 ID 为 `profile:<用户SHA-256>`，与 session 无关。document 是如下 JSON 字符串：

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

metadata 包含 `user_key`、`source_episode_id`、`source_created_at`、`updated_at` 和 `embedding_id`，不保存原始用户 ID。embedding 是 `semantic_text` 经模型编码得到的真实浮点向量；不是 `[1.0]` 占位，也不是 JSON 字符的 token ID。document 保存完整结构化画像，semantic_text 只是确定性的检索表示，不用 LLM 另写一篇描述。

## 什么内容向量化，何时生成

`profile_to_embedding_text(profile)` 输入**本次合并完成且清洗后的完整画像**，将三个字段分别排序后拼成带标签的文本。例如：

```text
长期偏好：偏好宽松版型；偏好棉质
关注商品类别：运动鞋
咨询问题类别：退换货
```

不包含用户 ID/哈希、session、episode ID、时间戳、处理回执等身份和运维信息。敏感候选在此之前已整项过滤；空画像不生成向量、不创建记录，但任务可以正常确认。

更新链路为：LLM 提取增量 → Python 合并最新旧画像 → 清洗 → 拼 semantic_text → embedding 编码 → 同一次 Chroma upsert 保存 JSON、metadata、向量 → Redis 确认。embedding 失败或向量为空、全零、NaN 等会在写库前失败，旧记录和待办保留。此次仍从 episode.summary 抽取，没有改成裁剪后的 working memory。

`memory/embeddings.py` 统一供画像和 episode 使用，配置沿用 `COMMERCEMIND_MEMORY_EMBEDDING_PROVIDER/MODEL/URL` 及 `SILICONFLOW_API_KEY`：

- `default`：本地 Chroma 默认 embedding，无服务 API 费，首次使用需要下载模型。
- `siliconflow`：远程模型 API，默认模型名是 `BAAI/bge-m3`，写画像和向量查询均可能产生相应服务费用。
- `auto`：有 Key 选择远程，无 Key 选择默认本地模型；运行失败不临时切换。仅仅配置模型名字不会安装本地 bge-m3。

这次不修改真实 `.env`，不自动下载模型或批量重建生产画像。返回维度取决于模型；各模型有自己的输入长度限制。当前单向量包含偏好与咨询类别，表达的是综合画像，不保证等同于纯“审美品味”。

## 三种读取方式与上下游

| 方法 | 输入 → 输出 | 是否调用 embedding / 边界 |
| --- | --- | --- |
| `get_profile(user_id)` | 用户 ID → UserProfile | 精确读取，不调用；适合回答前加载当前用户完整画像 |
| `search(user_id, query, min_score=.55)` | 用户 ID、问题 → 最多一条 ProfileMatch | 问题脱敏并编码；只查询当前用户，低相关返回空列表 |
| `find_similar_users(user_id, top_k=3, min_score=.55)` | 当前用户 → 其他用户的 ProfileMatch 列表 | 当前完整画像编码查询；排除自己，属于显式跨用户后台分析能力 |

`ProfileMatch` 包含 `user_key`、`profile`、`score`。分数为 cosine 的 `1-distance`，不是概率；检索服务失败返回空列表，非法 top-k/阈值会报错。代码还会核对返回用户、记录 ID 和模型，防止查询返回不符合筛选条件的数据。

```python
# 不需要相关性筛选时，仍然直接读取，零 embedding 调用。
profile = await profiles.get_profile(user_id)

# 个人记忆按当前问题检索，只返回当前用户画像。
hits = await profiles.search(user_id, "推荐一双适合我的运动鞋")
profile_texts = [hit.profile.model_dump() for hit in hits]

# 可信后台业务显式调用；没有开放 HTTP 接口，也不自动传给聊天用户。
peers = await profiles.find_similar_users(user_id, top_k=3)
```

这些接口尚未接入 `ContextBuilder` / `/chat`。后续放进 context 的是经过校验的 profile 文字或 JSON，不是 embedding；相似用户的画像不能当成当前用户的个人记忆注入。

## 旧的一维记录和切换模型后怎么办

旧 collection 的一维向量不能混写新维度，因此新建模型隔离的 v2 collection。精确读取优先查当前 v2，缺失时可读配置的来源 collection（默认 `user_profiles_v1`）；向量检索只查询当前模型的新 collection，不会把旧占位向量带进来。

显式迁移一位用户：

```python
migrated = await profiles.reindex_user(user_id)
```

它在用户锁内读取旧画像、重新清洗、拼 semantic_text、用当前模型编码并保存到新 collection；不调用 LLM、不重新抽取、不删除旧数据，不修改 episode 回执。当前 collection 已有该用户则跳过，防止旧档案覆盖新档案。迁移失败抛错，旧记录保留；返回 True 表示迁移，False 表示无来源/空画像/已有目标。

切换 embedding 模型后，可在启动前把 `COMMERCEMIND_MEMORY_PROFILE_SOURCE_COLLECTION` 设为上一个模型的 collection 名，再为需要的用户调用此方法。没有迁移的旧用户仍可精确读取，但不会出现在新模型的相似度结果里；后续有新的 episode 更新时，会读旧画像合并后写入新模型 collection。已处理 episode 的回执不会因为切换模型而失效，因此不能靠重放已处理任务来代替迁移。

Redis 新增两个无 TTL 的 Hash：

- `memory:pending_profiles:v1`：field 为 episode ID，value 为脱敏 Episode JSON，便于失败重试。
- `memory:applied_profiles:v1`：field 为已处理 episode ID，value 为 `1`，防止已完成任务因重复交接而再次处理。

回执会随已处理 episode 数量增长；本步不做自动清理，未来用户删除/数据保留策略需要同时覆盖它们。

## 并发、取消与失败

LLM 抽取在用户锁外；锁内再读取最新画像，保证同一用户两个 session 的增量不会基于各自旧快照覆盖。不同用户使用不同锁。Chroma 写线程启动后，协程取消会等待该线程结束再释放锁，避免线程在锁释放后继续执行迟到写入。

Chroma 与 Redis 不是跨库事务。Chroma document 保存最后来源 ID；如果写成功后 Redis 确认失败，重试可据此补回执。覆盖下一份画像之前，也先补齐上一份来源的回执，再写新画像。所有画像写入方必须走同一锁协议；不宣称数据库具备跨库 exactly-once 或 Chroma 自带 CAS。

LLM、解析、校验、读取、写入失败只记录异常类型，不记录模型原文或异常详情；旧画像和待办保留。取消继续向上传播。整个 compactor 现有的 episode flush 等待上限仍为 15 秒，可能中止尚未完成的画像抽取；其待办保留，后续独立调用 `profiles.flush_pending()` 可重试。

## 敏感信息边界

复用现有手机号、邮箱、身份证号规则；画像值做 NFKC 后再次检查，含这些原值或占位符的整项丢弃。同时禁止身份字段，过滤含姓名、地址、银行卡、密码等标签及常见 `sk-` 密钥形式的候选项。抽取前和落库前均处理，日志不打印内容。

这是已知模式和白名单 schema 的防线，不是对所有姓名、地址、国际电话号码或混淆字符串的全量 PII 识别。Redis 原始 working memory、会话 summary 的既有保存策略不在本次修改范围内；“敏感信息不落库”的验收针对新增画像及其待办、元数据和日志。

## 测试

```bash
.venv/bin/python -m pytest -q tests/test_memory_profile.py
# 可选：临时 collection 验证真实 Chroma，结束会删除；不调用真实 LLM/embedding API。
CHROMA_INTEGRATION=1 CHROMA_HOST=127.0.0.1 CHROMA_PORT=8001 \
  .venv/bin/python -m pytest -q tests/test_memory_profile.py -k real_profile
```
