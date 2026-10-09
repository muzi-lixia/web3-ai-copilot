# Web3 AI Copilot

当前提供钱包登录、Berachain 资产与行情查询、组合风险分析、质押仓位展示，以及本地 Ollama 单会话聊天。
聊天支持 SSE、历史持久化和摘要；每个用户的历史独立隔离，尚未接入实时资产工具或知识库。

## 后端目录与职责

后端按业务领域组织，完整结构与扩展规则见 [后端工程说明](backend/README.md)。

```text
backend/app/
  main.py           应用工厂与生命周期
  api/              HTTP/SSE、依赖注入、版本化路由
  core/             配置、异常、日志、服务装配
  modules/          auth、asset、market、risk、staking
  ai/               conversation、memory、llm
  infrastructure/   database、blockchain、providers
  common/           跨模块基础类型与纯工具
```

业务模块不依赖 AI 和 HTTP；未来 Tool Adapter 复用同一业务服务。当前只有普通聊天、摘要和持久化，不预建未实现的 Agent、RAG 或 Worker 空包。

## 本地启动

项目依赖 PostgreSQL 和 Ollama。首次准备：

```sh
docker compose up -d postgres
ollama pull qwen2.5:7b
cd backend
cp .env.example .env
uv sync
uv run python scripts/init_chat_db.py
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --workers 1
```

另一个终端启动前端：

```sh
cd frontend
cp .env.example .env
# 填写 VITE_REOWN_PROJECT_ID
npm ci
npm run dev
```

后端为 `http://127.0.0.1:8000`，接口文档为 `/docs`；前端为 `http://localhost:5173`。
Vite 将 `/api` 转发到后端。生产参数校验见 `.env.example`。
不使用 uv 时，可用 `pip install -r backend/requirements.txt` 安装已锁定的直接依赖。

## 验证与阅读

```sh
cd backend
uv run pytest -q
uv run ruff check app tests scripts
# 真 PostgreSQL 集成测试必须指向初始化好的专用测试库
RUN_CHAT_DB_TESTS=1 uv run pytest -q
# 真实 HTTP / PostgreSQL / Ollama，需使用没有其他实例占用的专用数据库
uv run python scripts/check_chat.py
# 修改链上常量后核对 RPC、Token、金库与数据源
uv run python scripts/verify_constants.py
```

前端在 `frontend` 中运行 `npm test`、`npm run lint`、`npm run build`。
先看 [文档索引](docs/README.md)，再沿 [代码阅读指南](docs/code-reading-guide.md) 追一条请求。

## 支持的链

目前只接入 **Berachain**（chain_id `80094`，原生币 BERA）。

链参数集中在 `backend/app/infrastructure/blockchain/chains.py`（chain_id / 原生币 / Multicall3 / 公共 RPC /
浏览器 / 两个行情源的链标识），各链读哪些 token 在 `backend/app/modules/asset/tokens.py`。
**新增一条链 = 各加一条条目**，读取层、服务层、路由都不用改。当前链由 `DEFAULT_CHAIN_ID`
决定，接口也支持按请求传 `chain_id`。

需要更稳或带 key 的专属节点时，用环境变量覆盖公共节点（不要把带 key 的 URL 写进代码）：

    RPC_OVERRIDES=80094=https://your-node.example.com

## 钱包登录

所有功能都需要先连接钱包。这是标准的「签名验明身份」，不涉及任何链上交易：

    前端                                后端
    连接钱包（Reown AppKit 弹窗）
    POST /auth/challenges              →     生成随机数，连同签名原文一起下发
    钱包对原文签名
    POST /auth/tokens             →     从签名反推地址，验过后签发 JWT
    后续请求带 Authorization: Bearer <token>

连接与签名分成两次点击：未连接时按钮是「连接钱包」（只弹钱包），连上后变成「签名并登录」。
钱包弹窗是异步的，用户可能切账号或直接关掉；把连接状态交给 wagmi 维护，
下一次点击时它自然是最新的，比在一个函数里等结果要少掉一整串分支。

三个关键实现点：

