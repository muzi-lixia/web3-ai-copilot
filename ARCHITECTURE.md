# Web3 AI Copilot — 工程蓝图

## 文档地图

| 文档 | 回答什么问题 |
|---|---|
| `PLAN.md` | 做什么、不做什么、什么顺序（**策略**） |
| `IMPLEMENTATION.md` | 三个数据模块的细节（**实现补遗**） |
| `ARCHITECTURE.md`（本文） | 技术栈 / 目录 / 契约 / 逐功能实现 / 前后端结合（**工程蓝图**） |

---

## 1. 技术栈定版

### 前端

| 用途 | 选型 | 备注 |
|---|---|---|
| 框架 | React 19 + TypeScript | 对齐存量 |
| 构建 | Vite | |
| UI | antd 5 | |
| 服务端状态 | TanStack Query | 资产/行情/质押的缓存、loading、重试 |
| 客户端状态 | zustand | 当前地址、对话、Agent 轨迹 |
| 图表 | echarts + echarts-for-react | 饼图/时序 |
| 路由 | react-router-dom | |
| 请求 | axios | 普通查询 |
| 流式 | fetch + ReadableStream | **不能用 EventSource**，见 §5.4 |

### 后端

| 用途 | 选型 | 备注 |
|---|---|---|
| Web | FastAPI + uvicorn | |
| 校验 / 模型 | Pydantic v2 | **前后端契约的唯一真源** |
| 配置 | pydantic-settings | 读 `.env` |
| HTTP 客户端 | httpx（async） | 调 RPC 和 CoinGecko |
| 链交互 | web3.py | 处理 ABI 编解码 / checksum 地址 |
| 数据库 | SQLAlchemy 2.0 async + asyncpg + Alembic | V2 |
| 向量检索 | **PostgreSQL + pgvector 单库**（不引入独立向量库） | V2；选型理由与迁移路径见 §8 |
| 缓存 | 内存 dict（V1）→ Redis（V3） | |

### AI

| 用途 | V1 | V2+ |
|---|---|---|
| LLM SDK | `openai`（原生 tool calling） | 保留 + `langgraph` |
| 编排 | 手写路由 + 确定性函数 | LangGraph StateGraph |
| 结构化输出 | `response_format` / 强制 tool | 同 |
| 向量 | — | pgvector + embedding |
| 流式 | SSE | SSE |

> **V1 不引入 LangChain** 的理由：原生 SDK 的 tool calling 约 30 行，链路完全透明、可断点 debug。LangGraph 的价值在 V2 出现条件分支、检查点、多步依赖时才付费。

---

## 2. 目录结构（代码怎么分布）

