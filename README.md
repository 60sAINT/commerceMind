# CommerceMind

CommerceMind 是面向电商客服场景的多 Agent 应用，提供通用咨询、技术支持、账单处理和升级转人工等处理路径。项目把意图识别、Agent 路由、知识库检索、外部工具、会话记忆、评测与监控串在同一条对话链路中。

本仓库是 **Python 后端**，使用 FastAPI 提供 HTTP API，并提供标准 MCP 服务端。

## 当前状态

项目正在按功能块逐 commit 实现，当前阶段在仓库骨架上增加了最小 LLM 对话链路：

- [x] 仓库骨架、依赖清单、环境变量模板、测试配置
- [x] 最小 LLM 对话链路（`/health`、`/chat/stream`）
- [x] 最小 Docker 镜像（只启动 app）
- [x] Redis / ChromaDB 本地编排与健康检查
- [ ] 基础工具、鉴权与会话并发控制
- [ ] 意图识别、Skills 动态加载
- [ ] 工具管理、知识库 RAG、MCP
- [ ] 记忆、Agent 路由与编排
- [ ] 评测、监控、FastAPI 入口
- [ ] Docker 部署与完整文档

## 目录结构

```text
api/          FastAPI 入口与 HTTP 接口
agents/       Agent 路由、工具规范与回复编排
core/         配置、LLM 客户端，以及后续意图识别、鉴权、并发、Skills 等基础能力
memory/       Redis 短期记忆与 ChromaDB 长期记忆
tooling/      工具管理、知识库、MCP 客户端/服务端
evaluation/   意图与对话质量评测
monitor/      在线指标、异常检测与告警
config/       MCP、Nginx、Prometheus 配置
data/         示例知识库与评测基线
skills/       Markdown 业务规则
tests/        单元测试、并发与回归测试
```

## 本地开发

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

## 对话链路（SSE + 短期记忆）

当前实现一条纵向切片：

```text
POST /chat/stream -> 读 Redis working memory -> 流式 LLM 调用 -> 原子提交本轮 -> SSE 事件
```

接口：

```http
GET /health
-> {"status": "ok", "model_configured": true, "model": "...",
    "memory": {"enabled": true, "redis": "ok", "chroma": "ok", "detail": null},
    "detail": null}

POST /chat/stream
{"message": "退款一般多久到账？", "user_id": "u1", "session_id": "c1", "client_request_id": "r1"}

event: meta
data: {"request_id": "5f2c...", "session_id": "c1"}

event: delta
data: {"text": "退款一般"}

event: delta
data: {"text": " 3-5 个工作日到账。"}

event: done
data: {"request_id": "5f2c...", "session_id": "c1", "response": "退款一般 3-5 个工作日到账。"}
```

- `session_id` 可省略，服务端生成并在 `meta` / `done` 中返回；后续轮次带同一个 `session_id` 才能续接记忆。
- `client_request_id` 可省略；同 ID、同消息直接回放上次结果，同 ID 不同消息返回 409。
  重试时即使不带 `session_id`，也会按用户级回执回到原会话。
- 生成阶段失败通过 `event: error` 返回（HTTP 仍为 200）；参数校验、模型未配置等前置错误仍是普通 JSON 4xx/5xx。
- Redis/Chroma 不可用时按无记忆模式继续回答，`/health` 显示 `degraded`；
  `COMMERCEMIND_MEMORY_REQUIRED=true` 时启动直接失败。

### 1. 准备配置

如果还没有 `.env`，先复制模板；已有 `.env` 就直接编辑，不要覆盖：

```bash
cp .env.example .env
```

编辑 `.env`，至少填写：

```dotenv
ANTHROPIC_API_KEY=你的真实Key
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
ANTHROPIC_MODEL=你的模型ID
```

可选：

```dotenv
COMMERCEMIND_LLM_TIMEOUT_S=15
COMMERCEMIND_LLM_MAX_TOKENS=1024
```

没有配置 Key 时服务仍可启动，`/health` 会返回 `model_configured: false`，`/chat/stream` 返回 503。

