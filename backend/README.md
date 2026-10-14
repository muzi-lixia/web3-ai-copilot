# 后端运行说明

同仓库包含基础 HTTP 服务、Agent HTTP 服务和独立文档 worker。PostgreSQL 提供业务持久化、模型 checkpoint、任务队列和租约；不依赖 Redis。

## 代码与依赖边界

| 目录 | 职责 |
|---|---|
| `app/foundation` | 基础服务配置、应用装配和生命周期 |
| `app/foundation/api/v1` | 基础服务路由、鉴权声明及请求校验 |
| `app/modules/identity` | 钱包登录、凭证、会话及审计业务 |
| `app/modules/asset/results.py` | 资产结果持久化与汇总 |
| `app/modules/*` | 资产、行情、风险、质押、在线知识库业务 |
| `app/agent` | Agent 配置、对话 API、模型与工具编排 |
| `app/workers/knowledge` | 独立文档消费者、解析、清洗、切分、入库和续租 |
| `app/infrastructure` | 数据库连接、RPC 与外部数据源适配 |
| `app/core/settings.py` | 配置加载基类和通用 HTTP 字段 |
| `app/core/http` | 两个 HTTP 服务共用的响应与异常处理 |
| `app/infrastructure/resources.py` | 显式配置和进程内数据源缓存 |

基础服务不导入 Agent 或 worker，worker 不加载 HTTP/Agent 应用；基础业务不依赖对话模型。处理算法在 worker，在线管理与检索在知识库业务模块，两边复用仓储、原件路径及向量契约。embedding 网络客户端在 `infrastructure/providers/embedding.py`，向量序列化在 `infrastructure/database/vector.py`；Agent 不再保留重排转发层。运行入口、诊断脚本与测试均使用各运行单元自己的配置，不再保留聚合配置兼容层。

## 配置与本地启动

四类配置各自校验：`FoundationSettings`、`AgentSettings`、`WorkerSettings`、`MigrationSettings`。基础服务不要求 Agent 数据库或模型密钥，Agent 不要求 JWT 密钥或基础数据库，迁移不要求 HTTP 配置。

可沿用 `backend/.env`（不覆盖已有 RPC/代理/密钥），也可参考 `.env.foundation.example`、`.env.agent.example`、`.env.worker.example`、`.env.migration.example` 分别注入环境变量。HTTP 可用 uvicorn 的 `--env-file`，worker 支持 `--env-file .env.worker.local`；生产建议由部署环境注入。`.env.*.local` 不入库。

先在仓库根目录运行 `docker compose up -d postgres`。然后进入 backend：

```bash
uv sync
uv run python scripts/init_chat_db.py
```

初始化脚本创建缺失账号、执行迁移和 checkpoint setup，再授权；已有账号口令不覆盖。生产用 `CHAT_DB_ADMIN_URL`、`CHAT_DDL_PASSWORD`、`FOUNDATION_DB_PASSWORD`、`AGENT_DB_PASSWORD`、`KNOWLEDGE_DB_PASSWORD` 等环境变量提供独立凭据。应用账号不执行 DDL。

在三个终端分别启动：

```bash
uv run uvicorn app.foundation.main:app --port 8000 --no-access-log
uv run uvicorn app.agent.main:app --port 8001 --workers 1 --no-access-log
uv run python -m app.workers.knowledge
```

开发可增加 `--reload`；生产 Agent 保持单 worker。默认对话模型需准备 Ollama 的 `qwen2.5:7b`，知识库需准备配置的 embedding 模型。前端把 `/api/v1/chat` 转发到 8001，其他 `/api` 到 8000；生产网关同样分流，SSE 关闭缓冲。API 使用 HTTPS。

生产数据库连接分别使用 `FOUNDATION_DATABASE_URL`、`AGENT_DATABASE_URL`、`KNOWLEDGE_DATABASE_URL`、`DATABASE_MIGRATION_URL`。worker 只用专用知识库连接，不回落到基础账号。其角色只能读取文档、更新处理字段、读写分片及清理/心跳表，不能访问身份、资产或 Agent 轮次，也不能修改原件文件名、删除文档。

