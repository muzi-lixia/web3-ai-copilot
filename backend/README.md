# 后端工程结构

当前采用模块化单体架构：按业务领域组织模块，在模块内部区分服务、数据契约和持久化。一个 FastAPI 进程提供业务 API 和单会话聊天，业务计算不依赖 AI 框架。

## 目录

```text
app/
├── main.py                     # ASGI 入口、应用工厂和资源生命周期
├── api/
│   ├── router.py               # 统一注册业务路由
│   ├── dependencies.py         # 从应用实例取得服务与可信身份
│   ├── health.py               # /health 存活、/ready 就绪
│   └── v1/                     # auth、wallet、market、risk、staking、chat
├── core/
│   ├── config.py               # 配置与生产参数校验
│   ├── container.py            # 服务和资源装配根
│   ├── exceptions.py           # 业务异常
│   ├── exception_handlers.py   # HTTP 异常转换
│   └── logging.py              # 日志配置
├── modules/
│   ├── auth/                   # service.py、schemas.py
│   ├── asset/                  # service.py、schemas.py、tokens.py、valuation.py
│   ├── market/                 # service.py、schemas.py
│   ├── risk/                   # service.py、schemas.py
│   └── staking/                # service.py、schemas.py、constants.py
├── ai/
│   ├── conversation/           # service、repository、models、schemas、prompts
│   ├── memory/                 # 上下文与摘要 service.py、摘要 prompts.py
│   └── llm/                    # ChatModel 协议与 Ollama 适配器
├── infrastructure/
│   ├── database/               # 连接、用户事务、ORM 基类、单实例运行锁
│   ├── blockchain/             # RPC、链注册表、只读合约调用
│   └── providers/              # Beep、DexScreener、DefiLlama 数据适配
└── common/                     # 共享数据质量契约、金额单位、并发请求合并
alembic/                        # 已有数据库迁移，SQL 与 revision 不变
scripts/                        # 初始化数据库、核对常量、真实聊天验证
tests/
├── unit/                       # 纯业务、模型协议、运行时故障用例
├── integration/                # HTTP、鉴权、真实 PostgreSQL
├── factories.py                # 测试共享数据工厂
└── test_architecture.py         # 实际依赖边界检查
```

只创建已经存在实现的模块。工具调用、Agent 工作流、RAG、Redis 和 Worker 尚未接入，没有为它们生成空文件或添加新框架依赖。

## 依赖方向与扩展

- HTTP API 调用 `modules/*/service.py` 或 `ai/conversation/service.py`，不直接操作数据库、模型或仓储。
- 普通业务模块不依赖 `ai`、FastAPI 或 Agent SDK，可被 REST、未来 Tool Adapter 和维护脚本复用。
- 外部 RPC 与行情解析放在 `infrastructure`；金额计算和风险规则仍由业务模块确定性完成。
- 基础设施不调用业务服务；适配器可以使用领域的数据契约。
- `ai/memory` 负责上下文策略，`ai/llm` 负责模型输送，`ai/conversation` 负责原文、轮次和生成生命周期。
- `core/container.py` 集中装配依赖，业务模块不反向导入容器。

新增业务时创建相应 `modules/<领域>`，在 `api/v1` 增加薄路由并通过 `api/router.py` 注册。需要数据库才增加模型和仓储，不为没有持久化需求的资产/行情模块创建空 repository。

新增 Agent 时建立真实 `ai/agent` 编排与 `ai/tools` 适配；Tool 复用现有业务服务，钱包身份由服务端运行上下文提供，不由模型参数决定。新增 RAG 时独立建立 `ai/rag` 的入库与检索能力。

更换模型时实现 `ChatModel` 协议，并通过 `ApplicationServices.build(settings, model=...)` 注入。HTTP、业务计算和上下文策略不依赖具体 SDK。

## 生命周期与一致性

`create_app()` 创建独立服务容器。启动先取得单实例数据库锁，再收尾遗留轮次；退出停止生成与后台任务，释放锁和连接池。未启动的聊天实例拒绝请求，测试不再依靠修改生产开关绕过启动。

每次数据库操作使用独立短事务，事务内设置用户身份并由 RLS 隔离。历史、轮次和上下文等组合读取采用一致的读快照；生成和 SSE 等待不持有数据库事务。

每用户一个固定会话，网络重发复用幂等 ID，停止与断开订阅分离。摘要与裁剪共用安全预算；摘要失败保留原文与旧版本。

之前目录迁移保留数据库与接口；最新 REST 契约整理已替换旧 URL 与响应，数据库表和迁移 revision 不变，也不恢复多会话入口。

## 验证

```sh
uv run ruff check app tests scripts alembic
uv run pytest -q
# DATABASE_URL 必须指向初始化好的专用测试库
RUN_CHAT_DB_TESTS=1 uv run pytest -q
# 独立测试库、临时 HTTP 服务、本机 Ollama
uv run python scripts/check_chat.py
```

当前完整后端回归为 118 项通过（包含真实 PostgreSQL）。之前目录迁移的回归为 87 项。架构用例检查真实目录存在，约束业务模块不依赖 AI/HTTP、基础设施不依赖业务服务、路由不绕过服务层，以及独立应用实例和资源所有权。

## 代码阅读

应用模块、类和函数均提供中文说明，关键分支解释事务、并发、状态和失败处理的原因。先读 `main.create_app()` 与 `ApplicationServices.build()`，再沿具体业务追踪，使用函数名定位而非固定行号。

详细路线见 [代码阅读指南](../docs/code-reading-guide.md)，原理自测见 [阅读问题与解释](../docs/code-reading-answers.md)。文档对应实际目录和单会话接口，未来能力另列规划。

2026-10-09 注释整理仅增加说明及调整格式，可执行代码结构未改变。此次验证为 70 项通过、17 项真实数据库用例未启用；上文 87 项是架构迁移时的完整回归结果。

## HTTP 接口契约更新

旧路由直接删除，认证改为 challenges/tokens，钱包关联资源统一归 wallets，会话使用幂等 PUT。普通成功为 code/msg/data，HTTP 错误也使用 code/msg/data，异步轮次返回 Location；SSE 保留事件协议。GET 不创建会话或直接补写终态。

详见 [当前接口契约](../docs/api-contract.md)。前端已同步新路径、方法及 code/msg/data 解包，普通请求与 SSE 建连错误共用错误解析。
