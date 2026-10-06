# 三个核心模块实现设计

数据源统一：**公共 RPC（读链）+ CoinGecko（读价）**，全部真实数据，无 Mock 层。

---

## 先厘清：项目里一共三类「地址」

「不需要写合约」和「不需要合约地址」是两件事 —— **不需要写（部署）合约，但需要一张合约地址表**。三类地址各管一件事：

### A. RPC 端点 —— 读链的「入口」

一个 URL，所有链上读取都发往它。**这是别人的基础设施，不用你自建。**

| 选项 | 地址 | 说明 |
|---|---|---|
| 公共节点（起步） | `https://eth.llamarpc.com` / `https://rpc.ankr.com/eth` / `https://cloudflare-eth.com` | 免注册、免 key，有速率限制，V1 够用 |
| Alchemy 免费版 | 注册后给你的专属 URL | 稳定、额度大，V2 建议换 |
| Infura 免费版 | 同上 | 备选 |

配在 `.env` 里：`ETH_RPC_URL=https://eth.llamarpc.com`。换节点只改这一个值。

### B. 合约地址 —— 「读哪个合约」

要读的目标合约**早就部署在以太坊主网上运行多年**，你只是调用方，不是部署方。起步表：

| 合约 | 用途 | 地址 |
|---|---|---|
| Multicall3 | 批量读取（落点 1 核心） | `0xcA11bde05977b3631167028862bE2a173976CA11` |
| USDC | ERC-20 余额（注意 decimals=6） | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` |
| USDT | ERC-20 余额（decimals=6） | `0xdAC17F958D2ee523a2206206994597C13D831ec7` |
| DAI | ERC-20 余额 | `0x6B175474E89094C44Da98b954EedeAC495271d0F` |
| WETH | ERC-20 余额 | `0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2` |
| LINK | ERC-20 余额 | `0x514910771AF9Ca656af840dff83E8264EcF986CA` |
| stETH | Lido 质押仓位 | `0xae7ab96520DE3A18E5e111B5EaAb095312D7fE84` |
| wstETH | Lido 质押仓位（份额型） | `0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0` |

地址来源（自己查证的三条路）：

1. **Etherscan** 搜合约名 → 看「Contract」页的地址和源码
2. **协议官方文档** 的 *Deployed Contracts* / *Addresses* 页（Lido、Aave 都有）
3. **官方 GitHub** 仓库里的 `deployment*.json` / `addresses.json`

> **落地硬规则：任何地址入库前必须自校验。** 对候选地址调 `symbol()`（选择器 `0x95d89b41`）和 `decimals()`（`0x313ce567`），返回值与预期一致才写入。上表地址请用这一步核一遍再使用 —— 地址错一个字符，整套结论都是错的。

**为什么不用自己写合约**：要读的全部是标准接口（`balanceOf` / `decimals` / `symbol`）和已上线协议（Lido、Aave、Multicall3）。你扮演的是调用者，不涉及部署、gas、私钥。

> **例外（重要）**：如果你要做「自有合约的节点/锁仓」这类质押，就**必须写并部署一个合约** —— 因为那种仓位记录在你自己的合约里，链上没有现成合约可读。这是全项目**唯一**需要写 Solidity 的地方，方案见模块 3 末尾。

### C. 你自己的服务地址 —— 前端要接的后端

| 环境 | 地址 |
|---|---|
| 本地开发 | `http://127.0.0.1:8000`（你自己跑的 FastAPI） |
| 部署后 | 平台分配的域名（Vercel / Railway / Fly.io），V3 再说 |

前端 `.env` 里配 `VITE_API_BASE_URL=http://127.0.0.1:8000`。

### D. 第三方 API 地址

| 服务 | Base URL |
|---|---|
| CoinGecko | `https://api.coingecko.com/api/v3` |
| Lido APR | 见 Lido 官方 API 文档 |

---

## 模块 1 · 钱包资产查询

### 输入输出

输入：`0x...` 地址。输出：`total_value` + `assets[]`（含 amount / price / value / percentage）+ 分布。

### 实现链路

**Step 1 — 读原生 ETH 余额**

```
eth_getBalance(address, "latest")  →  "0x2c68af0bb140000"
```