## 身份、模型与数据

钱包连接不等于登录，需签名验证；当前支持 EOA，未接入 ERC-1271。JWT 同时检查登录会话有效性，nonce、刷新令牌和限流均持久化。用户接口默认只接受 user 凭证；agent 凭证仅开放身份投影、创建余额结果、汇总、知识库检索，公开行情与链目录保持原规则。管理、删除、登录态操作和对话入口禁止 agent 凭证。委托绑定登录会话，尚未绑定单轮 ID。AGENT_PERMIT_SECONDS 只由基础服务配置（默认 300 秒）。签发接口返回 expires_at；Agent 根据实际剩余有效期校验排队与执行预算，不再维护重复配置。

模型接收局部脱敏查询、必要历史和白名单资产摘要（币种、数量、估值、时间等）。工具摘要不含钱包地址或凭证；输入脱敏不能识别所有敏感表达。新会话默认本地模型，云端须显式确认对话、持仓及知识库片段外发。独立云端改写不绕过用户选择；模型消息与展示原文分别保存。

PostgreSQL checkpoint 包含工具资产摘要；清空和过期清理删除模型历史。LangSmith 默认隐藏输入输出并替换异常原文；显式启用内容追踪时保留脱敏后的调试内容。未配置 key 时关闭自动追踪。stdout JSON 日志仅允许受控元数据，响应携带 X-Trace-Id；审计独立写数据库。旧 JWT 需按既有迁移规则重新登录，旧聊天表不删除、未自动导入新会话。

工具唯一注册处为 `app/agent/tools/__init__.py`。金额由业务后端计算，使用十进制字符串；缺价、失败网络与无汇率明确呈现，不编造。普通页面查询本人接口，不接受任意钱包作为查询目标。

## 文档任务、存储与检索

API 与 worker 必须共享 `KNOWLEDGE_STORAGE_DIR`；生产使用持久化绝对路径或共享挂载。任务使用 SKIP LOCKED 原子领取，默认租期 60 秒，每三分之一租期续租。仅回收过期任务，旧 token 无法续租或提交。反复中断默认最多自动尝试三次，恢复后退避 5 秒，耗尽后转失败；管理员手动重试会重置预算。

上传前登记持久化清理意图，原件经临时文件原子替换后才登记文档，成功时同事务撤销清理意图。并发重复上传返回 409。登记与清理锁住同一意图，避免清理和新引用竞争；提交结果不确定时不直接删除可能已被引用的文件。失败/中断上传的无引用文件默认一小时后清理。文档删除与清理任务同事务，磁盘清理失败后由 worker 重试。

worker 没启动时，新上传保持 pending，HTTP 与已有文档检索仍可用。SIGTERM/SIGINT 取消任务并关闭数据库，未完成任务由租约恢复；已启动的解析线程可能继续计算，但不写数据库。进程管理器应独立监督并重启 worker。

在线检索和离线入库拥有不同预算：`EMBEDDING_QUERY_TIMEOUT_SECONDS=15`、`EMBEDDING_QUERY_CONCURRENCY=2`（每个 API 进程）；入库 `EMBEDDING_INGEST_TIMEOUT_SECONDS=180`、`EMBEDDING_INGEST_BATCH_SIZE=8`。可分别设置 QUERY/INGEST_BASE_URL，留空使用 EMBEDDING_BASE_URL。进程预算不代表物理推理资源隔离；两个地址服务的模型权重、修订号和维度须一致。

检索统一执行兼容版本候选召回、原始余弦分过滤、重排和 top-k。候选池 max(20, limit×4)，最多 100。阈值 KNOWLEDGE_SEARCH_MIN_SCORE 默认 0.55，须结合当前语料正负例校准。无相关片段返回 no_answer，空索引与低分过滤分别注明原因。主题相似不等于文档一定包含答案，仍需要生成层忠实度评测。

版本由模型名、EMBEDDING_REVISION、维度与输入格式生成，文档和分片必须都兼容。同名模型更换权重必须提升修订号；改变维度还需迁移向量列及索引。旧向量不猜测版本，需管理员通过知识库页或 POST /knowledge/documents/{id}/reindex 从原件重建；重建期间暂不参与检索。全库不兼容返回明确 409，混合版本只查询兼容部分。

