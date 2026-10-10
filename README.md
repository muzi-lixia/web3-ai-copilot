# Web3 AI Copilot

当前工程采用同仓库的两个后端服务：基础服务提供独立的资产、行情、风险和质押 API；Agent 通过标准工具调用基础服务，负责自然语言查询与安全上下文。

- 后端：FastAPI、PostgreSQL、LangChain / LangGraph、Ollama / DeepSeek / Qwen。
- 前端：React、Ant Design、钱包签名登录、SSE、真实结果卡片。
- 不使用 Redis；日志采用 JSON＋trace_id，不部署独立追踪平台。
- 保留 Berachain 基础业务，并登记 Ethereum、BSC、Polygon、Arbitrum 和 Base。
- 当前 Agent 只读，不包含交易、签名执行或 RAG。

## 启动

完整配置与两个服务的启动命令见 [后端运行说明](backend/README.md)。

```bash
docker compose up -d postgres
cd backend
uv sync
uv run python scripts/init_chat_db.py
```

分别运行：

```bash
uv run uvicorn app.foundation.main:app --port 8000 --no-access-log
uv run uvicorn app.agent.main:app --port 8001 --workers 1 --no-access-log
```

前端在 `frontend` 中执行 `npm ci`、`npm run dev`。默认模型需准备本地 `qwen2.5:7b`。
已有 `.env` 保留原业务配置，按 `backend/.env.example` 补充新配置。生产不要使用开发密钥或口令。

## 阅读入口

- [当前架构](docs/backend-architecture.md)
- [一轮对话详细流程](docs/backend-beginner-walkthrough.md)
- [接口与日志契约](docs/api-contract.md)

旧聊天表不会被迁移删除，但旧 JWT 需重新登录，旧聊天历史没有自动导入新对话。
