# CommerceMind

CommerceMind 是面向电商客服场景的多 Agent 应用，提供通用咨询、技术支持、账单处理和升级转人工等处理路径。项目把意图识别、Agent 路由、知识库检索、外部工具、会话记忆、评测与监控串在同一条对话链路中。

本仓库是 **Python 后端**，使用 FastAPI 提供 HTTP API，并提供标准 MCP 服务端。

## 当前状态

项目正在按功能块逐 commit 实现，当前阶段在仓库骨架上增加了最小 LLM 对话链路：

- [x] 仓库骨架、依赖清单、环境变量模板、测试配置
- [x] 最小 LLM 对话链路（`/health`、`/chat`）
- [x] 最小 Docker 镜像（只启动 app）
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

## 最小对话链路

当前只实现一条最小纵向切片：

```text
HTTP /chat -> 参数校验 -> LLM 调用 -> 文本回复 -> 返回 JSON
```

接口：

```http
GET /health
-> {"status": "ok", "model_configured": true, "model": "...", "detail": null}

POST /chat
{"message": "退款一般多久到账？"}
-> {"request_id": "5f2c...", "response": "...", "latency_ms": 123.4}
```

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

没有配置 Key 时服务仍可启动，`/health` 会返回 `model_configured: false`，`/chat` 返回 503。

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

# 真实对话
curl -i -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "退款一般多久到账？"}'
```

成功响应会带 `X-Request-ID` 响应头，响应体包含 `request_id`、`response` 和 `latency_ms`。

常见错误：

| 场景 | 状态码 | `error_code` |
|---|---|---|
| 未配置 `ANTHROPIC_API_KEY` | 503 | `model_not_configured` |
| 消息为空或只有空格 | 422 | FastAPI 默认校验错误 |
| 模型超时 | 503 | `llm_timeout` |
| Key / Base URL 鉴权失败 | 502 | `llm_auth_failed` |
| 模型或接口地址不存在 | 502 | `llm_not_found` |
| 模型返回空内容 | 502 | `llm_empty_response` |

服务日志只记录 `request_id`、消息长度、耗时和错误类型，不记录 Key 和用户原文。

### 4. 运行测试

```bash
pytest -q tests/test_chat_api.py
```

测试全部使用 mock，不需要真实 Key，也不会访问模型接口。

## Docker

当前只容器化 app 本身，不启动 Redis、ChromaDB、Nginx 和 Prometheus。

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

### 验证

```bash
curl -s http://127.0.0.1:8000/health | python -m json.tool
curl -i -X POST http://127.0.0.1:8000/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "退款一般多久到账？"}'
```

没有配置 `ANTHROPIC_API_KEY` 时，`/health` 返回 `degraded`，`/chat` 返回 503，但容器可以正常启动。

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

- Redis 会话记忆
- 意图识别与 Agent 路由
- 知识库 RAG 和工具调用
- MCP
- 评测与监控

配置项说明见 [.env.example](.env.example)。