```
web3-ai-copilot/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI 实例、CORS、路由挂载、异常处理器
│   │   ├── core/
│   │   │   ├── config.py            # Settings（pydantic-settings）
│   │   │   ├── logging.py           # 结构化日志
│   │   │   └── errors.py            # 统一异常类型 + handler
│   │   ├── api/                     # 路由层（薄：只做校验，逻辑全在 service）
│   │   │   ├── wallet.py
│   │   │   ├── market.py
│   │   │   ├── staking.py
│   │   │   ├── agent.py             # ⭐ 含 SSE 端点
│   │   │   └── knowledge.py
│   │   ├── schemas/                 # ⭐ 契约层：前后端都以此为准
│   │   │   ├── wallet.py
│   │   │   ├── market.py
│   │   │   ├── staking.py
│   │   │   ├── risk.py
│   │   │   ├── agent.py
│   │   │   └── events.py            # SSE 事件模型
│   │   ├── services/                # ⭐ 业务逻辑（不依赖 HTTP，可单测）
│   │   │   ├── chain.py             # RPC 客户端 / Multicall / 重试 / 故障转移
│   │   │   ├── wallet_service.py
│   │   │   ├── market_service.py    # 含 TTL 缓存与降级
│   │   │   ├── staking/
│   │   │   │   ├── base.py          # StakingProvider 抽象
│   │   │   │   ├── liquid.py        # Lido / Rocket Pool
│   │   │   │   └── custom.py        # 自有锁仓合约
│   │   │   ├── risk_service.py      # 确定性风险计算（不含 LLM）
│   │   │   └── agent_runner.py      # V1 编排（V2 迁入 agents/）
│   │   ├── tools/                   # Agent 工具层（薄壳，包装 service）
│   │   │   ├── specs.py             # tool 的 JSON Schema 定义
│   │   │   └── registry.py          # name → callable 注册表
│   │   ├── agents/                  # V2：LangGraph
│   │   │   ├── graph.py
│   │   │   ├── nodes.py
│   │   │   ├── state.py
│   │   │   └── prompts.py
│   │   ├── rag/                     # V2
│   │   │   ├── loader.py
│   │   │   ├── splitter.py
│   │   │   ├── embedder.py
│   │   │   ├── store.py             # ⭐ VectorStore 协议（换存储只改实现类）
│   │   │   ├── pg_store.py          # pgvector 实现
│   │   │   ├── retriever.py
│   │   │   └── chain.py
│   │   ├── db/                      # V2：engine / session / models
│   │   └── constants/
│   │       ├── tokens.py            # 候选 token 表：地址 / decimals / coingecko_id
│   │       └── protocols.py         # 协议标签表：合约地址 → 协议 / 类型 / 底层资产
│   ├── scripts/                     # 后端运维脚本（如 RAG 语料索引导入）
│   ├── tests/
│   ├── .env.example
│   └── pyproject.toml
│
├── frontend/
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx                  # 路由 + 全局布局
│   │   ├── api/                     # 与后端路由一一对应
│   │   │   ├── client.ts            # axios 实例 + 拦截器 + 统一错误
│   │   │   ├── wallet.ts
│   │   │   ├── market.ts
│   │   │   ├── staking.ts
│   │   │   ├── agent.ts             # ⭐ 含 SSE 封装
│   │   │   └── knowledge.ts
│   │   ├── types/                   # 与后端 schemas 对齐（可由 OpenAPI 生成）
│   │   ├── stores/
│   │   │   ├── walletStore.ts       # 当前地址
│   │   │   └── agentStore.ts        # ⭐ 对话消息 + Agent 轨迹
│   │   ├── hooks/
│   │   │   └── useAgentStream.ts    # ⭐ SSE 订阅 + 分帧解析
│   │   ├── views/
│   │   │   ├── Dashboard.tsx
│   │   │   ├── Portfolio.tsx
│   │   │   ├── Staking.tsx
│   │   │   ├── Copilot.tsx          # ⭐ 核心页
│   │   │   └── Knowledge.tsx
│   │   ├── components/
│   │   │   ├── WalletInput.tsx
│   │   │   ├── AssetCard.tsx
│   │   │   ├── AssetChart.tsx
│   │   │   ├── ChatMessage.tsx
│   │   │   └── AgentTrace.tsx       # ⭐ 执行过程可视化
│   │   └── router/index.tsx
│   ├── .env.local                   # VITE_API_BASE_URL
│   └── package.json
│
├── contracts/                       # ⭐ 独立合约工程（与 backend 完全分离）
│   ├── src/NodeStaking.sol          # 自有锁仓合约
│   ├── abi/NodeStaking.json         # 部署后导出；backend 读它做 ABI 解码
│   ├── scripts/deploy_staking.py    # Sepolia 部署脚本
│   ├── tests/
│   └── README.md
│
├── docs/                            # RAG 语料
│   ├── ethereum-staking.md
│   ├── lido.md
│   ├── uniswap.md
│   ├── aave.md
│   └── web3-faq.md
│
├── evaluation/                      # V3
│   ├── dataset.json
│   ├── agent_eval.py
│   └── rag_eval.py
│
├── docker-compose.yml               # V2（postgres，pgvector 镜像）
└── *.md
```

