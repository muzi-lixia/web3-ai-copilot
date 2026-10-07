/**
 * 与后端 `app/schemas/` 一一对应的类型定义。
 *
 * ⚠️ 金额字段的序列化约定：后端用 Python `Decimal` 表示金额，Pydantic 序列化为 JSON
 * **字符串**（如 `"23482"`）而不是 number —— 链上金额是 bigint，JS 的 float64 会在
 * 大数上掉精度。因此下列字段类型写 `string`：展示时可直接渲染，参与计算前请先转
 * `BigInt` 或十进制库。
 *
 * V1 手工维护；V2 起可用 openapi-typescript 从 `/api/v1/openapi.json` 生成，避免手工漂移。
 */

/* ── common ────────────────────────────────────────────── */

export interface ErrorBody {
  code: string
  message: string
}

/** 后端统一错误响应体，见 backend/app/core/errors.py */
export interface ApiErrorBody {
  error: ErrorBody
}

/* ── wallet ────────────────────────────────────────────── */

export type AssetKind = 'native' | 'erc20'

export interface Asset {
  symbol: string
  /** ERC-20 合约地址；null 表示该链原生币 */
  contract: string | null
  amount: string
  decimals: number
  kind: AssetKind
  /** 单价；null 表示该币在所有行情源上都取不到价 */
  price_usd: string | null
  /** amount × price_usd；null 表示无价可算 */
  value_usd: string | null
  /**
   * 占**有价资产总额**的百分比（0-100）。
   *
   * 无价资产为 null 而非 0 —— 「占比 0%」和「算不出来」是两回事，
   * 后者必须显示成「—」。
   */
  percentage: number | null
}

export interface WalletAssets {
  /** EIP-55 checksum 格式 */
  address: string
  chain_id: number
  chain_name: string
  /** 区块浏览器根地址，拼合约链接用 */
  explorer: string
  /** 按 token 清单顺序，含余额为 0 的条目 */
  assets: Asset[]
  /** **有价部分**的合计；无价资产不计入 */
  total_value_usd: string
  /**
   * 是否至少有一个非零持仓取到了价格。
   *
   * 为 false 时 total_value_usd 没有意义（它是 "0"，但不代表钱包是空的）——
   * 界面必须显示「估值不可用」，不能显示「$0.00」。
   */
  has_valuation: boolean
  /** **持仓中**没有行情的 symbol；零余额的币不列 */
  missing_price: string[]
  /** 行情降级到过期缓存时为 true */
  stale: boolean
  /** ISO 8601 */
  computed_at: string
}

/* ── market ────────────────────────────────────────────── */

export type MarketSource = 'dexscreener' | 'defillama'

export interface TokenMarket {
  symbol: string
  price_usd: string
  /** 百分比，-2.1 表示 -2.1% */
  change_24h: number | null
  market_cap: string | null
  volume_24h: string | null
  /**
   * 这一行取自哪个源。
   *
   * 两个源的字段完整度不同：DefiLlama 只给价格，涨跌/市值/成交量必为 null。
   * 界面要能据此解释「这个币为什么只有价格」，而不是让人以为是取数失败。
   */
  source: MarketSource
  updated_at: string
  stale: boolean
}

export interface TokenMarketList {
  chain_id: number
  chain_name: string
  tokens: TokenMarket[]
  /**
   * 取不到行情的 symbol。
   *
   * 两种来源：请求了但不在候选清单里的（拼错的），以及清单里有、却在全部
   * 行情源上都没有报价的（如没有交易池的 BVT）。后者是市场事实，不是故障 ——
   * 它会被原样列出来，而不是被当成价格为 0。
   */
  missing: string[]
}

/* ── staking ───────────────────────────────────────────── */

/** 地址在金库上的**累计**收益。三个数回答三个不同问题，合成一个就说不清了。 */
export interface StakingEarnings {
  total: string
  /** 已落袋：随赎回一起结算的部分 */
  realized: string
  /** 仍押在金库里的浮盈 */
  unrealized: string
}

/**
 * 一笔排队中的赎回。
 *
 * 份额在排队时**就已经烧掉**（合约这么设计，防止排队期间继续吃收益），
 * 所以它既不在 `shares` 里、也不在资产页余额里 —— 只在这里出现一次。
 * `assets` 是发起时锁定的数量，解绑期内**不会再增长**。
 */
export interface PendingWithdrawal {
  request_id: number
  assets: string
  shares: string
  value_usd: string | null
  /** ISO 8601 */
  requested_at: string
  /** ISO 8601 */
  unlock_at: string
  /** 是否已过解绑期、可以完成提取 */
  ready: boolean
  receiver: string
}