### 2. 启动服务

```bash
source .venv/bin/activate
uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```

如果 8000 端口已被占用，可以换成 `--port 8010`，下面的 curl 地址同步修改。

启动后可访问：

- Swagger：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`

### 3. 调用接口

```bash
# 健康检查：确认模型是否已配置
curl -s http://127.0.0.1:8000/health | python -m json.tool

# 真实对话（SSE，-N 关闭 curl 缓冲）
curl -N -i -X POST http://127.0.0.1:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message": "退款一般多久到账？"}'
```

成功时先收到 `meta`，随后是若干 `delta`，最后是 `done`；`done.response` 是本轮完整回答。
生成阶段失败会收到 `event: error`，其中包含 `error_code` 和 `detail`。

常见错误：

| 场景 | 传输方式 | 状态码 | `error_code` |
|---|---|---|---|
| 未配置 `ANTHROPIC_API_KEY` | JSON | 503 | `model_not_configured` |
| 消息为空或只有空格 | JSON | 422 | FastAPI 默认校验错误 |
| 模型超时 | SSE `error` | 200 | `llm_timeout` |
| Key / Base URL 鉴权失败 | SSE `error` | 200 | `llm_auth_failed` |
| 模型或接口地址不存在 | SSE `error` | 200 | `llm_not_found` |
| 模型返回空内容 | SSE `error` | 200 | `llm_empty_response` |

服务日志只记录 `request_id`、消息长度、耗时和错误类型，不记录 Key 和用户原文。

### 4. 记忆调试接口（仅开发）

`COMMERCEMIND_MEMORY_DEBUG=false`（默认）时路由不存在，访问返回 404。开启后可以只读查看某个会话：

```bash
COMMERCEMIND_MEMORY_DEBUG=true uvicorn api.main:app --reload

curl -s "http://127.0.0.1:8000/memory/c1?user_id=u1" | python -m json.tool
```

```json
{
  "session_id": "c1",
  "user_id": "u1",
  "turn_count": 2,
  "summary": "用户正在咨询退款。",
  "state": {"stage": "refund"},
  "turns": [
    {
      "turn_id": "...",
      "request_id": "...",
      "user_message": "订单什么时候发货？",
      "assistant_message": "请提供订单号。",
      "created_at": "2026-11-11T09:00:00+00:00",
      "metadata": {}
    }
  ],
  "ttl_s": {"working": 604700, "state": 604700, "summary": 604700}
}
```

该接口只读，不触发压缩、画像或后台维护，也不返回 Redis key 和回执内容。生产环境不要开启。

### 5. 运行测试

```bash
pytest -q
```

测试全部使用 mock，不需要真实 Key，也不会访问模型接口。

## Docker

当前 compose 包含三个服务：

- `commercemind`：FastAPI 应用，宿主机 `8000`
- `redis`：短期记忆，宿主机 `6379`，AOF 持久化
- `chromadb`：长期记忆，宿主机 `8001`，数据卷持久化

`/chat/stream` 已接入 Redis 短期记忆；Chroma 目前只参与 `/health` 检查，长期记忆检索尚未接入。app 默认不强制依赖 Redis/Chroma，由 `COMMERCEMIND_MEMORY_REQUIRED` 控制是否允许降级启动。Nginx 和 Prometheus 仍未编排。

### 构建并启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f commercemind
```

镜像构建慢时，可以指定 pip 镜像源：

```bash
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build
```

### 只启动 Redis / ChromaDB

如果只想在宿主机运行 FastAPI，可以先单独启动记忆基础设施：

```bash
docker compose up -d redis chromadb
docker compose ps
```

宿主机访问地址：

```text
Redis:    127.0.0.1:6379
ChromaDB: http://127.0.0.1:8001
```

此时 `.env` 里要使用宿主机地址：

```dotenv
REDIS_URL=redis://:commercemind123@localhost:6379/0
CHROMA_HOST=localhost
CHROMA_PORT=8001
```

### 验证

