# Web3 AI Copilot — 可执行方案

> 输入：原《Web3 AI Copilot 项目开发计划》（54 节）
> 依据：既有的真实工程存量（4 个 React 前端工程 + 1 个 Web3 全栈工程，约 12.9 万行）

---

## 0. 先说结论

原计划**方向正确**（先单 Agent 后多 Agent、确定性计算 + LLM 解释、V1 不上 MCP），但有三处结构性问题：

1. **战线太长。** 12 个线性 Phase，前 4 个全是基础设施（Mock → 真实数据 → Tool → LangGraph），做完才见到第一个 AI 成果。中途没有可演示产物，极易烂尾。
2. **把最强的牌打成了 Mock。** 你做过一个 L1/L2 节点销售全栈工程：Aave V3 接入、Safe 多签、事件索引器、ERC-1155 合约、222 个合约测试。Aave / Lido 的业务模型你比绝大多数 AI 应用开发者清楚。计划里质押数据全 Mock、不接真实协议，等于主动放弃了唯一的差异化。
3. **前端选型与你的存量能力错位。** 你本地 4 个前端项目全是 `Vite + React 19 + TS + antd + zustand`，计划却写 Vue3。

**改法：把 12 个 Phase 压成 3 个可独立演示的版本，先做最小 AI 闭环，Web3 真实性后置但必须做。**

---

## 1. 三个必须先定的决策

### 1.1 前端用 React，不用 Vue3（除非 Vue 才是你的主武器）

- 依据：既有的 4 个前端项目 100% 是 Vite + React 19 + TypeScript + antd + zustand + axios，合计约 4.4 万行。React 是你的存量。
- 这个项目的稀缺性是 **Agent / RAG / 流式**，前端框架本身不产生加分。用最熟的那套，把学习成本留给 Python。
- 顺手复用：既有工程里成熟的请求层设计、组件与 i18n 约定，可以近乎直接迁移到新项目 `src/api/`。
- **若你 Vue 更熟就换回 Vue，其余方案不变。**

### 1.2 V1 彻底不要 LangChain / 向量检索 / PostgreSQL / MCP

| 原计划 | 改成 | 理由 |
|---|---|---|
| Phase 3 就引入 LangChain | **V1 只用 `openai` SDK 原生 Tool Calling** | 代码少一个数量级，调用链路完全透明，出问题能 debug |
| V1 目录里就有 Qdrant | V1 不引入向量检索；V2 用 **PostgreSQL + pgvector 单库** | 少一个容器，且向量与业务数据同库同事务 |
| Phase 8 才有 PostgreSQL | 提到 V2 末尾 | V1 对话存内存/JSON 即可 |
| Phase 10 MCP | 只在 V3、确实需要时才做 | 它是"解耦展示"，不是能力 |

LangChain / LangGraph 的价值在 **V2** 才付费——当你真的需要多步编排、条件分支、检查点的时候。V1 硬套反而不如 30 行原生 SDK 清楚。

### 1.3 风险分析不要交给 Agent 自主编排，要做成确定性子图

原计划第 5 节的链路 `get_wallet_assets → get_token_price → get_staking_positions → calculate_portfolio_risk → LLM` 是**固定顺序**，不是"自主选择工具"。让 LLM 去猜这个顺序，只会引入不确定性。正确做法：

```
用户问题
  → 路由（LLM 判断属于哪类意图，这一步才需要 LLM）
  → 风险分析分支：并行取 3 个数据源（deterministic）
                 → 代码计算指标（deterministic）
                 → LLM 只负责写解释
```

这也正好是你原计划坚持的原则（"不让 LLM 直接计算"），只是要把编排权也一起从 LLM 手里拿走。

**留一个例外给 LLM**：开放式提问（"我最近资产怎么样？"）走 Agent 自主 Tool Calling；结构化任务（"分析风险"）走确定性图。两条路径并存，本身也是架构上值得单独说明的判断。

---

## 2. 三个版本与验收标准

### V1「最小 AI 闭环」— 唯一必须做出来的东西

**目标**：跑通 `自然语言 → 工具调用 → 结构化数据 → LLM 解释`，前端能看到工具调用轨迹。

