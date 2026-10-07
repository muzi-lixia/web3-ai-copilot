# Web3 AI Copilot

基于 AI Agent 的链上资产分析与知识助手。用自然语言完成钱包资产查询、Token 行情、质押仓位分析、
资产风险分析，以及 Web3 协议知识问答。

## 目录

    backend/      FastAPI + 链上数据 + Agent + RAG
      app/
        api/
          routes/     HTTP 路由（auth / wallet / market / risk / staking）
          schemas/    接口数据契约（含预留 Agent / SSE 契约）
          deps.py     认证依赖
          exception_handlers.py  HTTP 异常映射
        services/     业务用例、估值与风险计算
        agents/       Agent 工作流（预留）
        tools/        业务工具适配（预留）
        llm/          模型客户端（client.py）
        rag/          索引与检索（预留）
        memory/       对话上下文管理（预留）
        repositories/ 持久化访问（预留）
        models/       数据库实体（预留）
        infra/
          blockchain/ RPC、Multicall、质押合约读取
          integrations/ Beep 数据源适配
          logging.py  基础日志
        guardrails/   模型与工具内容检查（预留）
        observability/ 执行追踪与指标（预留）
        workers/      后台任务入口（预留）
        config/
          settings.py 环境配置
          constants/  链、Token、质押模块注册表
        shared/       不依赖 HTTP 的业务异常
      scripts/        模型连通与常量核对脚本
      tests/          业务计算、模型配置与 HTTP 集成测试
    frontend/     React 19 + Vite + antd
      src/
        api/          与后端路由一一对应，client.ts 统一请求与错误
        types/        与后端 schemas 对齐
        stores/       zustand：当前地址、对话、Agent 轨迹
        hooks/        useAgentStream（SSE 分帧）
        views/        Dashboard / Portfolio / Market / Risk / Staking / Copilot / Knowledge
        components/   AppLayout / DistributionBar / RiskBadge / …
        utils/        format.ts —— 展示层格式化（纯字符串，不经过 number）
    contracts/    独立 Solidity 工程（尚未开始；与 backend 分离，仅通过 ABI 对接）
    docs/         RAG 语料
    evaluation/   Agent & RAG 评测（V3）

## 后端分层约定

- HTTP 入口在 `api/routes`；现有 URL、请求字段和响应字段保持不变。
- `services` 承载业务用例与确定性计算，复用 `api/schemas` 中的数据契约。
  Schema 仅定义数据，不依赖路由或请求对象；数据库实体单独放在 `models`。
- RPC 与合约读取在 `infra/blockchain`，Beep 在 `infra/integrations`。
  行情服务目前仍包含报价来源适配及缓存，后续可随业务迭代拆分。
- 业务异常在 `shared/errors.py`，HTTP 错误响应统一由 `api/exception_handlers.py` 转换。
- 页面路由与未来 Agent 工具复用同一套业务服务；工具不经内部 HTTP 再调用本服务。
- 后续聊天服务负责会话与运行生命周期，Agent 负责模型与工具编排；
  `memory` 负责上下文裁剪与摘要，消息持久化由 Repository 负责。
- `config/constants` 保留分文件注册表，避免将链、Token 与质押配置合并成一个大文件。
- 标记为“预留”的包当前只有职责说明，尚无数据库、队列、Agent 或 RAG 实现。
  本次目录迁移不代表这些业务能力已经完成。

## 本地启动

后端跑在 `:8000`：

    cd backend
    cp .env.example .env
    uv sync
    uv run uvicorn app.main:app --reload

不用 uv 的话走 `requirements.txt`（10 个直接依赖，版本锁死；传递依赖交给 pip 解析）：

    pip install -r requirements.txt
    uvicorn app.main:app --reload

前端跑在 `:5173`，`/api` 由 vite proxy 转发到 `:8000`：

    cd frontend
    cp .env.example .env    # 填 VITE_REOWN_PROJECT_ID，否则钱包弹窗打不开
    npm install
    npm run dev

验证：

- 后端：`curl http://127.0.0.1:8000/health` → `{"status":"ok","chain_id":80094}`
- 接口文档：http://127.0.0.1:8000/docs
- 前端：http://localhost:5173 → 登录后落在总览页；侧边栏可切到资产 / 行情 / 风险 / 质押
  （这四页的数据已接通），Copilot / Knowledge 仍是占位
- 模型出口：`cd backend && .venv/bin/python scripts/check_model.py` —— 实打一次，
  确认真的连得上、模型名没拼错、真能出字。**对话功能写之前先过这一关**：
  模型配置的错误大多是静默的，等到提问时才暴露会先被怀疑成业务代码写错了
- 常量表：`cd backend && .venv/bin/python scripts/verify_constants.py`

## 支持的链

目前只接入 **Berachain**（chain_id `80094`，原生币 BERA）。