**分层原则**：`api`（薄）→ `services`（厚，纯业务）→ `chain`（I/O）。`tools/` 只是 services 的 Agent 侧薄壳，**不重复实现业务**。

**为什么合约工程独立在顶层**：`contracts/` 是 Solidity 工程，有自己的工具链（编译 / 测试 / 部署）和独立依赖，与 Python 后端**没有任何构建期耦合**。后端只在运行时通过一份导出的 ABI 读它——依赖的是 `abi/NodeStaking.json` 这个产物，不是合约源码：

```
contracts/src/*.sol  ──编译部署──>  链上合约实例
                            └──────>  contracts/abi/NodeStaking.json
                                              │ 运行时读取
                                              ↓
                                        backend/app/services/staking/custom.py

backend  ──✗ 构建期不依赖──>  contracts/src
```

好处是两边节奏完全独立：合约改一次、部署一次、导出 ABI，后端不动；后端迭代也不碰合约。合约源码放 `backend/` 里会让「Python 服务」和「Solidity 工程」共用一套忽略规则、依赖清单和 CI 配置，是纯粹的负担。

---

## 3. 核心数据模型（前后端契约）

后端的 `schemas/` 是唯一真源，前端 `types/` 与之一一对应。

```python
# schemas/wallet.py
class Asset(BaseModel):
    symbol: str
    contract: str | None          # None = 原生 ETH
    amount: Decimal
    decimals: int
    price_usd: Decimal | None
    value_usd: Decimal | None
    percentage: float | None
    kind: Literal["native", "erc20", "staking", "lending"]
    protocol: str | None          # Lido / Aave / None

class WalletAssets(BaseModel):
    address: str
    total_value_usd: Decimal
    assets: list[Asset]
    computed_at: datetime
    stale: bool = False

# schemas/market.py
class TokenMarket(BaseModel):
    symbol: str
    price_usd: Decimal
    change_24h: float | None
    market_cap: Decimal | None
    volume_24h: Decimal | None
    updated_at: datetime
    stale: bool = False

# schemas/staking.py
class StakingPosition(BaseModel):
    protocol: str
    provider: Literal["liquid", "custom"]     # 哪一类质押
    asset: str
    amount: Decimal
    underlying_amount: Decimal | None         # 换算成底层资产
    value_usd: Decimal | None
    apy: float | None
    reward_estimate: Decimal | None
    reward_basis: Literal["onchain_exact", "apy_estimate"]   # ⭐ 口径必须标注
    unlock_time: datetime | None
    unlock_note: str | None                   # 无锁定期 → 机制说明

# schemas/risk.py
class RiskReport(BaseModel):
    # —— 以下全部由代码计算，LLM 不得参与 ——
    top_asset: str
    top_asset_ratio: float
    concentration_score: float        # 0-100
    stablecoin_ratio: float
    volatile_ratio: float
    staking_ratio: float
    liquid_ratio: float
    risk_level: Literal["low", "medium", "high"]
    computed_at: datetime
    # —— LLM 只填这一项 ——
    explanation: str | None = None

# schemas/events.py — SSE 事件（Agent 可观测性的载体）
class RunStarted(BaseModel):   type: Literal["run_started"];    run_id: str; seq: int; intent: str
class ToolStarted(BaseModel):  type: Literal["tool_started"];   run_id: str; seq: int; tool: str; args: dict
class ToolFinished(BaseModel): type: Literal["tool_finished"];  run_id: str; seq: int; tool: str; ok: bool; duration_ms: int; result_digest: str
class TokenDelta(BaseModel):   type: Literal["token_delta"];    run_id: str; seq: int; text: str
class RunFinished(BaseModel):  type: Literal["run_finished"];   run_id: str; seq: int; answer: str; usage: dict
class RunFailed(BaseModel):    type: Literal["run_failed"];     run_id: str; seq: int; error: str; partial: bool
```