```bash
curl -s http://127.0.0.1:8000/health | python -m json.tool
curl -N -i -X POST http://127.0.0.1:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message": "退款一般多久到账？"}'
```

记忆基础设施验证：

```bash
# Redis
docker compose exec redis redis-cli -a "$REDIS_PASSWORD" ping

# ChromaDB
curl -s http://127.0.0.1:8001/api/v1/heartbeat
```

没有配置 `ANTHROPIC_API_KEY` 时，`/health` 返回 `degraded`，`/chat/stream` 返回 503，但容器可以正常启动。

### 常用命令

```bash
docker compose stop        # 停止容器，保留镜像
docker compose down        # 停止并删除容器
docker compose up -d --build commercemind   # 改代码后重建
```

`.env` 通过 Compose 注入容器，不会打进镜像。修改 `.env` 后需要重建容器：

```bash
docker compose up -d --force-recreate commercemind
```

### 当前阶段不包含

- 记忆业务逻辑（Redis/ChromaDB 基础设施已就绪）
- 意图识别与 Agent 路由
- 知识库 RAG 和工具调用
- MCP
- 评测与监控

配置项说明见 [.env.example](.env.example)。

## Skills 渐进加载、自动匹配与手动选择

设置 `COMMERCEMIND_SKILLS_ENABLED=true` 后，应用创建时扫描
`COMMERCEMIND_SKILLS_ROOT_DIR`（默认 `skills`，相对服务工作目录）。
复用 `SkillManager` 的元数据快照、正文按需加载和原子重载机制；参考 EchoMind
的启动和管理接口方式，聊天使用轻量目录、description 语义选择和正文按需加载。

每个包使用 `skills/<skill_id>/SKILL.md`，front matter 支持 `name`、
`description`、`keywords`、`version`、`enabled`。API 要求 frontmatter 提供非空的
`name` 和 `description`，遇到闭合分隔符即停止读取，不从正文回退。目录中的 id 就是文件夹名。
名称和描述用于模型选择；keywords 不参与选择，也不注入模型目录。
`references/`、`templates/`、`scripts/` 保存按需资源；脚本不会自动执行。

- Skills 开启后，`/chat/stream` 的 system 始终包含轻量目录（id、名称、描述），包括无记忆、有记忆及记忆读取失败的路径。
- 不传 `skill_ids` 或传 `null`，且消息没有手动命令时，先调用模型根据 description 做语义选择。选择阶段只看到名称、描述和 id，不读取正文或资源。只有通过校验的选中 id 才会触发正文加载。
- 手动选择有两种方式：消息开头输入 `/billing_support 问题内容`；或请求传 `skill_ids`。二者合并去重并跳过自动选择。支持 `/billing_support /technical_support 问题内容`。
- 显式传 `skill_ids: []` 可关闭本轮自动选择（消息有 slash 命令时仍执行手动选择）。
- 每轮重新选技，不把选中状态永久绑定到会话。正文按需加载并缓存，加入 system 时保留原有系统角色和记忆上下文；不读取包内资源内容。
- 自动选择最多额外调用一次模型，最多等待 8 秒；未匹配、超时、调用失败或返回非法/越界 id 时不加载正文，继续普通回答。选择目录 JSON 受目录预算限制，放不下的条目跳过；完整目录可通过 GET 获取并手动选择。
- 前端在 `frontend/`：输入 `/` 会弹出 `GET /skills` 的技能候选菜单，支持按 id、名称、描述、关键词过滤与 `↑` / `↓` / `Enter` 选择；选中项以 chip 展示，最多 3 个，发送时作为 `skill_ids` 传给后端。也可以直接手输 `/skill_id 问题内容`，前端会把手输命令里已有的技能从 `skill_ids` 中剔除，避免重复。单独发送 `/billing_support` 时后端返回 `422 skill_message_required`，前端映射为“选择技能后还需要输入问题，请在 /技能名 后补充具体问题”。技能面板提供 SKILL.md/ZIP 上传、确认启用、列表与删除，支持 `POST /skills/reload`，加载异常可展开查看。
- 原始消息保留在记忆与幂等回执中；发给本轮回答模型的问题去除开头的 Skill 命令。
- Skills 关闭时，不创建管理器，不注入目录或正文，忽略 `skill_ids`，原有模型调用与记忆行为保持不变。