export interface StakingPosition {
  module_key: string
  name: string
  /** 收益来源 —— 用户看到的年化是「谁」给的，比年化是多少更要紧 */
  protocol: string
  share_symbol: string
  /** 份额数量。它**不是**底层资产数量 */
  shares: string
  underlying_symbol: string
  /** 按当前汇率折算出的底层数量 */
  underlying_amount: string
  /**
   * 1 份份额值多少底层。**必须展示** —— 它是「份额涨价」这个收益形式的唯一解释。
   * sWBERA 与 WBERA 不是 1:1，不显示这个数，用户会以为自己的仓位凭空变多了。
   */
  exchange_rate: string | null
  price_usd: string | null
  /** (份额折算量 + 排队中的锁定量) × 价格 */
  value_usd: string | null
  /** 0-1 的**比值**（0.0644 = 6.44%），不是百分数；取不到时为 null */
  apy: number | null
  /** 年化窗口（ONE_DAY / SEVEN_DAYS / …）；与 apy 同进同退 */
  apy_interval: string | null
  earnings: StakingEarnings | null
  pending_withdrawals: PendingWithdrawal[]
  unbonding_seconds: number
}

export interface StakingSummary {
  address: string
  chain_id: number
  chain_name: string
  explorer: string
  positions: StakingPosition[]
  /** **有价部分**的合计 */
  staked_value_usd: string
  /** 为 false 时 staked_value_usd 没有意义，界面要显示「估值不可用」而不是 $0.00 */
  has_valuation: boolean
  /** 有仓位却取不到价的底层资产 symbol */
  missing_price: string[]
  stale: boolean
  /** 未质押资产的价值合计（占比分母的另一半）；资产接口失败时为 null */
  liquid_value_usd: string | null
  /**
   * 质押价值 ÷（质押 + 资产），0-1。
   *
   * null 有两种含义：资产侧取不到，或整个组合价值为 0 ——
   * 两种情况下这个比值都没有定义。它是**不像 0%** 的。
   */
  portfolio_ratio: number | null
  /** ISO 8601 */
  computed_at: string
}

/* ── risk ──────────────────────────────────────────────── */

export type RiskLevel = 'low' | 'medium' | 'high' | 'unknown'

/** 除 explanation 外全部由后端代码计算，LLM 不参与 —— 数值可复现、可测试。 */
export interface RiskReport {
  /** 估值占比最高的资产；无可估值持仓时为 null */
  top_asset: string | null
  /** 0-1 */
  top_asset_ratio: number | null
  /** **波动资产内部**的集中度 0-100（HHI 归一化）；剔除稳定币后重新归一 */
  concentration_score: number
  /** 0-1 */
  stablecoin_ratio: number
  /** 0-1，高波动资产占比 */
  volatile_ratio: number
  /**
   * 0-1，质押价值占**整个组合**（质押 + 未质押资产）的比例。
   *
   * 有仓位却取不到价时为 null —— 那种情况下它是一个取不到的 0，不是真的没质押。
   */
  staking_ratio: number | null
  /** 0-1，可即时动用部分；与 staking_ratio 同进同退，两者之和为 1 */
  liquid_ratio: number | null
  /**
   * `unknown` 不是第三种风险，而是**输入不足**（没有可估值的持仓）——
   * 此时任何档位都是编的，界面必须显示空态而不是"低风险"。
   */
  risk_level: RiskLevel
  computed_at: string
  /** 由 LLM 生成；失败为 null，不影响数据可用 */
  explanation: string | null
}

/* ── events（SSE，Agent 可观测性载体）──────────────────── */

interface AgentEventBase {
  run_id: string
  /** 单调递增，前端据此排序与补发 */
  seq: number
}

export interface RunStarted extends AgentEventBase {
  type: 'run_started'
  intent: string
}

export interface ToolStarted extends AgentEventBase {
  type: 'tool_started'
  tool: string
  args: Record<string, unknown>
}

export interface ToolFinished extends AgentEventBase {
  type: 'tool_finished'
  tool: string
  ok: boolean
  duration_ms: number
  /** 人类可读摘要，不传全量数据 */
  result_digest: string
}

export interface TokenDelta extends AgentEventBase {
  type: 'token_delta'
  text: string
}

export interface RunFinished extends AgentEventBase {
  type: 'run_finished'
  answer: string
  usage: Record<string, unknown>
}

export interface RunFailed extends AgentEventBase {
  type: 'run_failed'
  error: string
  /** true = 部分数据源失败但结论仍可用 */
  partial: boolean
}

export type AgentEvent =
  | RunStarted
  | ToolStarted
  | ToolFinished
  | TokenDelta
  | RunFinished
  | RunFailed

/* ── agent ─────────────────────────────────────────────── */

export interface ChatRequest {
  message: string
  address?: string | null
  conversation_id?: string | null
}

export interface AnalyzePortfolioRequest {
  address: string
}

export interface AnalyzePortfolioResponse {
  address: string
  report: RiskReport
}

/* ── health ────────────────────────────────────────────── */

export interface HealthResponse {
  status: string
  chain_id: number
}

/* ── auth（钱包登录）───────────────────────────────────── */

export interface NonceRequest {
  address: string
}

export interface NonceResponse {
  address: string
  nonce: string
  /**
   * 要签名的完整原文。
   *
   * ⚠️ 由后端下发，前端**原样**签名，不要自己拼模板 ——
   * 两端各拼一次必然产生空格/换行差异，表现为验签莫名失败。
   */
  message: string
  expires_at: string
}

export interface VerifyRequest {
  address: string
  /** 0x + 130 位十六进制 */
  signature: string
}

export interface TokenResponse {
  token: string
  address: string
  expires_at: string
}

export interface MeResponse {
  address: string
}