**范围（只做这些）**
- 后端：FastAPI + 5 个 tool + `openai` SDK 原生 function calling
- 前端：React + 单页 Copilot（左侧对话 + 右侧 Agent 轨迹）
- 数据：**全部真实**——ETH / ERC-20 余额走 RPC + Multicall；行情走 CoinGecko；质押走 stETH / aToken 合约读取
- 存储：内存

**验收**：输入「分析一下我的资产风险」，右侧依次点亮工具调用，左侧流式输出风险解释。

**内容量**：后端 ~500 行，前端 ~400 行。这是 3~5 个工作日量级的编码量（不含环境折腾）。

#### 真实数据的两个真问题（必须先定，否则第一天就卡）

**1. 链上不存"某地址持有哪些 token"。** 没有任何一个链上调用能返回资产列表。两条路：

- **自维护候选 token 列表**（ETH / USDC / USDT / ARB / LINK / stETH / aToken…）→ 逐个 `balanceOf` → **Multicall 合成一次 RPC**。零外部依赖，而且正好是落点 1 的实现载体。**V1 走这条。**
- **第三方索引 API**（Alchemy `alchemy_getTokenBalances` / Covalent）一次拿全量 token。省事，但引入外部依赖和额度限制。**V2 作为增强。**

**2. 质押仓位不需要 Mock。** stETH、rETH、aToken 本身就是 ERC-20——只要建一张「协议合约地址 → 协议标识」的识别表，就能从余额里识别出质押仓位；APY 从 Lido / 协议公开接口取。**真正的缺口只有"解锁时间"**（unstake 队列），V1 留空即可。

**唯一前提**：真实数据的代价是「演示地址必须有内容」。需要预先准备 2~3 个已知持有资产与质押仓位的公开地址作为演示样本，否则查询结果是一片空——那不是 bug，是地址没东西。

---

### V2「Agent 化 + 知识库 + 真实数据」

- **LangGraph 重写编排**：State / Node / ConditionalEdge / ToolNode，把 V1 的顺序编排升成图；`get_graph().draw_mermaid()` 直接贴进 README
- **RAG**：5 篇协议文档 → chunk → embedding → 检索 → 带引用的回答
- **真实数据深化**：第三方索引 API 做全量 token 发现（Alchemy / Covalent）；多 RPC 故障转移；`bigint` + decimals 精度处理；扩展更多协议（Rocket Pool / Aave 借贷仓位）
- **SSE 流式**：token 流 + 工具执行事件流，前端实时渲染
- **风险指标确定性计算**：集中度、稳定币比例、质押比例、波动资产比例

**验收**：能演示 LangGraph 图、带来源引用的知识问答、流式轨迹、真实钱包余额。

**内容量**：2 周左右，大头在 RAG 调优和真实数据接入。

---

### V3「工程化 + 差异化」— 按需挑，不是全做

优先级从高到低：

1. **连接钱包自动分析**（推荐必做，见 §5）
2. PostgreSQL 持久化（Conversation / Message / Agent Run / Tool Call）
3. Docker Compose 一键起 + README + 架构图 + 录屏
4. Evaluation（30~50 条 + Tool Accuracy / Recall@K / Faithfulness）
5. MCP 抽离（确有需要时再做）
6. Tracing / CI

---

### 深度落点（决定这个项目的技术含量）

功能数量不产生深度。**能经得起"为什么这么设计、怎么衡量好坏、出错怎么办"追问的地方，才叫深度。** 全部资源集中在这五处，其余功能浅做即可：

**落点 1 · 链上读取的批量与容错 — 延续你「91 次请求压到 1 次」的能力**

玩具做法：查 N 个 token 就发 N 次 RPC，一个节点配到底。
深度做法：
- **Multicall 批量读取** —— 一次 RPC 拿到 N 个 token 余额，把 N+1 次请求压成 1 次。这与"减少冗余请求"这条优化主线是同一个命题，只是换到了链上场景。
- 多 RPC 故障转移（节点超时自动切换）、结果分层缓存（价格 TTL / 余额按 block 缓存）、`bigint` + decimals 精度处理（不要用 JS number 碰金额）。
- 证明方式：一张"优化前 N+1 次 RPC / 优化后 1 次"的对比。