```bash
curl http://localhost:8000/skills
curl -X POST http://localhost:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"退款一般多久到账？"}'
# 手动选择（跳过自动选择模型调用）
curl -X POST http://localhost:8000/chat/stream \
  -H 'Content-Type: application/json' \
  -d '{"message":"/billing_support 退款一般多久到账？"}'
```

`GET /skills` 返回 `enabled/count/skills/errors/version/loaded_at/management_enabled`，
启用时还返回文件管理 `limits`（单文件/整包字节数与文件数量）。
只包含元数据摘要，不包含正文、资源内容或正常目录项的服务器绝对路径。
关闭时返回 `enabled=false`、空目录、版本 0。
未知或已删除的 Skill 返回 HTTP 404，`error_code=skill_not_found`；
正文为空或读取失败返回 HTTP 503，`error_code=skill_unavailable`。
错误响应包含 `request_id`，并设置 `X-Request-ID`。

预算配置（均为正整数，非法环境变量回退默认值）：

| 环境变量 | 默认值 | 范围 |
| --- | --- | --- |
| `COMMERCEMIND_SKILLS_CATALOG_MAX_CHARS` | 4000 | 目录块总字符上限，超出截断；完整目录可用 GET 查看 |
| `COMMERCEMIND_SKILLS_BODY_MAX_CHARS` | 8192 | 每个选中正文的字符上限；截断会标记 |
| `COMMERCEMIND_SKILLS_RESOURCE_MAX_CHARS` | 20480 | 加载器单资源读取上限，本次聊天不自动读取资源 |
| `COMMERCEMIND_SKILLS_MAX_SELECTED_SKILLS` | 3 | 单次选择的不同 id 上限 |

`skill_ids` 按请求顺序去重并去除两端空白；空 id、超过 64 字符的 id 或类型错误返回 422。
不同 id 超过上限返回 422，`error_code=too_many_skills`。
每次新的选技请求应使用新的 `client_request_id`：现有记忆回执仍按用户消息判断重试，
相同消息与请求 id 会回放原答案，不会因 Skill 选择改变而重新调用模型。

当前没有鉴权，只有同时设置 `COMMERCEMIND_SKILLS_ENABLED=true` 和
`COMMERCEMIND_SKILLS_DEBUG=true` 才注册以下管理接口，否则返回 404，
OpenAPI 中也没有此路由。请仅在可信本地环境打开 debug。

```bash
curl -X POST http://localhost:8000/skills/reload
```

重载返回 `added/updated/removed/unchanged/errors/version/loaded_at/duration_ms/count`，
不返回正文；重新扫描目录、原子替换快照并清空正文缓存，后续请求使用新目录及正文。
重载只影响当前服务进程，多 worker 部署需逐进程重载或重启。

前端「技能」→「上传技能」选择单个 `SKILL.md` 或 ZIP，校验通过后展示名称、描述和文件数，
点击「上传并启用」才正式安装。界面保留技能列表、删除确认和重新加载，不提供在线文件编辑器。
ZIP 必须只包含一个根技能文件夹，例如 `my-skill/SKILL.md` 与 `my-skill/references/...`，
不接受把 SKILL.md 直接散放在 ZIP 根目录。新上传技能的 `name` 使用小写字母、数字、连字符，
最多 64 字符，ZIP 根文件夹名必须与 name 一致；已有中文名称/下划线 ID 的技能继续兼容。
现有上传预算保持不变：默认单文件 64KiB、整包 1MiB、64 个文本文件；同时约束 ZIP 原始大小和解压后大小。
这是格式/路径/压缩包安全校验，不包含语义恶意内容扫描或人工审核流程。

