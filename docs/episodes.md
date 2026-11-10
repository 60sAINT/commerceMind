# ChromaDB 情景记忆使用说明

## 这次增加的能力

- `models.py` 的 `Episode` 是一次压缩段的摘要记录，包含稳定 ID、用户/会话哈希、起止轮次哈希、轮数、摘要和时间。
- `redaction.py` 的 `redact(text)` 输入文本，输出替换手机号、邮箱、身份证号后的文本。规则脱敏并非完整 PII 检测，不保证覆盖姓名、地址等所有敏感信息。
- `longterm.py` 的 `LongTermMemory.add_episode(episode)` 写入脱敏摘要及向量；`search(user_id, session_id, query)` 返回带相关性分数的 `EpisodeMatch` 列表；`flush_pending(store)` 重试有限批次的待写记录。
- `tokens.py` 增加独立文本计数/截断，用已有官方 tokenizer 控制检索文本总量，不套 working memory 消息模板。

只保存本次被压缩旧消息的“新增摘要”，不保存原文、不把累计 summary 重复作为 episode。默认压缩段至少两轮才形成 episode；最近两轮不参与摘要，规则不变。

## 写入和失败恢复

```text
旧消息 → LLM 新增摘要
           ├→ 与旧 summary 合并 → Redis summary
           └→ 规则脱敏 → Redis 待写 episode → embedding → ChromaDB
```

待写 episode 与 Redis 摘要更新、旧消息裁剪在同一事务中提交到 Hash `memory:pending_episodes:v1`，field 为 episode ID，value 为脱敏后的 Episode JSON；无 TTL，避免向量库故障期间自动过期丢失。

ChromaDB 写入成功才删除待写记录。使用稳定 episode ID 和 upsert，写成功但尚未确认就崩溃时，重试不会新增重复记录。只有 Redis 压缩条件提交成功才产生待写 episode；摘要失败或并发冲突不会产生。默认同一段的起止轮次/身份决定稳定 ID，不是按摘要随机生成。

接入现有 compactor 的方式（放在异步函数内）：

```python
from memory.longterm import LongTermMemory
from memory.compactor import MemoryCompactor

longterm = LongTermMemory.from_env()  # 初始化应在启动阶段完成
compactor = MemoryCompactor(store, llm.summarize, longterm=longterm)
# 一轮成功落库后：
compactor.start(user_id, session_id)
# 服务恢复后也可以显式重试，无需再生成摘要：
await longterm.flush_pending(store)
# 查询同一用户、其他会话中的相关记忆：
hits = await longterm.search(user_id, session_id, "运动鞋怎么退货？")
texts = [hit.text for hit in hits]
```

不注入 longterm 时，压缩仍会保存待写记录，但不会即时写 ChromaDB。完整的自动重试调度和 `/chat` 多轮/长期记忆接入仍是后续步骤；仅完成这些代码，不代表运行中的旧版应用已经自动产生情景记忆。改代码后也需要更新运行实例。

## embedding 和检索规则

- `auto`：有 `SILICONFLOW_API_KEY` 时使用已配置的 SiliconFlow embedding，默认 `BAAI/bge-m3`；没有 Key 则使用 Chroma 默认 embedding。默认后端第一次使用可能需要下载本地模型。
- `COMMERCEMIND_MEMORY_EMBEDDING_PROVIDER` 可显式设为 `siliconflow` 或 `default`。不是 `ANTHROPIC_API_KEY`，也不是对话模型的 tokenizer。
- 一个 collection 固定一套 embedding，名称为 `episodic_` 加 embedding 标识 SHA-256 前 12 位。两种后端使用不同 collection；请求失败不会临时切换模型查询旧向量。
- 切换 embedding 后旧 collection 仍保留，但不会被新后端自动检索；需要单独迁移/重新生成向量，不能把不同模型的向量直接混用。
- collection 使用 cosine 距离，检索分数为 `1 - distance`，默认分数不低于 0.55；这是检索阈值，不是正确率或概率，需要后续用真实语料标定。
- 按用户哈希过滤，排除当前 session；多取候选后排序、阈值过滤、按 ID/脱敏文本去重，最多返回 3 条。
- 返回的文本以空行连接，总量默认不超过 600 tokens。截断只影响返回副本，不改 Chroma 中的摘要。查询问题也先脱敏再生成向量。
- 向量调用和 Chroma 同步调用放在工作线程，避免直接阻塞事件循环。检索失败返回空列表；写入失败保留 Redis 待写记录。

## 怎么查看 ChromaDB 里是否有情景记忆

项目根目录执行以下只读命令，不加载 Chroma SDK、tokenizer 或 embedding：

```bash
.venv/bin/python -m tooling.inspect_episodes --host 127.0.0.1 --port 8001
```

输出解释：

- `collections: []`：默认租户/数据库下还没有 `episodic_` 开头的 collection。
- 某个 collection 的 `count: 0`：collection 已创建，但还没有记录。
- `count > 0`：有已落库的 episode；`sample.ids/documents/metadatas` 展示前 3 条。

