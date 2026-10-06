# Web3 AI Copilot

基于 AI Agent 的链上资产分析与知识助手。用自然语言完成钱包资产查询、Token 行情、质押仓位分析、
资产风险分析，以及 Web3 协议知识问答。

## 文档地图

| 文档 | 回答什么 |
|---|---|
| `PLAN.md` | 做什么、不做什么、什么顺序（策略） |
| `IMPLEMENTATION.md` | 钱包 / 行情 / 质押三个模块的实现细节 |
| `ARCHITECTURE.md` | 工程蓝图：技术栈 / 目录 / 契约 / 逐功能实现 / 前后端结合 |

## 目录

    backend/      FastAPI + 链上数据 + Agent + RAG
      app/
        core/         配置、日志、异常
        api/          路由层（薄，只做校验）
        schemas/      前后端契约的唯一真源
        services/     业务逻辑（不依赖 HTTP，可单测）
        tools/        Agent 工具定义与注册表
        agents/       LangGraph 编排（V2）
        rag/          检索链路（V2，pgvector）
        constants/    token 表、协议识别表
      scripts/       后端运维脚本（RAG 索引导入等）
    frontend/     React 19 + Vite + antd
      src/
        api/          与后端路由一一对应，client.ts 统一请求与错误
        types/        与后端 schemas 对齐
        stores/       zustand：当前地址、对话、Agent 轨迹
        hooks/        useAgentStream（SSE 分帧）
        views/        Dashboard / Portfolio / Staking / Copilot / Knowledge
        components/   AppLayout / AgentTrace / …
    contracts/    独立 Solidity 工程（自有锁仓合约，Phase D 部署 Sepolia）
      src/          NodeStaking.sol
      abi/          部署后导出，backend 运行时读它做 ABI 解码
      scripts/      deploy_staking.py
    docs/         RAG 语料（Ethereum / Lido / Uniswap / Aave）
    evaluation/   Agent & RAG 评测（V3）

## 本地启动

后端跑在 `:8000`：

    cd backend
    cp .env.example .env
    uv sync
    uv run uvicorn app.main:app --reload

不用 uv 的话走 `requirements.txt`（由 `uv.lock` 导出，72 个包全部锁版本，与 `uv sync` 结果一致）：

    pip install -r requirements.txt
    uvicorn app.main:app --reload

前端跑在 `:5173`，`/api` 由 vite proxy 转发到 `:8000`：

    cd frontend
    npm install
    npm run dev

验证：

- 后端：`curl http://127.0.0.1:8000/health` → `{"status":"ok","chain_id":1}`
- 接口文档：http://127.0.0.1:8000/docs
- 前端：http://localhost:5173 → 侧边栏五个页面可切换