## 观测与压测

基础服务 `/ready` 检查数据库，Agent `/ready` 检查运行租约；`/health` 仅为存活探针。worker 每 10 秒独立更新心跳，30 秒内有心跳视为在线；管理员可读 GET /api/v1/knowledge/status 查看 worker 数、队列、最老任务年龄与清理积压。心跳仅说明进程仍在活动，不保证文档一定处理成功。

Agent 终态保存 queue_ms、first_token_ms、execution_ms、total_ms，并记录活动轮次、待补写数量与拒绝事件。计时从生成任务开始，到生成结束，首字含排队时间，不含最终持久化和网络传输；无模型正文时首字为 null。跨用户统计通过仅返回聚合值的受限数据库函数提供，不开放原始会话数据。

```bash
uv run python -m scripts.runtime_status --service knowledge
uv run python -m scripts.runtime_status --service agent
```

真实 HTTP/SSE 压测使用不同用户的有效访问凭证。凭证文件为 JSON 字符串数组，保存在仓库外；同一用户固定会话不能作为多个并发用户。该命令会真实调用模型/RPC，也会生成对话历史，不自动清空。

```bash
uv run python -m scripts.benchmark_agent --tokens-file /tmp/tokens.json --concurrency 1 2 4 --rounds 3
```

输出状态计数、完成吞吐与成功请求的 P95；服务内 total_ms 与客户端 end_to_end_ms 分开，失败/429 单独计数。结合模型/GPU负载、数据库连接和 SSE 连接数判断瓶颈，再决定提高单实例并发或设计多实例，不能直接增加 Agent worker 数。

## 验证与升级

```bash
uv run pytest -q
RUN_DATABASE_TESTS=1 uv run pytest -q tests/integration/test_knowledge_lease.py
RUN_DATABASE_TESTS=1 uv run pytest -q tests/integration/test_services_v1.py
uv run ruff check app tests scripts alembic
uv run python -m evals.runner --layer retrieval
```

知识库数据库测试使用临时 schema 和临时无登录角色，验证并发、版本、清理与最小权限，结束后清理；可通过 KNOWLEDGE_TEST_DATABASE_URL 指定可创建 schema/角色的专用测试库。普通服务集成测试使用随机测试钱包。真实钱包、模型与 RPC 链路仍需启动后验证，不能用确定性回归替代生产压测。

检索评测包含无答案负例，单独统计拒绝与误召回，空库/版本不兼容记为 blocked，不计成功，也不混入正常问答 MRR。重建后运行真实语料评测，再人工确认是否更新基线。

最新迁移为 0015_observability（包含关联字段和 checkpoint 清理队列；之前为 0012/0013/0014）。升级时停止旧版基础服务、Agent 和 worker，用管理员初始化缺失的 app_knowledge 角色，再使用迁移账号升级，授权后启动新版。`scripts/init_chat_db.py` 封装了上述顺序，运行前应由部署环境提供正确账号凭据。业务运行账号不会自行迁移，不能混跑新旧消费者。仅更新应用代码而不迁移会因缺列/缺表失败。

### 命名与维护入口

资产 HTTP 路由为 `app/foundation/api/v1/assets.py`，资产工具为 `app/agent/tools/assets.py`，
资产执行图为 `app/agent/asset_graph.py`。运行入口为 `app.foundation.main`、
`app.agent.main` 和 `app.workers.knowledge`，不保留旧 `app.main` 别名。
依赖统一由 `pyproject.toml` 与 `uv.lock` 管理，使用 `uv sync` 安装。

诊断命令在 backend 目录通过模块方式运行：

- `uv run python -m scripts.inspect_document <原件>`：加载与清洗摘要。
- `uv run python -m scripts.inspect_cleaning <原件> [输出.html]`：清洗报告。
- `uv run python -m scripts.inspect_chunks <原件...> [输出.html]`：分块报告。
- `uv run python -m scripts.inspect_checkpoint <会话ID>`：完整模型消息历史。
- `uv run python -m scripts.inspect_tool_calls <用户ID> [轮数]`：工具轨迹；身份是 users.id。

