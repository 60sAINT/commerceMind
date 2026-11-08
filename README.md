# CommerceMind

CommerceMind 是面向电商客服场景的多 Agent 应用，提供通用咨询、技术支持、账单处理和升级转人工等处理路径。项目把意图识别、Agent 路由、知识库检索、外部工具、会话记忆、评测与监控串在同一条对话链路中。

本仓库是 **Python 后端**，使用 FastAPI 提供 HTTP API，并提供标准 MCP 服务端。

## 当前状态

项目正在按功能块逐 commit 实现，当前提交只包含仓库骨架：

- [x] 仓库骨架、依赖清单、环境变量模板、测试配置
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
core/         意图识别、鉴权、并发、Skills 等基础能力
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

配置项说明见 [.env.example](.env.example)。
