# 后端运行说明

后端是同一仓库内的两个部署单元，不使用 Redis，也不依赖独立 Trace 平台。

## 本地启动

在仓库根目录启动数据库：

```bash
docker compose up -d postgres
```

在 `backend` 中安装依赖、初始化配置和数据库：

```bash
uv sync
cp .env.example .env
uv run python scripts/init_chat_db.py
```

已有 `.env` 请补齐示例中的新字段，不覆盖已有 RPC、代理或密钥配置。
`init_chat_db.py` 创建迁移账号、两个服务运行账号、业务表和框架 Checkpointer 表。
应用运行账号不执行 DDL；生产请通过部署环境提供各账号的独立密码。

分别打开两个终端：

```bash
uv run uvicorn app.foundation.main:app --port 8000 --no-access-log
```

```bash
uv run uvicorn app.agent.main:app --port 8001 --reload --no-access-log
```

本地开发使用 `--reload` 自动加载代码变更；生产不启用 reload，使用单 worker。

默认模型需要另行启动 Ollama 并准备 `qwen2.5:7b`。基础服务不依赖模型，可以独立运行。
前端 Vite 将 `/api/v1/chat` 转发到 8001，其他 `/api` 请求转发到 8000。
生产网关采用同样分流，SSE 路径关闭代理缓冲。API 用 HTTPS，数据库按部署规范加密与备份。

## 业务与 Agent 边界

- 基础资产、行情、风险、质押的计算逻辑继续位于 `app/modules`，不依赖模型。
- 普通钱包页面使用 `/me/assets`、`/me/risk-report`、`/me/staking-positions`，不提交钱包地址。
- Agent 仅注册余额概览、单币余额、公开行情、链信息、结果汇总五个标准工具。
- 工具定义与唯一注册列表在 `app/agent/tools.py`。
- 模型只看到安全查询和元数据；真实结果保存于基础服务，前端凭结果引用读取。
- Checkpointer 保存模型消息；对话表保存展示历史和归属，两者不是两套模型历史。
- nonce、登录会话、Refresh Token、限流使用 PostgreSQL；短时公开行情缓存使用进程内存。

## 验证

```bash
uv run pytest -q
RUN_DATABASE_TESTS=1 uv run pytest -q tests/integration/test_services_v1.py
uv run ruff check app tests scripts
```

启动两个服务及模型后，可执行 `uv run python scripts/check_chat.py` 验证真实模型/RPC 链路。
数据库测试使用随机测试钱包，模型及链上读取被替换以实现确定性回归，不能替代生产压测。
新网络的公共 RPC 和代币参数上线前还需要部署环境验证，`scripts/verify_constants.py` 支持链上核对。

## 日志

每行一个 JSON，包含 `trace_id`、`span_id`、`parent_span_id`、服务名、阶段、耗时、状态。
HTTP 响应返回 `X-Trace-Id`；Agent 调基础服务显式传播同一追踪 ID。
不记录消息原文、地址、资产金额、凭证或异常响应原文。stdout 的轮转和保留由部署环境管理。
安全审计单独写入数据库，不能依赖日志采样或日志留存。

## 数据迁移与边界

新表通过新增迁移建立，不删除原聊天表；旧钱包 JWT 需要重新签名登录。
旧聊天数据保留在旧表，未自动导入新对话，避免把旧含金额的消息直接送入模型。
迁移使用冻结的版本化 SQL，不保留旧聊天 ORM，也不自动生成迁移。
默认只支持 EOA 钱包登录；合约钱包 ERC-1271 尚未接入。
用户输入采用安全规范化白名单，不把任意原文送入在线模型；无法识别时追问。
云端模型需配置对应 API Key，使用 `/chat/session/model` 切换，显式确认元数据外发。
七日行情或清单外代币的报价缺失时明确返回缺失，不编造价格；CNY 汇率不可用时明确报错。
完整实现和阅读流程见 [新手阅读文档](../docs/backend-beginner-walkthrough.md)。
