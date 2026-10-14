/**
 * 与后端基础业务契约及 Agent 展示快照对应的类型定义。
 *
 * ⚠️ 金额字段的序列化约定：后端用 Python `Decimal` 表示金额，Pydantic 序列化为 JSON
 * **字符串**（如 `"23482"`）而不是 number —— 链上金额是 bigint，JS 的 float64 会在
 * 大数上掉精度。因此下列字段类型写 `string`：展示时可直接渲染，参与计算前请先转
 * `BigInt` 或十进制库。
 *
 * V1 手工维护；V2 起可用 openapi-typescript 从 `/api/v1/openapi.json` 生成，避免手工漂移。
 */

/* ── common ────────────────────────────────────────────── */

export interface DataQuality {
    status: 'complete' | 'partial' | 'unavailable'
    issues: { code: string; message: string; asset: string | null }[]
}

/** 普通 HTTP 响应：成功 code=0，失败是非零整数；HTTP 状态码保持真实语义。 */
export interface ApiResponse<T> {
    code: number
    msg: string
    data: T
}

export interface ValidationIssue {
    location: (string | number)[]
    message: string
    code: string
}

/** 错误同样采用 code/msg/data，校验详情位于 data.errors；不使用旧 error 包装。 */
export type ApiErrorBody = ApiResponse<{ errors: ValidationIssue[] } | null>

/* ── wallet ────────────────────────────────────────────── */

export type AssetKind = 'native' | 'erc20'