- **签名原文由后端下发，前端原样签。** 两端各拼一次模板，迟早会因为空格或换行差一个字符
  导致验签失败，而且极难排查。模板只有一个出处就没有这个问题。
- **nonce 一次性。** 验签前先删除，同一个签名提交第二次必然失败 —— 防重放的最省事做法。
- **进受保护路由时静默校验一次凭证。** token 在 sessionStorage 里，服务端可能已经不认了
  （过期 / 密钥轮换）。不验的话页面会完整显示，直到用户点了某个接口才吃到 401。

钱包连接用 **Reown AppKit**（wagmi 的原生适配层），支持浏览器插件与 WalletConnect 扫码
—— 后者是必须的：`injected()` 只能唤起插件钱包，手机打不开，等于把链接发给别人就打不开页面。
它需要一个 projectId（客户端公开标识，不是密钥）：

    cd frontend && cp .env.example .env
    # 填 VITE_REOWN_PROJECT_ID，免费创建：https://cloud.reown.com

普通 JSON 的以下业务字段均位于 `data`，完整响应和错误说明见 [接口契约](docs/api-contract.md)。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/challenges` | `{address}` → `{nonce, message, expires_at}` |
| POST | `/api/v1/auth/tokens` | `{address, signature}` → `{token, address, expires_at}` |
| GET | `/api/v1/users/me` | 需 Bearer token → `{address}` |

`JWT_SECRET` 本地可不设置（使用开发默认值）。生产设置 `ENVIRONMENT=production`、
`DEBUG=false`、至少 32 字节的独立随机密钥和实际 `SIWE_DOMAIN`；不满足时拒绝启动。
生成方式见 `backend/.env.example`。

> **为什么不用「请求头带钱包地址」当登录。** 那种做法（连接钱包后把地址放进
> `X-Wallet-Address`，后端据此查白名单）在内网管理员后台里够用，但地址是明文可任意伪造的，
> 等于没有鉴权 —— 而本项目是公开可访问的，且「证明私钥在手」正是 Web3 身份区别于
> 普通账号体系的地方。所以这里保留签名链路，只借用 AppKit 的连接体验。

## 钱包资产

按登录地址查余额（原生币走 `eth_getBalance`，ERC-20 走 Multicall3 一次批量 `balanceOf`），
并附上 USD 估值与占比（价格来自 Token 行情模块）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/wallets/{address}/assets` | 需 Bearer token，且 `address` 必须是登录地址 → 资产数组 + 总额 + 占比 |

三条设计约定：

- **返回全部候选条目（含余额为 0 的）。** "要不要看零余额"是展示偏好，属前端；
  后端过滤掉会让"余额为 0"和"这个币根本没查"无法区分。
- **金额一律是十进制字符串。** 链上金额是 bigint，走 JSON number 会掉精度，
  所以数量 / 单价 / 估值都序列化成字符串，前端只做显示裁剪、不当数字参与计算。
- **余额和估值的失败是分开的。** 行情源的可用性不归我们控制，所以行情全挂时
  资产接口**不会**变成 502 —— 它照常返回余额，只是 `has_valuation=false`、
  `missing_price` 列出没有报价的持仓、总额为 0。若只给总额一个字段，
  这种降级在界面上会显示成 `$0.00`，读起来就像「这个钱包是空的」。