**落点 2 · RAG 的检索质量可度量 — 把"能检索"做成"检索得准"**

玩具做法：切分 + 向量库 + top-k。
深度做法：
- 切分策略对比（固定长度 vs 标题感知）→ 用实验数据选，不是拍脑袋。
- 混合检索：BM25 + 向量 + RRF 融合，再加 rerank。
- **引用溯源** —— 回答里每个论断标注来源 chunk；检索置信度低时**主动拒答**（"知识库中没有相关内容"），而不是硬编。
- 证明方式：调优前后 Recall@K / MRR / Faithfulness 的对比表。**有数字的调优过程才是工程，没有就是调库。**

**落点 3 · Agent 的可靠性与失败恢复**

玩具做法：LLM 自由决定调哪个 tool，错了就重试。
深度做法：
- 混合编排（见 §1.3）：确定性任务走子图，不交给 LLM 猜顺序。
- **参数 schema 校验拦截** —— LLM 生成错误工具名或非法参数时，在调进业务代码之前拦下并回灌纠错。
- 部分失败降级：某个数据源超时，仍给出可用结论并**显式声明缺失**，不整体崩。
- 超时预算、工具调用幂等。
- 证明方式：Tool Selection Accuracy 指标 + 一组"故意让 RPC 超时"的容错演示。

**落点 4 · Agent 可观测性 — 全链路追踪与回放**

玩具做法：`console.log`。
深度做法：每次 Agent Run 落库完整链路（识别出的意图 → 工具序列 → 每次调用的入参/出参/耗时/token → 最终答案），前端时间轴可视化并可回放历史 Run。

这一条同时是两处加分：数据层是真工程，可视化是你的前端主场。**"构建了 Agent 可观测性体系"本身就是一项独立的工程亮点。**

**落点 5 · 确定性计算与 LLM 的职责边界**

风险指标全部由代码算（集中度 HHI、稳定币比例、质押比例、波动资产比例），LLM 只做意图路由和自然语言解释。这个边界感是当前 AI 应用工程最看重也最容易做错的地方——**能讲清"哪部分绝不能交给模型"，比能讲清"我用了什么框架"值钱。**

**自检标准**：如果某个功能只能被问到"你用了什么库"，那是浅的；如果能被问到"为什么这么设计、怎么衡量、出问题怎么办"，那是深的。五处落点都必须经得起持续追问。

---

## 3. V1 执行清单

### Step 0 — 环境准备

V1 的依赖极少，先看清什么需要、什么**不**需要：

| 依赖 | 本机状态 | 处理 |
|---|---|---|
| node 22 / npm | ✅ 已装 | 前端直接用 npm |
| python3 | ✅ 已装 | 后端 |
| uv | ❌ 未装 | `curl -LsSf https://astral.sh/uv/install.sh \| sh`；不想装就退回 `python3 -m venv` + `pip` |
| 以太坊 RPC | 用公共节点起步 | 量大后再注册 Alchemy / Infura 免费额度 |
| CoinGecko | 免 key | 直接用 |

**本机没有 Docker——但 V1 不需要。** PostgreSQL（含 pgvector 扩展）到 V2 才引入，届时再定路线（装 Docker Desktop / 用云免费层 / 本地原生安装）。V1 不要提前为它做任何准备，避免第一天耗在基础设施上。

### Step 1 — 后端骨架

```bash
mkdir -p backend && cd backend
uv init . && uv add fastapi "uvicorn[standard]" openai pydantic-settings httpx
uv run uvicorn app.main:app --reload
```

验收：`GET /health` 返回 200。

### Step 2 — 三个 service + 5 个 tool

```
tools/wallet.py    get_wallet_assets(address)      → RPC + Multicall 读 ETH/ERC-20 真实余额
tools/market.py    get_token_price(symbol)         → CoinGecko 真实接口
tools/staking.py   get_staking_positions(address)  → 读 stETH / aToken 合约，识别协议归属
tools/risk.py      calculate_portfolio_risk(...)   → 纯代码计算，不碰 LLM
```

关键：**tool 的返回必须是 Pydantic 模型**，不是自由文本。这是"结构化输出"的地基。

### Step 3 — 原生 Tool Calling