`int(hex_str, 16)` 得到 wei（**Python int 是任意精度，不要碰 float**）→ `/ 10**18` 得 ETH。

**Step 2 — 读 ERC-20 余额**

链上不存在"资产列表"调用，所以维护候选表，逐个调 `balanceOf`：

```
data = "0x70a08231" + address[2:].lower().rjust(64, "0")
eth_call({ to: token_contract, data }, "latest")  →  余额 hex
```

`0x70a08231` 是 `balanceOf(address)` 的 4 字节选择器。

**Step 3 — decimals 处理**

余额是整数，必须除 `10**decimals` 才是可读数量。两种做法：

- 候选表里**硬编码** decimals（USDC/USDT=6，DAI=18，WBTC=8，WETH=18）—— 省一次调用
- 或调 `decimals()`（选择器 `0x313ce567`）并**永久缓存**（decimals 不会变）

> USDC 的 6 位小数是最经典的踩坑点：不处理会显示成 `5000000000` 个 USDC。

**Step 4 — 批量读取（落点 1 的实现载体）**

三个阶段，本身就是一条优化曲线：

| 阶段 | 做法 | RPC 次数 |
|---|---|---|
| 朴素 | 串行逐个 `eth_call` | N+1 |
| 并发 | `asyncio.gather` 并发发出去 | N+1（但耗时压到 1 个 RTT） |
| **Multicall** | Multicall3 `aggregate3` 一次调用 | **1** |

Multicall3 部署在以太坊主网及 100+ 链的**同一地址**：`0xcA11bde05977b3631167028862bE2a173976CA11`。

把 N+1 次请求压成 1 次 —— 这就是"91 次压到 1 次"在链上场景的同一命题。三个阶段都实现一遍，横向对比 RPC 次数和耗时，这份对比表本身就是产出。

**Step 5 — 取价并计算**

CoinGecko 按**合约地址**查价，比按 symbol 查更稳（无同名歧义）：

```
GET /api/v3/simple/token_price/ethereum
    ?contract_addresses=0xA0b8...,0xdAC1...
    &vs_currencies=usd
```

计算：

```
value_i      = amount_i × price_i
total_value  = Σ value_i
percentage_i = value_i / total_value × 100
```

### 三个坑

1. **除零**：`total_value == 0`（空地址/无效地址）时必须短路，不要算占比。
2. **精度**：金额链路用 `Decimal` 或整数运算到最后一步；`float` 只在展示层用。
3. **灰尘过滤**：余额为 0 的 token 必须过滤；`value < $1` 的建议折叠标记为 dust，否则列表会被垃圾 token 淹没。

---

## 模块 2 · Token 行情查询

### 数据源：一个接口拿全四个字段

CoinGecko `simple/price` 支持一次性返回你需要的全部四项：

```
GET /api/v3/simple/price
    ?ids=ethereum,usd-coin,arbitrum
    &vs_currencies=usd
    &include_24hr_change=true
    &include_market_cap=true
    &include_24hr_vol=true
```

→ 价格、24h 涨跌、市值、24h 成交量，**一次请求全拿到**，不需要再加 `/coins/markets`。

### 关键：symbol → CoinGecko id 映射表

CoinGecko **不认 symbol**，只认自己的 id。必须维护映射：

```
ETH → ethereum     BTC → bitcoin      USDT → tether
USDC → usd-coin    ARB → arbitrum     LINK → chainlink
```

这是这个模块最主要的"手工维护成本"，也是它的稳定性来源 —— 比调 `/search` 动态解析可靠得多（同名 token 会解析错）。

### 给 Agent 用的形态

行情必须以 **Tool** 形式暴露，返回值是**结构化模型**而不是自然语言：

```json
{
  "symbol": "ETH",
  "price": 3200.5,
  "change_24h": -2.1,
  "market_cap": 386000000000,
  "volume_24h": 15000000000,
  "updated_at": "2026-10-06T09:00:00Z",
  "stale": false
}
```

LLM 只负责把结构化的数字组织成话 —— 数字永远来自接口，不来自模型。

### 缓存与降级（这个模块的深度在这里）

CoinGecko 免费版有限流，直接在 Agent 循环里裸调必然被 429 打爆。