---

## 4. API 契约

统一前缀 `/api/v1`。成功直接返回数据，错误用 HTTP 状态码 + FastAPI `HTTPException`。

| 方法 | 路径 | 用途 | 版本 |
|---|---|---|---|
| GET | `/health` | 健康检查 | V1 |
| GET | `/wallet/{address}/assets` | 资产列表 | V1 |
| GET | `/market/token/{symbol}` | 单 token 行情 | V1 |
| GET | `/market/tokens?symbols=ETH,USDC` | 批量行情 | V1 |
| GET | `/staking/{address}/positions` | 质押仓位 | V1 |
| POST | `/agent/chat` | **SSE 流式对话** | V1 |
| POST | `/agent/analyze-portfolio` | 确定性风险分析 | V1 |
| POST | `/knowledge/query` | RAG 问答（带引用） | V2 |
| POST | `/knowledge/documents` | 上传文档 | V2 |
| GET | `/agent/runs/{run_id}` | 回放某次 Run | V3 |

### 4.1 SSE 契约

```
POST /api/v1/agent/chat
Content-Type: application/json

{ "message": "分析一下我的资产风险", "address": "0x...", "conversation_id": null }
```

响应 `text/event-stream`，事件序列形如：

```
event: run_started
data: {"run_id":"r_1","seq":1,"type":"run_started","intent":"risk_analysis"}

event: tool_started
data: {"run_id":"r_1","seq":2,"type":"tool_started","tool":"get_wallet_assets","args":{"address":"0x..."}}

event: tool_finished
data: {"run_id":"r_1","seq":3,"type":"tool_finished","tool":"get_wallet_assets","ok":true,"duration_ms":412,"result_digest":"6 assets · $23,482"}

event: token_delta
data: {"run_id":"r_1","seq":9,"type":"token_delta","text":"当前"}

event: run_finished
data: {"run_id":"r_1","seq":20,"type":"run_finished","answer":"...","usage":{"prompt_tokens":1680,"completion_tokens":210}}
```

**设计要点**：

- `seq` 单调递增，前端据此排序与补发，也是断线重连的依据。
- `result_digest` 只传摘要不传全量数据 —— 轨迹面板只需要"发生了什么"，不需要把资产列表再传一遍。
- `RunFailed.partial` 标记"部分数据缺失但结论可用"，对应失败降级策略。

---

## 5. 八个功能点怎么实现

### 5.1 钱包资产查询

```
GET /wallet/{address}/assets
 └─ api/wallet.py            校验地址（checksum 归一化）
 └─ services/wallet_service.py
     ├─ chain.py              Multicall 一次读：ETH balance + N 个 ERC-20 balanceOf
     ├─ constants/tokens.py   候选表：合约地址 / decimals / coingecko_id
     ├─ market_service.py     批量取价（命中缓存则不出网）
     └─ 计算 value_usd / percentage，过滤零余额，标记 dust
 └─ 返回 WalletAssets
```

关键：**decimals 从候选表读，不逐个调 `decimals()`**（省 N 次调用，且 decimals 不会变）。

### 5.2 Token 行情查询

```
GET /market/tokens?symbols=ETH,USDC
 └─ api/market.py
 └─ services/market_service.py
     ├─ 查内存 TTL 缓存（价格 15~30s，市值/量 60s）
     ├─ 未命中 → httpx 一次批量请求 CoinGecko simple/price
     ├─ 429 / 超时 → 返回旧缓存并置 stale=true（不抛错）
     └─ symbol → coingecko_id 映射表
 └─ 返回 TokenMarket[]
```

### 5.3 质押仓位分析