export interface Asset {
    symbol: string
    /** ERC-20 合约地址；null 表示该链原生币 */
    contract: string | null
    amount: string | null
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

export interface WalletAssets extends DataQuality {
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

/* ── chat（持久化正文快照契约）────────────────────────── */

/** 本人固定会话的内部标识，前端不提供会话列表或切换。 */
export interface ChatSession {
    id: string
}

/* Result：一次回答里随消息下发的业务结果。信封（所有 kind 通用）+ body（按 kind 判别）。 */

/** 结果种类；加新类型要动三处：这里、body、前端 SourceDetail 的分发（漏了会落进兜底渲染）。 */
export type ResultKind = 'balance' | 'price' | 'aggregate' | 'knowledge'
export type ResultStatus = 'complete' | 'partial' | 'failed'

/** 信封：只放跨类型字段，业务数据一律在 body 里。 */
export interface ResultEnvelope {
    result_id: string
    kind: string
    status: ResultStatus
    queriedAt: string
    currency?: string | null
    /** 数据来源（源名 + 时间）；余额结果暂未按源记录，行情结果有。 */
    sources?: { name: string | null; at?: string | null }[]
    /** 失败项；目前 type 只有 chain、ref 即链 ID；按 type 分派是给将来别的失败物留位。 */
    failed?: { type: string; ref: number | string }[]
    /** 后端组装的来源描述，不是模型生成、也不该在前端拼。 */
    source_desc?: string | null
}

/** 余额类 body：链级明细 + 合计 + 未计价。 */
export interface BalanceBody {
    scope: string
    chains: Omit<WalletAssets, 'address'>[]
    total_value: string
    total_value_usd?: string
    unpriced: { chain_id: number; symbol: string; contract: string | null }[]
    exchange_rate_date?: string
}

/** 公开行情报价（/markets/prices 的返回体，也是 price body 的 token 字段）。 */
export interface MarketQuote {
    symbol: string
    chain_id: number
    currency: string
    price: string
    market_cap?: string | null
    volume_24h?: string | null
    change_24h?: number | null
    change_7d?: string | null
    updated_at?: string
    source?: string
    stale?: boolean
    exchange_rate_date?: string | null
    queriedAt?: string
}

/** 行情类 body。 */
export interface PriceBody {
    chain_id: number
    token: MarketQuote
}

export interface BalanceResult extends ResultEnvelope {
    kind: 'balance'
    body: BalanceBody
}

export interface PriceResult extends ResultEnvelope {
    kind: 'price'
    body: PriceBody
}

/**
 * 一条知识库命中片段。
 *
 * 出处与原文一起下发：用户要能自己核对「模型说的是不是这份文档里的原话」，
 * 所以 document_id + 标题路径 + 页码 + 相似度都留着，不在前端另算。
 */
export interface KnowledgeHit {
    /** 引用编号，与模型正文里写的 [1][2] 同源；两边由后端一次分配 */
    index: number
    document_id: string
    /** 片在文档内的序号，与 document_id 一起唯一定位一条分片 */
    ordinal: number
    /** 所在的标题路径，如「Token > Methods」；非结构化来源为 null */
    heading: string | null
    content: string
    /** PDF 的起止页；非 PDF 为 null */
    page_start: number | null
    page_end: number | null
    /** 所属文档名与原始文件名 */
    title: string
    filename: string
    /** 余弦相似度，越大越接近；只用于展示与取舍 */
    score: number
}

/** 知识库检索类 body。 */
export interface KnowledgeBody {
    /** 实际用于检索的句子；多跳追问时它可能与用户原话不同 */
    query: string
    /** 按相似度从高到低；可能为空，空不代表出错 */
    hits: KnowledgeHit[]
}

export interface KnowledgeResult extends ResultEnvelope {
    kind: 'knowledge'
    body: KnowledgeBody
}

/** 合并结果：后端把多份余额结果合成一条，body 与余额完全同构，前端复用同一套渲染。 */
export interface AggregateResult extends ResultEnvelope {
    kind: 'aggregate'
    body: BalanceBody
}

/** 走「链级明细」渲染的结果种类；新增同构 kind 要同时扩这里和 isBalanceLike 守卫，渲染组件不动。 */
export type BalanceLikeResult = BalanceResult | AggregateResult

/**
 * 消息里下发的结果。刻意不用判别联合：未知 kind 也要能落到同一个类型上，
 * 前端按 kind 走守卫（isBalanceLike / isPrice），不认识的一律兜底渲染。
 */
export type CopilotResult = ResultEnvelope & { kind: string; body: unknown }

/** 一次工具调用的元信息：工具名、参数、耗时、状态，以及它产出的数据。 */
export interface ToolCallMeta {
    tool: string
    args?: Record<string, unknown>
    duration_ms?: number
    status?: string
    error_code?: string
    /** 这次调用新产出的结果 id；合并类工具会把被并入的结果删掉，那些 id 不在其中。 */
    result_ids?: string[]
}

/** 整轮的工具调用元信息；与单条结果是一对多，所以不塞进结果里。 */
export interface ToolMeta {
    calls: ToolCallMeta[]
    total_duration_ms: number
    trace_id?: string | null
}

/** 数据库消息的稳定 ID 和会话内顺序；流中更新覆盖同一条 assistant 消息。 */
export interface ChatMessage {
    id: string
    turn_id: string
    seq: number
    role: 'user' | 'assistant'
    content: string
    status: string
    /** 提问时刻；历史与快照共用同一个值，格式为 ISO 8601。 */
    created_at?: string
    /** 本轮的业务结果，随消息下发，前端不再单独请求。 */
    results?: CopilotResult[]
    /** 本轮工具调用元信息（工具名 / 耗时 / trace），整轮一份。 */
    tool_meta?: ToolMeta
}

/** 一页历史加恢复信息；active_turn_id 不为空时继续订阅后台生成。 */
export interface MessagePage {
    items: ChatMessage[]
    next_cursor: number | null
    active_turn_id: string | null
}

/** 完整正文快照；version 属于轮次，不能与消息 seq 混用。 */
export interface TurnAccepted {
    turn_id: string
    created: boolean
}

export interface TurnSnapshot {
    turn_id: string
    session_id: string
    version: number
    status: string
    message: ChatMessage
    error: string | null
    context_info?: { execution_phase?: string }
    /** 终态尚未落库；此时仍订阅补写结果，不允许发送新一轮。 */
    persistence_pending?: boolean
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
    message: string
    /** 0x + 130 位十六进制 */
    signature: string
}

export interface TokenResponse {
    session_expires_at: string
    refresh_token: string
    user_id: string
    token: string
    token_type: string
    address: string
    expires_at: string
}

export interface MeResponse {
    address: string
    user_id: string
    /** 是否具备知识库管理权限；白名单未配置时所有人都为 false。 */
    is_admin: boolean
}

/* ── knowledge ─────────────────────────────────────────── */

/** 上传后由后台依次经过解析、切分、向量化；failed 时 error 有值。 */
export type DocumentStatus = 'pending' | 'processing' | 'ready' | 'failed'

/**
 * 知识库文档元数据。
 *
 * 上传返回时状态必然是 `pending` —— 正文还没解析，所以它不能当作最终结果用，
 * 页面要靠轮询这个接口跟上进度。分片内容与向量不从这里返回。
 */
export interface KnowledgeDocument {
    embedding_version?: string | null
    index_compatible?: boolean
    id: string
    /** 文件名去掉扩展名，展示用 */
    title: string
    filename: string
    mime: string
    /** 字节数 */
    size_bytes: number
    status: DocumentStatus
    /** 已入库的分片数；处理完成前为 0 */
    chunk_count: number
    /** 处理失败的原因；成功或尚未处理为 null */
    error: string | null
    /** 失败停在离线链路的哪一步（load / clean / split / embed）；未失败为 null */
    stage: string | null
    /** 失败码，形如 load.parse_failed，供界面按类别给文案；未失败为 null */
    error_code: string | null
    /** ISO 8601 */
    created_at: string
    /** ISO 8601 */
    updated_at: string
}