## Token 行情

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/markets/quotes` | 需 Bearer token。可选 `chain_id` 与 `symbols`（逗号分隔）→ 行情数组 + `missing` |

两个免费源互补，都不需要注册：

| 源 | 提供 | 覆盖到 |
|---|---|---|
| DexScreener | 价格 / 24h 涨跌 / 市值 / 24h 成交量 | 有 DEX 流动性池的币 |
| DefiLlama | 只有价格 | 无 DEX 池的币（如 BGT） |

两者都按「链标识 + 合约地址」查询，**不需要维护任何第三方 coin id** ——
清单里加一行 `TokenMeta` 就自动有行情。同一合约地址可能部署在多条链上
（实测 USDT0 在 Mantle / Stable / Berachain 都有池子，且 Mantle 上的流动性高一个量级），
所以两个源都强制带链标识，避免读到别条链的价格。

**取不到行情不算失败。** 像 BVT 这样没有交易池的币，所有源都给不出报价，它会原样出现在
`missing` 里，而不是被当成价格为 0。每行都带 `source`，因为两个源的字段完整度不同：
DefiLlama 的行必然只有价格，涨跌与市值/成交量显示为 `—`。

原生币 BERA 没有合约可查，行情借用 WBERA（1:1 锚定）—— 登记在 `TokenMeta.price_address`。

后端缓存 60 秒（`MARKET_CACHE_TTL`）；上游失败时最多再使用 300 秒
（`MARKET_MAX_STALE_SECONDS`），并标为 `stale`。报价本身的时间也受两者之和约束。
超过期限的报价不参与估值。反算 quote token 时仅返回价格，不沿用 base token 的涨跌与市值；
FDV 不再替代流通市值。

## 资产风险

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/wallets/{address}/risk-report` | 需 Bearer token，且 `address` 必须是登录地址 → 风险报告 |

**数值全部由代码计算，LLM 不参与。** 模型生成的数字不可复现、不可测试，也解释不了它是怎么来的。
风险接口只返回确定性指标，未实现的模型解释字段已移除。

四个指标：

| 指标 | 口径 |
|---|---|
| 风险资产集中度 | **波动资产内部**的 HHI（Σ 份额²）归一化到 0-100 |
| 稳定币占比 | `risk_class="stable"` 的资产占比 |
| 高波动资产占比 | 稳定币的补集 —— 两类互斥且穷尽，两者之和恒为 1 |
| 质押占比 | 质押价值 ÷（质押 + 未质押资产） |

**口径包含质押仓位。** 风险口径是「钱包里的币 + 金库里的仓位」，两者都是持有。
质押那一行按**底层资产**分类（sWBERA 跟随 WBERA），所以它计入波动/稳定币比，而不是被漏掉。
一个 100% 质押 BERA 的钱包，`volatile_ratio` 就是 100%，不会因为「币不在钱包里」而算成 0。

于是有**两套各自完整、但回答不同问题的切分**，界面上也分组显示：

    稳定币 + 高波动 = 100%     按价格波动性切
    质押   + 可动用 = 100%     按能否即时动用切

不分组地摆一排，「四个数加起来不是 100%」会被当成漏算。

档位由三条判据的**命中条数**决定，不用加权求和（权重是拍脑袋的，而"命中几条"谁都能复核）：

    风险资产集中度 ≥ 70  ·  高波动资产 ≥ 80%  ·  稳定币 < 10%
    命中 0 / 1 / 2+ 条  →  低 / 中 / 高

两处刻意设计：

- **集中度只算波动资产。** 一个 100% 持有 USDT0 的钱包，对全部资产算 HHI 也是满分 ——
  指标显示「极度集中」，而它没有任何价值波动敞口，档位却会给成中/高风险，指标与档位自相矛盾。
  把稳定币从分子分母一起剔掉再归一，衡量的才是「风险有多集中」。
- **`staking_ratio` 的 null 与 0 是两回事。** 测过确实没质押 → `0`；
  **有仓位却取不到价**（或质押模块整个读不出来）→ `null`。后者界面显示「—」并说明原因。
  给 0 会让「没测到」读成「确实没质押」，方向上是错的。