```
GET /staking/{address}/positions
 └─ api/staking.py
 └─ services/staking/（遍历已注册 provider，聚合结果）
     ├─ liquid.py   复用 wallet_service 的结果 → 按 protocols.py 标签筛出
     │              → 换算 underlying（stETH share 换算 / wstETH × stEthPerToken）
     │              → 取 APY（Lido API / Aave currentLiquidityRate）
     └─ custom.py   调自有合约 getPositions(address)
                    → ABI 解码 struct[] → 计算 pendingReward（链上精确）
 └─ 统一成 StakingPosition[]
```

关键：**liquid provider 不重复读链**，直接消费 `wallet_service` 的结果。

### 5.4 AI Copilot 对话

V1 用原生 tool calling，流程是"路由 + 双路径"：

```
POST /agent/chat  (StreamingResponse)
 └─ services/agent_runner.py
     Step 1  意图路由（一次 LLM 调用，结构化输出）
             → Intent{ name: risk_analysis | market_query | portfolio_query | knowledge_query | smalltalk }
     Step 2  分支
             ├─ risk_analysis → 确定性路径：按固定顺序调 tool，不让 LLM 选
             └─ 其他          → LLM tool calling 循环（最多 N 轮，防死循环）
     Step 3  每步 emit 一个 SSE 事件（run_started / tool_started / tool_finished）
     Step 4  最终回答：LLM 流式生成 → 逐个 token_delta
     Step 5  run_finished（带 usage）
```

意图路由用强制 structured output：

```python
class Intent(BaseModel):
    name: Literal["risk_analysis", "market_query", "portfolio_query", "knowledge_query", "smalltalk"]
    symbols: list[str] = []
    address: str | None = None
```

**为什么分两条路径**：确定性任务的顺序是已知的，交给 LLM 猜只会引入不确定性；开放式提问才需要模型自主选工具。这个分界就是 §1.3 那条架构判断的落地。

### 5.5 Web3 RAG 知识库（V2）

```
离线（索引）：
docs/*.md → loader（Markdown 解析，保留标题层级）
          → splitter（标题感知切分，非固定长度）
          → embedder（批量 embedding）
          → pgvector upsert（doc_chunks 表，列带 doc / section / chunk_id）

在线（问答）：
POST /knowledge/query
 └─ query rewrite（LLM 改写，可选）
 └─ retriever：向量 top-k + BM25 → RRF 融合
 └─ reranker 重排
 └─ 拼 prompt（每个 chunk 带编号）
 └─ LLM 生成 → 解析出引用编号
 └─ 置信度低于阈值 → 直接拒答
 → { answer, sources: [{doc, section, chunk_id, score}] }
```

### 5.6 资产风险分析

```
POST /agent/analyze-portfolio
 └─ 确定性步骤（全代码，不碰 LLM）：
     concentration_score = HHI 归一化(Σ weight_i²) → 0~100
     stablecoin_ratio    = Σ稳定币价值 / 总价值
     volatile_ratio      = Σ(非稳定币 且 非质押) / 总价值
     staking_ratio       = Σ质押价值 / 总价值
     liquid_ratio        = 1 - staking_ratio
     risk_level          = 阈值规则表映射（不是 LLM 判断）
 └─ RiskReport（metrics 部分完成）
 └─ LLM 只填 explanation，输入是上面的结构化指标
```

阈值规则表要写死在代码里，例如：`concentration_score >= 70 且 stablecoin_ratio < 0.2 → high`。**可解释、可测试、可复现**——这正是"不让 LLM 算数"的价值。

### 5.7 Agent 执行过程展示

```
后端：每个步骤 emit 事件（§4.1）+ 落库（V2）
前端：
  hooks/useAgentStream.ts   fetch + ReadableStream 手动分帧（按 \n\n 切）
                            → 按 type 分发到 agentStore
                            → 支持 AbortController 中止
  stores/agentStore.ts      messages[]（对话）+ trace[]（轨迹节点）
                            tool_started 建节点，tool_finished 补结果与耗时
  components/AgentTrace.tsx 时间轴渲染 trace：图标 / 工具名 / 耗时 / 结果摘要
```