风险与质押目前仍为注册的独立业务 API；前端页面移除不改变其服务端可用性。

### 部署配置与调优配置

四份 `.env.*.example` 仅保留主要部署字段。未填写的可选项使用代码默认值，
无需把所有参数抄入环境变量。真实密钥只放在本地 env 或部署密钥系统。
HTTP 服务分别只读取 FOUNDATION_DATABASE_URL 和 AGENT_DATABASE_URL，
不再回退到 DATABASE_URL；worker 和迁移同样只用各自专用连接。

模型按提供方各维护一套配置；MODEL_PROVIDER 仅指定后台默认提供方。
为保留用户外发确认，新会话仍默认本地 Ollama，云端需要用户显式选择。
旧 MODEL_NAME、MODEL_BASE_URL、MODEL_API_KEY、MODEL_CONTEXT_WINDOW
应分别迁移到对应提供方的下列字段，不再作为独立配置来源。

| 提供方 | 模型 | 地址 | 密钥 | 窗口（token） |
| --- | --- | --- | --- | --- |
| Ollama | OLLAMA_MODEL | OLLAMA_BASE_URL | 无 | OLLAMA_CONTEXT_WINDOW（默认 16384） |
| DeepSeek | DEEPSEEK_MODEL | DEEPSEEK_BASE_URL | DEEPSEEK_API_KEY | DEEPSEEK_CONTEXT_WINDOW（默认 65536） |
| Qwen | QWEN_MODEL | QWEN_BASE_URL | QWEN_API_KEY | QWEN_CONTEXT_WINDOW（默认 32768） |

可选调优项如下，按运行数据调整；REWRITE_PROVIDER 只允许留空（沿用当前模型）或
ollama（独立本地改写），配置独立云端改写会校验失败。

| 所属服务 | 配置及默认值 | 用途 |
| --- | --- | --- |
| Agent | MODEL_CONCURRENCY=1、CHAT_QUEUE_LIMIT=16 | 模型并发与在途任务容量（含执行中的任务） |
| Agent | CHAT_QUEUE_TIMEOUT_SECONDS=30、COPILOT_TIMEOUT_SECONDS=180 | 排队与执行超时，单位秒 |
| Agent | MODEL_OUTPUT_TOKENS=8192、MODEL_CONTEXT_RESERVE=1024、AGENT_CONTEXT_TOKENS=16384 | 输出上限、窗口余量与输入预算 |
| Agent | MODEL_PROXY=、REWRITE_PROVIDER= | 云端请求代理与改写策略 |
| Foundation | AGENT_PERMIT_SECONDS=300 | 委托有效期；须覆盖 Agent 排队与执行，建议留有余量 |
| Foundation | RPC_OVERRIDES=、MARKET_PROXY= | RPC 与行情代理覆盖 |
| Foundation | KNOWLEDGE_SEARCH_MIN_SCORE=0.55 | 暂定检索阈值，尚待真实语料评测标定 |
| Foundation | EMBEDDING_QUERY_TIMEOUT_SECONDS=15、EMBEDDING_QUERY_CONCURRENCY=2 | 在线向量请求超时与并发 |
| Foundation | EMBEDDING_QUERY_BASE_URL= | 在线专用地址，留空沿用 EMBEDDING_BASE_URL |
| Worker | EMBEDDING_INGEST_BASE_URL= | 离线专用地址，留空沿用 EMBEDDING_BASE_URL |
| Worker | EMBEDDING_INGEST_TIMEOUT_SECONDS=180、EMBEDDING_INGEST_BATCH_SIZE=8 | 离线向量请求超时与批量大小 |
| Worker | KNOWLEDGE_WORKER_POLL_SECONDS=1、KNOWLEDGE_TASK_LEASE_SECONDS=60 | 扫描间隔与租约，单位秒，处理期间独立续租 |
| Worker | KNOWLEDGE_MAX_ATTEMPTS=3、KNOWLEDGE_RETRY_DELAY_SECONDS=5 | 最大尝试次数与恢复等待秒数 |