- **TTL 缓存**：价格 15~30s，市值/成交量 60s。V1 用内存 dict + 时间戳，V2 换 Redis。
- **批量合并**：一次请求查多个 token，不要按 symbol 逐个查。
- **降级而非报错**：命中限流时返回上次缓存值并置 `stale: true`，让 LLM 在回答里说明"价格数据为 N 分钟前"。**功能降级优于功能中断**，这是 Agent 场景的硬要求 —— 工具挂了不能把整轮对话带崩。

---

## 模块 3 · 质押仓位分析

### 核心认知：质押凭证本身就是 ERC-20

stETH / wstETH / rETH / aToken 都是标准 ERC-20。所以「查询质押仓位」**不需要任何专门的质押接口** —— 它就是模块 1 资产列表的一个**分类问题**：给资产打上「协议归属」标签。

### 逐字段数据来源

这是模块 3 最容易被误解的地方。**逐字段拆开看，会发现没有任何一个字段来自"质押接口"**：

| 字段 | 来源类型 | 具体来源 |
|---|---|---|
| 仓位数量 | **链上读取** | `balanceOf(address)` 调 stETH / wstETH / aToken 合约 |
| 协议归属 | **自维护标签表** | 合约地址 → Lido / Aave |
| 底层资产 | **自维护标签表** | stETH → ETH |
| 仓位价值 | **链上余额 × CoinGecko 价格** | 直接复用模块 1 的结果 |
| APY | **外部接口 + 链上** | Lido 官方 API；Aave `currentLiquidityRate` |
| 收益 | **计算得出（非查询）** | `amount × APY`，口径必须标注 |
| 解锁时间 | **机制说明（非查询）** | 流动性质押 / Aave 均无锁定期 |
| 质押资产占比 | **计算得出** | Σ质押价值 ÷ 总资产价值 |

三类来源：**链上读取 / 自维护标签表 / 计算得出**。没有一个需要"质押协议提供接口"。

**精确换算的链上调用**（余额 ≠ 价值时用）：

- stETH 是 rebase 型，`balanceOf` 随收益增长 → 更精确用 `sharesOf(address)` + `getPooledEthByShares(shares)`
- wstETH 是份额型，余额固定 → `balanceOf() × stEthPerToken()` 换算成底层 ETH 数量

两者都返回**底层 ETH 数量**，这才是"我质押了多少 ETH"的准确答案。

### 协议识别表

落地时建一张表（`合约地址 → 协议 / 类型 / 底层资产`）：

| 凭证 | 协议 | 类型 | 底层 |
|---|---|---|---|
| stETH | Lido | 流动性质押（rebase） | ETH |
| wstETH | Lido | 流动性质押（份额） | ETH |
| rETH | Rocket Pool | 流动性质押 | ETH |
| aToken (aEthWETH / aEthUSDC…) | Aave V3 | 借贷存款 | 对应资产 |

> **地址必须校验后再入库**：对候选地址调 `symbol()`（选择器 `0x95d89b41`）和 `name()`（`0x06fdde03`），返回值对得上才写入。地址表错一个字符，结论全错 —— 这个自校验步骤本身也是工程细节。

### APY 从哪来

- **Lido**：走 Lido 公开 API 取最新 APR
- **Aave**：读 Aave V3 Pool 的 `getReserveData(asset).currentLiquidityRate`（ray 单位 `1e27`，除 `1e27` 得年化），这是链上真实值，比第三方数据可信

### 收益怎么算 —— 这里必须诚实

链上**拿不到"你什么时候买入的"**，所以「已赚取收益」不是能直接查到的数。两个可用口径：

| 口径 | 算法 | 说明 |
|---|---|---|
| 年化推算 | `amount × APY` | 标注"按当前 APY 推算" |
| 日收益推算 | `amount × APY / 365` | 同上 |

**不要假装能算出"累计已赚"**，除非去扫历史事件（成本高，不值得）。口径标注清楚比数字好看重要。

还有一个真实细节：**stETH 是 rebase 型（`balanceOf` 随收益增长），wstETH 是份额型（`balanceOf` 不变、单价增长）**。所以"余额"和"价值"的关系在这两类资产上完全不同，用一套公式算会错。

### 解锁时间 —— 大部分情况下它不是"查出来的"

原计划里有「Unlock Time」字段，但要认清现实：