**必须知道的坑**：`EventSource` **只支持 GET**，而聊天要 POST 传消息体。所以必须用 `fetch` + `ReadableStream` 手动解析 SSE 分帧。这是这个模块最容易卡住的地方。

### 5.8 工程化与评测

```
PostgreSQL（单库装全部）：
  conversations(id, title, created_at)
  messages(id, conversation_id, role, content, created_at)
  agent_runs(id, conversation_id, intent, status, started_at, finished_at, usage)
  agent_events(id, run_id, seq, type, payload, ts)      # ⭐ 可观测性的数据基座
  tool_calls(id, run_id, tool, args, result_digest, ok, duration_ms)
  documents(id, title, source_path, indexed_at)
  doc_chunks(id, document_id, section, ordinal, content, embedding vector(N), tsv tsvector)
      └─ 向量索引：USING hnsw (embedding vector_cosine_ops)
      └─ 全文索引：GIN(tsv)，承接混合检索的 BM25 一侧

Docker Compose：postgres（镜像 pgvector/pgvector:pg17，扩展已内置）

Evaluation：
  evaluation/dataset.json   30~50 条 {question, expected_tools[], expected_intent}
  agent_eval.py             跑 Agent → Tool Selection Accuracy / Success Rate / Latency / Token
  rag_eval.py               跑检索 → Recall@K / MRR / Faithfulness
  → 输出 JSON，前端 Evaluation 页渲染
```

---

## 6. 前后端怎么结合

### 6.1 契约先行

后端的 `schemas/` 是唯一真源。FastAPI 自动产出 OpenAPI，前端可用 `openapi-typescript` 生成 `types/`，**避免手工同步漂移**。

### 6.2 三条数据流

**流 1 · 同步查询**（Dashboard / Portfolio / Staking）

```
React 组件 → TanStack Query → api/*.ts（axios） → FastAPI api/ → services/ → 链 / CoinGecko
                          ←──────────── JSON（schemas 序列化）────────────┘
```

**流 2 · Agent 流式**（Copilot）

```
用户输入 → useAgentStream → fetch POST /agent/chat
   ←──── event-stream（逐事件）──── agentStore 更新 ──── AgentTrace / ChatMessage 重渲染
```

**流 3 · RAG 问答**（Knowledge）

```
问题 → POST /knowledge/query → 检索 + 生成 → { answer, sources } → 渲染回答与引用
```

### 6.3 状态管理分工

| 数据 | 归属 | 理由 |
|---|---|---|
| 资产 / 行情 / 质押 | TanStack Query | 服务端数据，需要缓存、失效、重试 |
| 当前钱包地址 | zustand | 纯客户端状态 |
| 对话消息 / Agent 轨迹 | zustand | 流式追加，不走查询缓存 |

**不要把服务端数据塞进 zustand** —— 这是他已经在用的 React 工程规范，保持一致。

### 6.4 本地联调

- 后端：`uvicorn app.main:app --reload` → `:8000`
- 前端：`vite` → `:5173`
- 在 `vite.config.ts` 配 `server.proxy` 把 `/api` 转发到 `:8000`，**开发期免 CORS**；生产用环境变量 + Nginx。

```ts
// vite.config.ts
server: { proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } } }
```

---

## 7. 实施顺序（文件级）

**Phase A · 后端数据层**（可用 Swagger / curl 独立验证）

1. `core/config.py` + `main.py` + `/health`
2. `schemas/` 全部模型
3. `constants/tokens.py` + `protocols.py`
4. `services/chain.py`（RPC + 单次读取）
5. `services/wallet_service.py` —— **先串行 → 再并发 → 最后 Multicall**（三个阶段都跑，留对比数据）
6. `services/market_service.py`（+ TTL 缓存）
7. `services/staking/*`
8. `services/risk_service.py`
9. `api/` 五个路由

**Phase B · 前端数据页**