若 Chroma 没有记录，也可以检查 Redis 是否有尚未写入向量库的 episode（只读数量）：

```bash
docker compose exec redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli HLEN memory:pending_episodes:v1'
```

数量大于 0 时可在应用中调用 `await longterm.flush_pending(store)` 重试，并检查 embedding 配置和 Chroma 连通性。

需要查看向量或指定 collection：

```bash
.venv/bin/python -m tooling.inspect_episodes --host 127.0.0.1 --port 8001 --embeddings --limit 1
.venv/bin/python -m tooling.inspect_episodes --host 127.0.0.1 --port 8001 --collection episodic_实际后缀 --limit 5
```

此工具面向项目固定的 Chroma 0.5.23 REST v1 API。宿主机访问 8001，容器之间访问 `chromadb:8000`。它不自动加载 `.env`，命令中的 host/port 优先于进程环境变量，也不会创建 collection。

## 手动写一条可辨认的真实示例

以下命令会真实写入一条 `demo-user` 的脱敏 episode，可能调用配置的 embedding 服务；这是可选手动验证，不会自动运行。要从宿主机使用容器端口，显式覆盖 host/port。重复执行使用固定轮次编号，不会不断新增同一条示例。

```bash
CHROMA_HOST=127.0.0.1 CHROMA_PORT=8001 .venv/bin/python - <<'PY'
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from memory.models import Turn, Episode
from memory.longterm import LongTermMemory

load_dotenv()  # 默认不覆盖上面显式设置的 host/port
async def main():
    memory = LongTermMemory.from_env()
    turns = tuple(Turn(
        request_id=f"demo-request-{i}", turn_id=f"demo-turn-{i}",
        user_message="我想退运动鞋", assistant_message="可以提交退货申请",
        created_at=datetime(2026, 11, 10, tzinfo=timezone.utc),
    ) for i in range(2))
    episode = Episode.from_turns("demo-user", "demo-old-session", turns,
        "用户想退运动鞋，已告知退货申请流程；联系电话13800138000。")
    await memory.add_episode(episode)
    print("collection:", memory.collection.name)
    print("episode_id:", episode.episode_id)
    print("count:", memory.collection.count())
    print("跨会话查询:", await memory.search("demo-user", "demo-new-session", "运动鞋退货"))
asyncio.run(main())
PY
```

## 数据结构长什么样

Chroma 中每条记录由四部分组成：

| 部分 | 类型 | 本项目内容 |
|---|---|---|
| id | 字符串 | 稳定的 64 位 SHA-256 episode ID |
| document | 字符串 | 脱敏后的本次压缩段摘要，例如“用户想退运动鞋，联系电话[手机号]” |
| metadata | 扁平字典 | `user_key`、`session_key`、`turn_start`、`turn_end`、`turn_count`、`created_at`（压缩段末轮时间）、`content_hash`、`embedding_id` |
| embedding | 浮点数组 | 由所选 embedding 模型生成的向量，用来相似度检索；维度取决于模型 |

用户、会话和轮次标识使用哈希，metadata 不存聊天原文和原始用户 ID。哈希用于标识，不意味着不可推测或完整匿名化。`content_hash` 针对脱敏文本；正文不放进 metadata。

下面是只读接口 `get` 返回形状的示意，哈希用说明文字代替，向量默认不展示：

```json
{
  "ids": ["64位episode哈希"],
  "documents": ["用户咨询运动鞋退货，联系电话[手机号]。"],
  "metadatas": [{
    "user_key": "64位用户哈希",
    "session_key": "64位会话哈希",
    "turn_start": "64位起始轮次哈希",
    "turn_end": "64位结束轮次哈希",
    "turn_count": 2,
    "created_at": "2026-11-10T00:00:00+00:00",
    "content_hash": "64位摘要内容哈希",
    "embedding_id": "siliconflow:BAAI/bge-m3"
  }]
}
```

这些数组按索引一一对应：`ids[0]`、`documents[0]`、`metadatas[0]`、`embeddings[0]` 是同一条记忆。`query` 为多条查询保留了一层列表，内部的 `distances` 是距离，代码再转换为分数。

`docs/episode-storage-example.json` 保存了真实 Chroma 临时验收的返回样例：使用 fake 三维向量，测试 collection 已删除，仅供理解结构，不是生产 embedding 模型的向量。

## 测试

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p pytest_asyncio.plugin -q tests/test_memory_longterm.py
# 显式连接真实 Chroma；使用 fake embedding，不下载模型；测试 collection 会自动删除：
CHROMA_INTEGRATION=1 CHROMA_HOST=127.0.0.1 CHROMA_PORT=8001 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -p pytest_asyncio.plugin -q tests/test_memory_longterm.py -k real_chroma
```

fake 测试覆盖脱敏、幂等写入、用户/会话过滤、相关性边界、去重、token 上限、失败降级、待写重试和压缩失败不入库。真实测试默认 skip。若本机虚拟环境依赖被 iCloud 挂起，先恢复依赖文件或使用干净环境；只读查看工具仅用 Python 标准库，不依赖 Chroma SDK 导入。