| 类型 | 解锁时间 |
|---|---|
| 流动性质押（stETH / wstETH / rETH） | **无锁定期**，随时可在二级市场卖出 |
| Aave 存款 | **无锁定期**，随时可取 |
| Lido unstake 发起后 | 进入提款队列，数天级 |
| 原生质押（32 ETH validator） | 退出队列，天数级 |

也就是说，用户没发起 unstake 时，这个字段是**协议机制说明**，不是实时数据。诚实做法是返回"无锁定期"或"需先发起提现请求"这类机制描述，而不是编一个倒计时。

### 质押资产占比

```
staking_ratio = Σ 质押类资产价值 / 总资产价值
```

口径要写明：分母是**总资产**还是**可流动资产**，两者含义不同（后者更能反映流动性风险）。建议两个都给。

---

### 补充：自有合约的节点/锁仓（唯一需要写合约的部分）

**为什么必须写**：流动性质押的凭证是链上已有的 ERC-20（stETH…），你只读。而「自有合约锁仓」的仓位记录在**你自己的合约**里，链上没有现成合约可读 —— 所以必须写一个。

**方案：写最小锁仓合约，部署到 Sepolia 测试网**（真实链、Etherscan 可查、任何人都能自行验证）。

合约最小接口：

```solidity
struct Position {
    uint256 amount;      // 质押数量
    uint256 stakedAt;    // 质押时间（收益计算的依据）
    uint256 unlockTime;  // 解锁时间
    uint256 apyBps;      // APY，基点表示（500 = 5%）
}

function stake(uint256 amount, uint256 lockDays) external;
function unstake(uint256 index) external;                                        // 需到期
function pendingReward(address who, uint256 index) external view returns (uint256);
function getPositions(address who) external view returns (Position[] memory);
```

**一个反直觉的结论：这一类反而比 Lido 更"真实"。**

| 字段 | Lido 那类 | 自有合约 |
|---|---|---|
| 仓位数量 | 链上可读 | 链上可读 |
| APY | 外部 API / 协议合约 | 合约里存了，直接读 |
| **收益** | **只能按 APY 推算** | **可精确计算**（合约有 `stakedAt`） |
| **解锁时间** | **只能给机制说明** | **真实时间戳** |

因为合约自己记录了 `stakedAt` 和 `unlockTime`，这两项从"推算/机制说明"升级成了**真实数据**。这个对比本身就有技术说明价值。

**部署要点**：Sepolia 测试网 → 从 faucet 领免费测试 ETH → Foundry `forge create` 或 Remix 图形化部署 → 记下**合约地址 + ABI**（ABI 是读它的钥匙，必须存进项目）。

**读取要点**：`eth_call` 调 `getPositions(address)`，返回值是 **struct 数组，需要 ABI 解码** —— 比读 ERC-20 的单个 `uint256` 复杂，这是这一块真正的技术点。

**两类统一成 provider 抽象**（这是能把两类质押放在一个系统里的关键）：

```
StakingProvider（抽象接口）
├── LiquidStakingProvider   Lido / Rocket Pool → 读 ERC-20 + 协议标签表
└── CustomContractProvider  自有锁仓合约        → 读 ABI 方法 + 解码 struct
          ↓
  统一输出 StakingPosition { protocol, asset, amount, apy, reward, unlock_time, value }
```

Agent 侧只认 `StakingPosition`，不关心背后是哪个 provider。抽象层的价值就在这里，也是一处值得单独说明的设计。

---

## 三模块如何串给 Agent

模块 1 / 2 / 3 是数据层，Agent 侧把 1、3 直接封装成 Tool，2 封装成另一个 Tool：

```
get_wallet_assets(address)        → 模块 1，返回结构化资产列表
get_token_price(symbols[])        → 模块 2，返回结构化行情
get_staking_positions(address)    → 模块 3，内部复用模块 1 + 协议标签表 + 自有合约 provider
```

**关键：`get_staking_positions` 内部复用 `get_wallet_assets`，不要重复读链。** 一次 RPC 的结果喂三个模块，这既是性能设计，也是 Agent 里「工具不应重复取数」的基本要求。

风险分析（模块 6）依赖这三个的输出，且**计算全部由代码完成**，LLM 只做解释 —— 编排权不放给模型（见 PLAN.md §1.3）。