用 `openai` SDK 的 `tools=[...]` + `tool_calls` 循环。不引入 LangChain。先让「ETH 多少钱」走通「模型返回 tool_call → 本地执行 → 结果回灌 → 模型出答案」。

### Step 4 — 前端 Copilot 页

```bash
npm create vite@latest frontend -- --template react-ts
npm i antd zustand axios echarts
```

两栏布局：左对话、右 Agent 轨迹。V1 可以先用普通 POST + 一次性返回，SSE 留到 V2。

### Step 5 — 风险分析（确定性版）

前端点「分析我的资产风险」→ 后端走确定性编排（不用 LLM 选工具）→ 返回指标 + LLM 解释。

到这一步 V1 结束，可以录一个 60 秒 demo。

---

## 4. 具体数据源（不用猜）

| 用途 | 方案 | 备注 |
|---|---|---|
| Token 行情 | `https://api.coingecko.com/api/v3/simple/price?ids=ethereum,arbitrum&vs_currencies=usd&include_24hr_change=true` | 免 key，有速率限制，V1 够用 |
| ETH / ERC-20 余额 | 公共 RPC + `eth_getBalance` / `eth_call` 调 `balanceOf`，**用 Multicall 合成单次请求** | 只做 Ethereum 主网；Multicall 是落点 1 的实现载体 |
| 资产发现（该地址有哪些 token） | V1：自维护候选列表 + Multicall；V2：Alchemy `alchemy_getTokenBalances` | 链上不存在"资产列表"这个调用 |
| Aave 仓位 | Aave V3 Subgraph 或公开 REST API | 你接过 Aave V3，业务字段不用重新学 |
| Lido 仓位 | 直接读合约：`stETH.balanceOf()`、`getPooledEthByShares()` | 比找第三方 API 更快 |
| 知识库文档 | Ethereum Staking / Lido / Uniswap / Aave / Web3 FAQ 各 1 篇 | 5 篇高质量就够，**不要贪多** |

---

## 5. 一个高价值的具体建议：连接钱包

原计划是"用户手输 `0x1234...`"。但你手上有 `wagmi` + `viem` + Reown AppKit 的存量代码（4 个前端项目都在用）。

**改成：点「连接钱包」→ 自动读到用户自己的地址 → Agent 直接分析他本人的真实资产。**

这一步的收益：demo 从"演示一个查询工具"变成"用户看到自己的钱"——演示效果完全不同，而实现成本对你几乎为零。放到 V2 做，一天内能接上。

---

## 6. 风险与止损

| 风险 | 止损 |
|---|---|
| Phase 2「真实链上数据」卡住 | V1 用 Mock，别让真实数据阻塞闭环 |
| RAG 检索质量调不好 | 只要 5 篇文档；先做 baseline，V2 再加 Query Rewrite / Reranker |
| Python 不熟导致进度崩 | 围绕项目学（已写在原计划第 43 节，这条判断是对的，照做） |
| 想同时做 Multi-Agent / MCP / Evaluation | 全部推到 V3，没有 V1 的这三个都没意义 |

---

## 7. 核心亮点（把 8 条压到 4 条）

原计划第 52 节的 8 条亮点信息密度低、互相重叠。压成 4 条，每条对应一个能当场演示的东西：

1. 基于 **LangGraph** 构建状态化 Agent 工作流，实现意图路由、工具调用与**确定性风险计算子图**的混合编排——LLM 负责判断与解释，数值计算全部由代码完成。
2. 将钱包资产、Token 行情、质押仓位、风险指标封装为 **Agent Tools**，通过 Tool Calling 实现自然语言驱动的多步骤数据获取与分析。
3. 基于 **RAG** 构建 Web3 协议知识库（Ethereum / Lido / Uniswap / Aave），回答带来源引用。
4. **FastAPI + SSE** 流式输出 Agent 执行过程，前端实时可视化 Tool Calling 轨迹；接入真实链上数据（Ethereum RPC / Aave / Lido）。

**注意**：第 4 条里"接入真实链上数据"必须真的做，否则整个项目会退化成"调了个 LLM 的 CRUD"。

---

## 8. 与原计划的差异总览