Foundation 与 worker 启动时都会核对 chunks.embedding 列维度；配置与数据库不符会直接启动失败。
两者须使用相同的模型、修订与维度，并共享原件目录。改变模型内容或输入格式后须更新修订并重建文档。

### 日志、追踪与恢复

一次 Agent 轮次通过 trace_id 关联 HTTP 与基础服务请求，通过 turn_id 关联业务轮次；
LangSmith 根 run_id 与 turn_id 一致，并保存于 agent_turns.run_id。LangSmith metadata
只允许 trace_id、turn_id、session_id、run_id。默认不外发输入输出，异常原文替换为 redacted；完整模型
历史仍只由 PostgreSQL checkpointer 管理。工具拒绝、鉴权失败、取消和异常均记录真实状态。
排队超时和执行超时分别使用 queue_timeout、execution_timeout；界面仍显示中文说明。

stdout 输出 JSON；只记录受控元数据。异常保留类型和最多 12 层文件名/函数名/行号，
不含绝对路径、源码、局部变量和原文。HTTP 未处理异常同样返回并记录 X-Trace-Id。
后台维护任务有服务标签。数据库记录事务耗时、连接获取耗时（含健康检查/建连）、提交耗时；
超过 500ms 的 SQL 记录慢查询事件，SQL 失败只记录类型和 SQLSTATE，不记录 SQL、参数或 DSN。
日志收集、保留时间与轮转由部署环境配置，应用不重复将普通日志写入业务数据库。

文档登记保存上传 trace_id。每次 worker 领取生成独立 attempt_id，关联 document_id、
worker_id 和 attempt_count；加载、清洗、切分、向量化和提交分别记录 knowledge.stage。
knowledge.claimed / knowledge.completed / knowledge.failed 记录任务结果。
失败原因仅保存安全说明和阶段错误码，不保存服务响应正文、原件路径或异常原文。

删除会话时，数据库触发器在同一事务写入 agent_checkpoint_deletions；清理 checkpoint
成功后才确认删除队列记录，失败在后续维护中重试。清理操作幂等，队列没有会话外键，
不会随会话一起消失。过期会话重建也返回旧 thread_id，保留即时清理路径。

Agent 终态先原子写入 AGENT_OUTBOX_DIR（默认 var/agent-outbox）并 fsync，再提交数据库；
提交成功才删除本地记录。启动时先恢复补写，再收尾中断轮次，不重新调用模型或工具。
恢复补写未成功时拒绝启动，避免收尾逻辑覆盖待恢复结果；数据库中的已有终态不会被重复补写覆盖。
目录权限 0700、记录权限 0600，不保存访问凭证，但包含消息及资产结果，须作为业务数据保护。
生产必须为这个目录挂载 Agent 独享的持久卷（各环境独立），不能使用临时容器文件系统，
也不要与知识库原件目录共享。损坏记录不会自动丢弃，启动会失败并保留文件待修复。
若磁盘和数据库同时失败、磁盘丢失，或进程在写前日志完成前被强杀，仍无法保证恢复最终结果；
已存在的 queued/running 轮次由启动恢复收尾。当前方案不提供跨主机无共享存储的结果恢复。

### 告警检查

在 backend 目录使用对应服务的环境变量运行：

```bash
uv run python -m scripts.check_runtime --service agent
uv run python -m scripts.check_runtime --service knowledge
```

退出码 0 表示未达到阈值，1 表示告警，2 表示配置或数据库等依赖不可用；输出不含业务正文。
Agent 检查应运行于 Agent 主机/容器并挂载同一 outbox。部署监控每分钟执行一次并负责告警投递，
建议连续两次退出非零才通知，恢复为 0 后解除。仓库不会主动发送外部通知。

默认阈值：worker 健康心跳数为 0；知识库 pending 超过 300 秒或存在失败文档；
Agent 活动轮次超过 300 秒；近 15 分钟创建的轮次至少 5 个且失败比例达到 20%；
checkpoint/原件清理队列超过 100 条；终态补写文件存续超过 30 秒。
失败文档告警会持续到重试或删除，表示需要处理的积压，不是瞬时错误次数。
这些阈值是初始运维策略，须结合真实压测和服务目标调整 scripts/check_runtime.py。