10. 脚手架 + antd + 路由 + `api/client.ts`
11. Dashboard / Portfolio / Staking 三页

**Phase C · Agent（V1 收口）**

12. `tools/specs.py` + `registry.py`
13. `services/agent_runner.py`（原生 tool calling + 意图路由）
14. `api/agent.py` 的 `/analyze-portfolio`
15. 前端 Copilot 页 + `AgentTrace`（先非流式，验证链路）

**Phase D · V2**

16. SSE（后端事件流 + `useAgentStream`）
17. LangGraph 重写编排
18. RAG（loader → splitter → pgvector → retriever → rerank）
19. PostgreSQL（+ pgvector）+ Docker Compose
20. 锁仓合约（`contracts/` 独立工程：写 → 部署 Sepolia → 导出 ABI）+ backend 侧 `CustomContractProvider` 读 ABI

**Phase E · V3**

21. Evaluation（数据集 + 两个脚本 + 展示页）
22. MCP 抽离
23. 部署 + README + 架构图 + demo 录屏

---

## 8. 存储选型：为什么是 pgvector 单库

### 结论

**V2 用 PostgreSQL + pgvector 单库**，不引入独立向量数据库。到 V3 数据规模或检索需求真的越界时，再评估是否迁移到 Qdrant。

### 判断依据

知识库规模是 5 篇文档、几百到几千个 chunk。在这个量级上：

| 维度 | pgvector 单库 | PG + Qdrant 双库 |
|---|---|---|
| 运维对象 | 1 个容器 | 2 个容器 + 2 套备份 |
| 数据一致性 | 同一事务，删文档级联删干净 | 跨库无事务，需补偿/对账脚本 |
| 检索能力 | 向量近邻 + `tsvector` 全文，够用 | filter 下推、原生稀疏向量、量化 |
| 资源隔离 | 向量查询与业务查询共享 PG | 各自独立 |

**双库带来的复杂度是确定的，收益是理论上的。** 为几千个 chunk 付跨库一致性成本，不划算。

### 什么情况下这个结论翻转

三条具体信号，任一出现就重新评估：

1. **chunk 到百万级** —— HNSW 索引体积和构建时间开始拖累 PG 的备份与恢复。
2. **检索要带复杂 filter** —— pgvector 在 `WHERE` 高选择性过滤下，planner 常无法有效使用 HNSW，容易退化成顺序扫描；Qdrant 把 filter 下推进 HNSW 遍历，差距在真实查询里很明显。
3. **向量查询影响业务查询** —— `agent_runs` 这类高频小事务的 P95 被向量查询抢占 buffer pool / CPU 拖慢。

### 迁移成本控制

不让将来的迁移变成重写，现在就把边界划好：

```
rag/store.py          定义 VectorStore 协议（upsert / search / delete_by_doc）
rag/pg_store.py       pgvector 实现
rag/qdrant_store.py   V3 需要时新增，上层不动
```

`retriever.py` 只依赖 `VectorStore` 协议，不直接碰 SQL 或 pgvector API。**换存储时改一个实现类，检索链路不动。**

### 这块的设计要点

不是「我用了 pgvector」，而是：

> 知识库向量存储评估了 pgvector 与 Qdrant 两种方案。在几千 chunk 的规模下选择 pgvector 单库，避免跨库一致性与额外运维成本；通过 `VectorStore` 接口隔离实现，并在评测中记录数据量、过滤选择性与检索延迟三组指标，作为后续迁移决策的依据。

**「为什么现在不选 X」和「为什么将来会选 X」，比「我用了 X」更能撑住追问。**

### 附：如果最终仍要上 Qdrant

这份选型不锁死 Qdrant。你原计划明确列了 Qdrant，若你更看重「双存储」这项技术叙述，就按 `pg_store` + `qdrant_store` 同时实现、跑同数据集对比——**那次对比实验的结论本身就是产出**，而不是把它们都跑起来就完事。