| 项 | 原计划 | 本方案 |
|---|---|---|
| 阶段划分 | 12 个 Phase 线性推进 | 3 个可独立演示的版本 |
| 前端 | Vue3 | React（对齐存量） |
| V1 AI 层 | LangChain | 原生 `openai` SDK |
| V1 存储 | PostgreSQL + Qdrant | 内存 |
| 向量存储 | PG + Qdrant 双库 | **PG + pgvector 单库**（迁移路径见 `ARCHITECTURE.md` §8） |
| 质押数据 | 长期 Mock | V2 接真实 Aave / Lido |
| 钱包输入 | 手输地址 | wagmi 连接钱包（V2） |
| 风险编排 | Agent 自主选工具 | 确定性子图 + 路由 |
| 工程化亮点 | 未体现 | 复用请求层 / 缓存 / 首屏优化的存量能力 |

---

## 9. 模块覆盖矩阵

原计划的 6 个模块，加上你补充的「Agent 执行过程展示」与「工程化与评测」，共 8 个。**全部在范围内**，差异只在版本归属——V1 不引入 PostgreSQL / pgvector，是**后置**不是砍掉。

| # | 模块 | V1 | V2 | V3 |
|---|---|---|---|---|
| 1 | 钱包资产查询 | **真实 RPC + Multicall** | 全量 token 发现（索引 API） | 多链支持 |
| 2 | Token 行情查询 | CoinGecko 真实数据 | + TTL 缓存 / 限流退避 / 多源降级 | — |
| 3 | 质押仓位分析 | **真实读 stETH / aToken** | Aave 借贷仓位 / APY 口径 | 多协议扩展 |
| 4 | AI Copilot 对话 | 原生 Tool Calling + 意图路由 | **LangGraph** 重写编排 | 多轮记忆 |
| 5 | Web3 RAG 知识库 | — | Loader→切分→**pgvector**→检索→引用溯源 | 混合检索 + rerank + 拒答 |
| 6 | 资产风险分析 | 代码计算指标 + LLM 解释 | 指标扩充 + 解释质量 | 历史趋势 |
| 7 | Agent 执行过程展示 | 一次性返回完整轨迹 | **SSE 流式** + 时间轴 | 历史 Run 回放 |
| 8 | 工程化与评测 | — | PostgreSQL（+pgvector）+ Docker | Evaluation + MCP + 向量库迁移评估 |

### 一个结构性观察

8 个模块里，**前 6 个是业务功能，后 2 个是横切能力**。而项目的深度主要由后 2 个决定：

- 「Agent 执行过程展示」= **可观测性**
- 「工程化与评测」= **可度量性**

**资源分配原则：前 6 个模块做到"能用"即可，后 2 个必须做到"能撑住追问"。** 这也是为什么 V1 只做前 6 个的最小版本——它存在的唯一目的是让后 2 个模块有东西可挂。

### 各模块的「玩具 vs 深度」分界

| # | 模块 | 玩具做法 | 深度做法（要落到的线） |
|---|---|---|---|
| 1 | 钱包资产 | 每个 token 发一次 RPC | Multicall 一次拿全 + 多节点故障转移 + 分层缓存 + bigint 精度 |
| 2 | Token 行情 | 直接透传三方接口 | TTL 缓存 + 限流退避 + 多源降级（单源挂不等于功能挂） |
| 3 | 质押仓位 | 永远返回固定 Mock 数 | 真实读合约，处理 shares↔assets 换算与 APY 口径 |
| 4 | Copilot | LLM 自由选工具 | 意图路由 + 确定性/自主双路径 + 参数 schema 校验拦截 |
| 5 | RAG | 向量 top-k 直接塞 prompt | 切分策略对比实验 + 混合检索 + rerank + 引用溯源 + 低置信拒答 |
| 6 | 风险分析 | 让 LLM 直接估风险等级 | 全指标代码计算，LLM 只负责解释——职责边界不可越 |
| 7 | 执行过程展示 | `console.log` 打日志 | 每次 Run 全链路落库（意图/工具序列/入参出参/耗时/token）+ 时间轴回放 |
| 8 | 工程化评测 | 服务能跑起来就行 | 评测数据集 + 指标对比表（Tool Accuracy / Recall@K / Faithfulness） |

每一行的"深度做法"都对应 §2 的五处落点，不是额外要求，是同一件事的两种说法。