升级需先停止旧服务，使用迁移账号应用 0015，并通过 init_chat_db.py 同步权限后再启动。
本次代码变更不会自动修改正在使用的业务数据库；临时 schema 集成测试与业务升级分开执行。

### 当前目录组织

本工程按运行入口和业务模块组织，服务专属代码放回所属服务，不另建多层通用架构。

- `foundation/main.py` 管理 HTTP 生命周期，`foundation/container.py` 装配业务服务与资源。
- `foundation/api/` 包含基础服务路由、健康检查和鉴权依赖；原顶层 `app/api/` 已移除。
- `agent/service.py` 协调受理、幂等、取消与生命周期。
- `agent/execution.py` 负责模型额度、单轮模型/工具执行和快照提交。
- `agent/streaming.py` 负责快照读取、SSE 帧、订阅与广播。
- `agent/recovery.py` 持有待补写状态，负责启动恢复、持久化补写和 checkpoint 清理。
- `agent/llm.py` 负责模型客户端和提供方选择，原 `agent/models.py` 已移除。
- Agent 配置在 `agent/config.py`，Foundation 认证和业务配置在 `foundation/config.py`。
- `infrastructure/providers/config.py` 定义 RPC、行情及其他数据源的配置契约。
  独立脚本的默认资源只加载这组配置，不加载 Foundation 的身份和数据库配置。
- `core/` 保留运行基础能力，`common/` 保留共享数据类型与计算工具；本次不整体重命名。

三个启动命令、HTTP URL 和 env 字段保持既有用法；本次无需新增数据库迁移。
资产结果独立使用 ASSET_RESULT_TTL_HOURS（默认 720 小时），不再误用 Agent 会话配置。
架构测试约束共享层和基础设施不得导入具体服务，Agent 不得导入 Foundation 路由。

### 知识问答的检索约束与调试追踪

`agent/knowledge_policy.py` 将知识问答所需检索作为模型回答的前置条件，通过标准工具节点执行
search_docs，继续记录工具轨迹、耗时、鉴权和调用次数。知识型解释优先检索，明确的资产、行情、
链目录查询保留原来的工具路由，简单问候不触发检索。其他事实问题默认按知识库问题处理；
这是一项保守的产品规则，不是另一个模型分类器。应用触发的调用会标记 origin=policy，
模型自行发起的调用标记 origin=model，不将前置检索冒充成模型自主决策。

每轮都要求本轮成功检索，不复用历史工具结果；发送模型前会移除旧检索结果和旧结论，
包括历史中没有工具调用却声称“已经查询”的回答。数据库原始消息和 checkpoint 不会因此删除。
知识库问题最多 1000 字，超出后明确拒绝，不截断问题、不循环重试。

只有成功完成检索但命中为空才属于 no_answer。索引不兼容返回
knowledge_index_incompatible，其他检索故障返回 knowledge_retrieval_failed，并结束为 failed。
错误保留在工具轨迹与轮次中，前端显示具体故障，不允许模型把系统故障解释成没有相关资料。
前置检索失败时尚未调用回答模型，因此追踪中只有工具阶段而没有 LLM 输出属于预期行为。

LangSmith 内容追踪默认关闭；在 Agent 实际加载的 env 文件中配置后重启 Agent：

```dotenv
LANGSMITH_CAPTURE_CONTENT=true
```

开启后，新追踪包含脱敏后的用户问题、提示词、工具输入输出、模型回答和异常详情。
可识别的 Bearer/JWT、API 密钥、签名、钱包地址、私钥和运行上下文仍会遮蔽，
配置中的模型和 LangSmith 密钥也会按实际值替换。资产数值与文档片段保留用于诊断，
因此这些内容会进入所配置的 LangSmith 项目；关闭此开关即回到仅元信息模式。
旧追踪缺失的输入输出不能补回，重启后新建的追踪才使用新设置。

本地 `.env` 已为此次调试开启内容追踪，样例仍默认 false。本次未重建或排队任何现有索引，
保留旧文档的版本状态及已有对话，方便复现原故障；这些应用改动无需数据库迁移。