## 质押仓位

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/wallets/{address}/staking-positions` | 需 Bearer token，且 `address` 必须是登录地址 → 仓位 + 年化 + 累计收益 + 提款队列 |

接入的是 **Berachain PoL v2 的 BERA 质押金库**（存 BERA / WBERA 得 sWBERA）。
它不是「委托给验证者」那套（那是 PoL v1）：收益来自 PoL 激励里 33% 的协议费用
回购成 WBERA 注入金库，**自动复利**。

**收益以「每份更值钱」发放，不是「发更多份额」。** 所以仓位有两个数量，缺一个就说不清：

| 字段 | 含义 |
|---|---|
| `shares` | 持有的份额数量（sWBERA），基本不动 |
| `underlying_amount` | 按当前汇率折算出的底层数量（WBERA），持续变大 |
| `exchange_rate` | 1 份份额值多少底层 —— **必须展示**，它是「我的仓位怎么自己变多了」的唯一解释 |

实测 `1 sWBERA ≈ 1.467 WBERA`。**直接拿份额当底层数量会低估约 32%，而且不报错**，
所以读取链路是 `balanceOf → convertToAssets`，一步都不能省。

**赎回是排队的，不是即时的。** 排队 → 等 7 天解绑 → 再手动完成。排队时份额
**就已经烧掉**（合约这么设计，防止解绑期内继续吃收益），所以它既不在 `balanceOf` 里、
也不在资产页余额里，只在 `pending_withdrawals` 里出现一次；锁定的数量在解绑期内**不再增长**。
它仍然计入质押价值 —— 解绑期一过就是可提取的资产，不会因为「正在排队」而从身家里消失。

数据来源分三层，可靠性不同，契约里也分开表达：

| 数据 | 来源 | 取不到时 |
|---|---|---|
| 份额 / 折算 / 提款队列 | 链上 Multicall3 直读 | 即抛 —— 没有它这个功能没有主体 |
| 累计收益 `total` / `realized` / `unrealized` | Beep | `null`（不是 0） |
| 区间年化 `apy` + `apy_interval` | Beep | `null` |

**年化必须带窗口。** `apy` 是 0-1 的**比值**（`0.0644` = 6.44%），`apy_interval` 说明它是
6 小时 / 24 小时 / 7 天里的哪个口径 —— 只说「6.4%」不说窗口，等于把最容易被误读的那个量藏起来。

**为什么用 Beep（`beep.berachain.com`）。** 它是 Berachain 官方的数据层
（Berachain Event Extraction Pipeline），BeraHub 页面上的 APR 与收益就出自这里 ——
所以这里的数就是页面上的那个数，不是我们另算的近似值。DexScreener / DefiLlama 给不出这个量：
它不是某个池子的报价，而是协议把激励回购后注入金库形成的收益。
调用要带 `X-Client-Id`（配置项 `BEEP_CLIENT_ID`），缺了会被直接拒（400）。

**这一页只展示，不提供质押/赎回操作。** 那些都需要用户签名交易，是钱包的事 ——
后台不持钥，也不该摆一个不签名的「操作」按钮出来。

质押凭证 sWBERA **故意不进 token 清单**：它若进了，资产页和质押页会各展示一次同一个仓位，
`total_value_usd` 也会重复计入，风险报告里的「质押占比」会算出大于 100%。
质押模块登记在 `backend/app/modules/staking/constants.py`，**加模块 = 加一条 `StakingModule`**。

## Ollama 模型配置

项目只使用 `backend/app/ai/llm/ollama.py`，对话与摘要共用原生 `/api/chat`。
配置统一由 Settings 读取：

| 参数 | 用途 |
|---|---|
| `COPILOT_OLLAMA_URL` | 本地 Ollama 地址 |
| `COPILOT_MODEL` | 默认 `qwen2.5:7b` |
| `COPILOT_TIMEOUT_SECONDS` | 推理与排队超时 |
| `COPILOT_CONTEXT_WINDOW` | 上下文窗口 |
| `COPILOT_OUTPUT_TOKENS` | 回答输出上限 |
| `COPILOT_SUMMARY_OUTPUT_TOKENS` | 摘要输出上限 |
| `COPILOT_CONTEXT_RESERVE` | 模板等安全余量 |

提示词集中在 `backend/app/ai/memory/prompts.py`。输入先检查预算，模型流缺少 done 时明确失败；
模型 HTTP 请求不走本机环境代理。四平台备用工厂、对应 key 和 MODEL_PROVIDER 配置已移除。
真实聊天与摘要连通性通过 `scripts/check_chat.py` 验证。

## 总览页

登录后的落地页（`/` 重定向到 `/dashboard`），把资产、风险、质押三块的**结论**聚在一屏，
明细去各自页面。它不新增接口，只复用 `/wallets/{address}/assets`、`/wallets/{address}/risk-report`
与 `/wallets/{address}/staking-positions`。

三个请求分开取、分开报错。余额来自自家 RPC，风险报告还要在余额之上再取一次行情，
质押又依赖另一套链上读取 + 第三方数据层 —— 三者失败原因和可恢复性都不同，
后者失败只是少了一格，不该把已经读到的总资产一起抹掉。

总览 / 资产 / 风险 / 质押四页用**同一个 queryKey**，react-query 按 key 共享缓存，
页间切换不重复请求。

质押那一格的三种状态是分开的：该链没接质押模块 → `$0.00`；有仓位且算得出价 → 金额；
有仓位但取不到价 → 「—」。合成一个「—」会把「确实没质押」和「没测到」混掉。

## 数据完整性与单进程部署

钱包、质押、风险响应增加 `status`（complete / partial / unavailable）与 `issues`。
余额读取失败时 `amount=null`；报价缺失或余额未知时只展示已知估值，前端明确标注部分数据。
钱包或质押估值不完整时，组合占比为 null，风险报告为 unknown，相关指标也为 null。
集中度按底层价格敞口归并 BERA / WBERA / sWBERA，仓位明细仍单列。
提款明细读取失败时整个模块视为不完整，不从总额中静默扣除；所有模块失败返回 502。

金额从链上整数精确转换；金额运算使用调用局部的 512 位 Decimal 精度，展示边界再舍入。
同一次钱包或金库读取固定区块高度；RPC 传输异常可切换节点，合约执行错误不盲目重试。
同一事件循环内并发的相同余额、报价和仓位读取会合并，单个调用取消不会取消其他调用。
风险读取不请求 Beep 年化与历史收益。

本阶段不使用 Redis，**部署只支持一个实例、一个 worker**：

    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

nonce 有 TTL、容量上限和每分钟清理，消费受线程锁保护。nonce / verify 共用每个客户端 IP
的每分钟额度（默认 30 次），超过返回 429。限流状态同样有容量上限与过期回收。
应用不自行信任 X-Forwarded-For；位于反向代理后时，需由服务器仅信任实际代理的转发头，
避免所有用户共用代理 IP 额度或客户端伪造 IP。
重启会使未完成的 nonce 登录失效；密钥不变时已签发 JWT 仍有效。
多个 worker / 多实例需要共享 nonce 存储，不能直接增加 worker 数。

离线回归：`cd backend && .venv/bin/python -m pytest -q`。
模型协议测试使用 HTTP 模拟传输；真实连通性由 `scripts/check_chat.py` 检查。

## 环境说明

**行情取不到价？** 行情源（DexScreener / DefiLlama）需要外网可达。若环境里的 `HTTP_PROXY`
指向的代理对这些域名不工作（实测表现为 SSL 断连或 502），在 `backend/.env` 里显式指定：

    MARKET_PROXY=http://127.0.0.1:7890

链上读取走公共 RPC，不受这一项影响。

**常量表核对。** `backend/app/config/constants/` 里的合约地址、decimals、行情源的链标识、
质押金库与解绑时长都是手工维护的。写错不会报错 —— 只会静默读到空、读到不相干的合约、
清单里所有币一起取不到价（会和"这些币本来就没有市场"混在一起），
或者让"还有几天能提"整个算错而日期看起来照样合理。
新增条目后必须跑一次核对，它会逐条调用链上的 `symbol()` / `decimals()`、校验节点 chain_id、
比对质押模块的底层资产与链上解绑时长，并实际打一次行情源与 Beep 的年化接口：

    cd backend && .venv/bin/python scripts/verify_constants.py

**已跑过一次（2026-10-07 实测）**：8 个合约地址全部有代码、decimals 逐条对上，质押模块四项全对，
行情源 8 个取到价（`BVT` 取不到是事实 —— 它没有交易池，不是故障）。只有两处 `symbol()` 与链上写法不同：

- `USDT0` → 合约写的是 `USD₮0`：用 U+20AE（蒙古图格里克符号）冒充字母 T，Tether 官方就这么写
- `HONEY` → 合约写的是 `BUSD`：自称 "Bera USD"，社区后来都叫 HONEY，合约没跟着改

这两处登记在 `TokenMeta.onchain_symbol`，是**把差异写下来**而不是把判据放松：`symbol` 是
展示与做键用的名字，`onchain_symbol` 是合约自称的名字，两者本来就允许不同。没登记的差异照旧报错 ——
否则「地址填成了另一个合约」这个错误就再也测不出来了，而它的症状恰好就是名称对不上。

## Copilot 持久化与摘要

启动与模型准备见前文“本地启动”。数据库默认监听本机 54329，持久化使用独立数据卷。

- 每个用户一个固定会话；会话、轮次、消息与版本化摘要持久化到 PostgreSQL，前端不保存会话选择、不提交历史。
  旧版 sessionStorage 对话不会被删除，但不会自动导入服务器。
- 打开即恢复本人历史，支持历史分页、SSE 流式回复、停止和最后一轮失败重试；无会话列表、新建、切换或改名入口。
- 用户身份只取 JWT，事务级身份 + PostgreSQL RLS 隔离；关联表通过复合外键保证归属一致。
- 正文约每 1 秒保存一次快照，结束与取消时立即保存。SSE 实时传全量正文快照，前端按消息 ID/版本替换，
  断线自动重连。切页/刷新不会停止生成；停止按钮使用独立取消接口。
- 服务重启将遗留运行标为 interrupted，保留已提交的正文快照；未提交的正文可能丢失；数据库写入失败时不能承诺只丢失一个周期。
- 同一会话最多一轮生成，网络重发用同一幂等 ID；失败重试关联原问题，不重复写用户消息。
- 上下文由后端组装：系统提示 + 带出处的结构化摘要 + 摘要未覆盖的完整轮次 + 当前问题。
- 默认保留最近 6 轮原文，约 7,000 输入 token 估算时触发摘要，输入目标不超过 10,000。
  每 5 次增量摘要周期从原文分批重建；摘要失败保留旧版本，不推进覆盖范围，按完整轮次裁剪兜底。
- 原始消息不会因摘要被删除。失败/取消的半段回答可查看，但不进入后续模型上下文。
- 记忆只属于本人固定会话，不跨用户共享；尚未接入链上查询工具或知识库。
- 显式流错误会查询轮次状态，并提供“恢复对话”；旧实例检测到数据库运行锁失效后停止调度，需重启服务。
- 旧多会话数据保留，固定使用最近创建的非空会话，其他旧历史不合并、不删除；详见 [单会话说明](docs/single-session-chat.md)。

应用使用 `DATABASE_URL`（app_rw）；迁移使用 `DATABASE_MIGRATION_URL`（app_ddl），
开发默认口令见 `.env.example`。生产必须替换全部默认口令，数据库不得公网暴露。
初始化脚本支持 `CHAT_DB_ADMIN_URL`、`CHAT_DDL_PASSWORD`、`CHAT_RW_PASSWORD`；
这些管理员/迁移凭据只用于初始化命令，不需要给应用授予管理员权限。

数据库通过 advisory lock 强制一个实例、一个 worker；启动的受限恢复函数只允许将遗留运行标记中断。
数据库停机时应用无法提供对话；已有会话保存在数据卷中。`docker compose down` 不删除数据卷，
不要使用 `down -v`，除非明确要清空数据。

模型配置：`COPILOT_OLLAMA_URL`（默认 http://127.0.0.1:11434）、`COPILOT_MODEL`（qwen2.5:7b）、
`COPILOT_TIMEOUT_SECONDS`（180）。其他摘要预算与快照参数见 `.env.example`。
本地模型请求不走环境代理。

SSE 反向代理应关闭响应缓冲（Nginx `proxy_buffering off`）与压缩，读超时大于 15 秒心跳间隔。

验证命令见前文“验证与阅读”；真实模型脚本必须使用没有其他实例占用的专用数据库。

### 后端 HTTP 契约变更

当前后端不保留旧接口，资源 URL、会话 PUT、轮次重试/取消和普通响应表示已更新，详见 [接口契约](docs/api-contract.md)。前端已同步适配新路径和 code/msg/data；SSE 事件载荷保持独立协议。