| 方法与路径 | 输入 | 行为 |
| --- | --- | --- |
| `POST /skills/upload/preview?filename=...` | 原始 `.md` / ZIP 字节 | 校验并返回 skill/file_count/total_bytes，不写正式目录、不发布快照 |
| `POST /skills/upload?filename=...` | 同一文件的原始字节 | 重新校验、创建技能并 reload，同名 409，不覆盖 |
| `GET /skills/{id}/files` | 路径 ID | 返回 `id/files:[{path,content}]/revision`，读取完整可编辑文本包 |
| `POST /skills/import` | `id/files` | 创建包并重载，同名默认 409；兼容已有显式 `overwrite` 参数 |
| `PUT /skills/{id}/files` | `files/revision` | 按完整文件清单替换包并重载，未列出的旧资源删除 |
| `DELETE /skills/{id}` | query `revision` | 删除整个包并重载 |
| `POST /skills/reload` | 无 | 重新扫描磁盘文件，发布新目录快照、清空正文缓存 |

确认上传的接口内部已执行 reload，前端只刷新 GET 目录，不再重复 POST reload。
JSON 文件接口保留兼容既有调用，前端不再展示编辑入口。编辑和删除使用读取时的 SHA-256 内容版本 `revision`；文件发生变化则 409，避免覆盖并发修改。
上传只接受 UTF-8 文本：根 `SKILL.md` 和 `references/`、`templates/`、`scripts/` 下资源，
默认单文件 64KiB、整包 1MiB、64 个文件。符号链接、越界路径、隐藏文件及二进制资源拒绝管理，
防止完整替换时静默丢失不能编辑的文件；脚本仅保存，不执行。
写入采用暂存校验、备份、rename 安装和失败时尽力恢复；普通上传失败保留所选文件供重试，
网络中断导致结果未知时应先刷新列表核对。Compose 将 `./skills` 可写挂载至 `/app/skills`，重建容器后文件仍保留。
此机制由显式请求触发，无目录监听器；进程内锁不覆盖多 worker 或外部程序直接改文件。

验证：`python -m pytest -q tests/test_skill_api.py tests/test_skill_loader.py tests/test_skill_content.py`。

## 工具调用可观测性

设置 `COMMERCEMIND_TOOLS_DEBUG=true` 后，提供以下只读接口；关闭时不注册路由：

- `GET /tools/stats`：注册状态、缓存条数、各工具执行统计与 Function Calling 汇总；
- `GET /metrics`：Prometheus 文本指标，不需要额外监控依赖。

```bash
curl http://localhost:8000/tools/stats
curl http://localhost:8000/metrics
```

工具统计区分逻辑调用、物理尝试、重试、参数拒绝、超时、熔断拒绝、降级和取消。
参数拒绝单独计数，不加入逻辑调用 total；缓存命中算逻辑成功，不增加 attempts。
failed 表示主路径失败，因此降级有数据时仍增加 failed；取消发生在降级期间时也可能同时记录主路径失败。
平均延迟沿用执行层累计口径；P95 使用最近 256 次观测（包括缓存命中、参数拒绝和取消）的实际等待时间，采用 nearest-rank 算法。
Function Calling 另汇总成功、失败、取消、提前关闭、轮数、调用数、修复次数和协议回退次数。
token 指标仅覆盖非流式工具阶段，未包含意图识别、重排和最终流式回答。

这些指标是进程内累计值，重启后清零，多 worker 需分别采集。工具注销后清除对应样本，评测 fork 不继承线上计数。
读取指标不会调用工具、清理缓存或占用半开探测名额；缓存条数包含尚未惰性清理的过期条目。
SSE done 额外提供 `tool_loop_latency_ms`，涵盖工具循环与最终回答。

日志中的 `tool_call`、`tool_result` 和 `function_calling_completed` 均可通过 SSE meta 的 request_id 关联。
聚合快照不保存请求 ID；日志和指标不包含用户/会话身份、消息、工具参数、结果正文或异常详情。
Prometheus 标签只使用已注册工具名、固定错误码和固定状态，不使用用户提供的名称。