链参数集中在 `backend/app/config/constants/chains.py`（chain_id / 原生币 / Multicall3 / 公共 RPC /
浏览器 / 两个行情源的链标识），各链读哪些 token 在 `backend/app/config/constants/tokens.py`。
**新增一条链 = 各加一条条目**，读取层、服务层、路由都不用改。当前链由 `DEFAULT_CHAIN_ID`
决定，接口也支持按请求传 `chain_id`。

需要更稳或带 key 的专属节点时，用环境变量覆盖公共节点（不要把带 key 的 URL 写进代码）：

    RPC_OVERRIDES=80094=https://your-node.example.com

## 钱包登录

所有功能都需要先连接钱包。这是标准的「签名验明身份」，不涉及任何链上交易：

    前端                                后端
    连接钱包（Reown AppKit 弹窗）
    POST /auth/nonce              →     生成随机数，连同签名原文一起下发
    钱包对原文签名
    POST /auth/verify             →     从签名反推地址，验过后签发 JWT
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

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/nonce` | `{address}` → `{nonce, message, expires_at}` |
| POST | `/api/v1/auth/verify` | `{address, signature}` → `{token, address, expires_at}` |
| GET | `/api/v1/auth/me` | 需 Bearer token → `{address}` |

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
| GET | `/api/v1/wallet/{address}/assets` | 需 Bearer token，且 `address` 必须是登录地址 → 资产数组 + 总额 + 占比 |

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
| GET | `/api/v1/market/quotes` | 需 Bearer token。可选 `chain_id` 与 `symbols`（逗号分隔）→ 行情数组 + `missing` |

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
| GET | `/api/v1/risk/{address}/report` | 需 Bearer token，且 `address` 必须是登录地址 → 风险报告 |

**数值全部由代码计算，LLM 不参与。** 模型生成的数字不可复现、不可测试，也解释不了它是怎么来的。
自然语言解读（`explanation`）留给 LLM 模块，现在恒为 null。

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
| GET | `/api/v1/staking/{address}/positions` | 需 Bearer token，且 `address` 必须是登录地址 → 仓位 + 年化 + 累计收益 + 提款队列 |

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
质押模块登记在 `backend/app/config/constants/staking.py`，**加模块 = 加一条 `StakingModule`**。

## 模型出口（可切换）

对话能力还没接（见「进度」），模型出口已经封好了 —— `backend/app/llm/client.py`。
**换平台只改一个环境变量**，加一个平台就是在 `PLATFORMS` 里加一行。

| `MODEL_PROVIDER` | 默认模型 | 需要 key | key 的环境变量 |
|---|---|---|---|
| `ollama`（默认） | `qwen2.5:7b` | 否 | —（本地，不出网） |
| `deepseek` | `deepseek-chat` | 是 | `DEEPSEEK_API_KEY` |
| `qwen` | `qwen-plus` | 是 | `DASHSCOPE_API_KEY` |
| `openai` | `gpt-4o-mini` | 是 | `OPENAI_API_KEY` |

    from app.llm.client import get_client

    llm = get_client()                                     # 按 MODEL_PROVIDER 选
    resp = await llm.ainvoke([{"role": "user", "content": "你好"}])

用 LangChain 的 `ChatOpenAI`。四家都提供 OpenAI 兼容的 `/chat/completions`，所以切平台就是换
`base_url` 与 key。模型名、`temperature`、`max_tokens` 都绑在 client 上，取一次到处用；
其余参数走 `**kwargs` 透传 —— `get_client(temperature=0.2, max_tokens=1024)`。

有一个坑要记：**LangChain 里字段名拼错不报错** —— 只给一条 warning 就把参数塞进 `model_kwargs`，
参数等于没生效。超时字段叫 `request_timeout` 而**不是** `timeout`。有单测钉住「传进去 = 读得到」。

配完先实打一次 —— 配置错误的症状大多是静默的（key 填错只在真正提问时才 401，
模型名拼错只会得到空回复）：

    cd backend && .venv/bin/python scripts/check_model.py

它做两件事：`models.list()` 证明连通、核对模型名在不在列表里；再发一句最小请求证明真能出字。
**已实测（2026-10-07，本机 Ollama）**：`ollama list` 只有 `bge-m3`（向量模型，不能对话）
与 `deepseek-r1:7b`，后者连通、能对话。要用本地模型先 `ollama pull qwen2.5:7b`。
另外留一条给 ⑤ 的记录：**`deepseek-r1` 这类推理模型收到 `tools` 会当没看见** ——
不报错、直接用自己印象回答，接工具链路时不能拿它当脑子。

## 总览页

登录后的落地页（`/` 重定向到 `/dashboard`），把资产、风险、质押三块的**结论**聚在一屏，
明细去各自页面。它不新增接口，只复用 `/wallet/{address}/assets`、`/risk/{address}/report`
与 `/staking/{address}/positions`。

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
模型配置单测隔离本机代理与模型环境变量；真实连通性仍由 `scripts/check_model.py` 检查。

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
