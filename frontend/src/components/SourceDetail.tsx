/** 一条结果的数据本体：来源行 + 明细（余额表 / 行情来源 / 知识片段）。
 *
 * 按 kind 分发，认不出的 kind 给原始数据兜底。挂在哪由调用方决定 ——
 * 现在它出现在「产出这条数据的工具」条目下，所以这里不带卡片外框。
 */
import { Fragment } from 'react'
import { useQuery } from '@tanstack/react-query'

import { getChains } from '../api/resources'
import { formatAmount, formatValue } from '../utils/format'
import Markdown from './Markdown'
import type {
    BalanceLikeResult, CopilotResult, KnowledgeHit, KnowledgeResult, PriceResult, WalletAssets,
} from '../types/api'

const isBalanceLike = (result: CopilotResult): result is BalanceLikeResult =>
    result.kind === 'balance' || result.kind === 'aggregate'

const isPrice = (result: CopilotResult): result is PriceResult => result.kind === 'price'

const isKnowledge = (result: CopilotResult): result is KnowledgeResult => result.kind === 'knowledge'

/** 结果的规模，给折叠条当标题：一眼看出这条数据有多少东西。 */
function resultSummary(result: CopilotResult): string {
    if (isBalanceLike(result)) return `${result.body.chains.length} 条网络`
    if (isKnowledge(result)) return `${result.body.hits.length} 个片段`
    if (isPrice(result)) return '公开行情'
    return result.kind
}

/** 来源行是后端拼好的一整串，其中 balanceOf 是标识符 —— 用等宽字体与正文区分。 */
function SourceLine({ desc }: { desc: string }) {
    const parts = desc.split('balanceOf')
    return (
        <>
            {parts.map((part, i) => (
                <Fragment key={i}>
                    {i > 0 && <span className="mono">balanceOf</span>}
                    {part}
                </Fragment>
            ))}
        </>
    )
}

/** 出处一行：编号 + 文档名 · 标题路径 · 页码 · 相似度；缺的段直接不出现，不留空占位。
 *
 * 编号摆在最前，是为了跟正文里引用的 [1][2] 对上；编号由后端分配，前端不重排。
 */
function hitLabel(hit: KnowledgeHit): string {
    const pages = hit.page_start === null || hit.page_end === null
        ? ''
        : hit.page_start === hit.page_end
            ? ` · 第 ${hit.page_start} 页`
            : ` · 第 ${hit.page_start}-${hit.page_end} 页`
    return `[${hit.index}] ▤ ${hit.title}${hit.heading ? ` · ${hit.heading}` : ''}${pages} · 相似度 ${hit.score.toFixed(3)}`
}

/** 余额明细：链级分组 + 该链小计，查询失败的链单列一行。 */
function BalanceDetail({ result }: { result: BalanceLikeResult }) {
    const { data: chainDirectory } = useQuery({ queryKey: ['chains'], queryFn: getChains })
    const body = result.body
    const currency = result.currency ?? 'USD'
    const failedChains = (result.failed ?? []).filter(f => f.type === 'chain').map(f => Number(f.ref))

    /** 金额一律「到分」，与完整页一致；链级小计后端只按 USD 算，非 USD 时宁可不显示也不混币种。 */
    const subtotal = (chain: Omit<WalletAssets, 'address'>) => {
        if (!chain.has_valuation) return '估值不可用'
        return currency === 'USD' ? `小计 ${formatValue(chain.total_value_usd, 'USD')}` : ''
    }

    return (
        <>
            <div className="src">
                <SourceLine desc={result.source_desc ?? `${body.chains.length} 条网络 balanceOf`} />
            </div>
            {/* 明细可达数十项，整块铺开会把对话区顶长，故固定高度、内部滚动。 */}
            <div className="blk-scroll">
                <div className="mini">
                    <table>
                        <thead>
                            <tr><th>资产</th><th className="num">余额</th><th className="num">估值</th></tr>
                        </thead>
                        <tbody>
                            {body.chains.map(chain => (
                                <Fragment key={chain.chain_id}>
                                    {/* 分组行留一个空列，小计才对到「估值」列下面，而不是压在余额列。 */}
                                    <tr className="grp">
                                        <td><span className="cdot" />{chain.chain_name}</td>
                                        <td />
                                        <td className="s">{subtotal(chain)}</td>
                                    </tr>
                                    {chain.assets.map(asset => (
                                        <tr className="tk" key={`${chain.chain_id}:${asset.contract ?? 'native'}`}>
                                            <td>{asset.symbol}</td>
                                            <td className="num">{formatAmount(asset.amount)}</td>
                                            <td className="num">
                                                {asset.value_usd === null
                                                    ? <span className="nop">无价格</span>
                                                    : formatValue(asset.value_usd, currency)}
                                            </td>
                                        </tr>
                                    ))}
                                </Fragment>
                            ))}
                            {failedChains.map(cid => (
                                <tr className="grp" key={`failed:${cid}`}>
                                    <td>
                                        <span className="cdot" />
                                        {chainDirectory?.find(c => c.chain_id === cid)?.name ?? `链 ${cid}`}{' '}
                                        <span className="nop">查询失败</span>
                                    </td>
                                    <td />
                                    <td className="s">未计入</td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                </div>
            </div>
        </>
    )
}

/** 行情只有来源行：价格本身由模型在正文里陈述，这里不重复。 */
function PriceDetail({ result }: { result: PriceResult }) {
    const token = result.body.token
    return (
        <div className="src">
            {result.source_desc ?? '公开行情'}{token.change_7d ? ` · 七日 ${token.change_7d}%` : ''}
        </div>
    )
}

/** 命中片段全部展示、不做截断；编号由后端一次分配，与正文引用的 [1][2] 是同一套。 */
function KnowledgeDetail({ result }: { result: KnowledgeResult }) {
    const { query, hits } = result.body
    return (
        <>
            <div className="src">{result.source_desc ?? '知识库检索'} · 检索词「{query}」</div>
            {hits.length === 0 ? (
                <div className="blk-b"><div className="tot-lim">知识库里没有命中任何片段。</div></div>
            ) : (
                <div className="cite-list">
                    {hits.map(hit => (
                        <div className="cite" key={`${hit.document_id}:${hit.ordinal}`}>
                            <strong>{hitLabel(hit)}</strong>
                            <Markdown text={hit.content} />
                        </div>
                    ))}
                </div>
            )}
        </>
    )
}

/** 未知 kind：给出可诊断的原始数据，后端先上线、前端后跟也不会白屏。 */
function UnknownDetail({ result }: { result: CopilotResult }) {
    const raw = { marginTop: 10, padding: 10, background: 'var(--panel)', borderRadius: 8, overflow: 'auto' }
    return (
        <div className="blk-b">
            <div className="tot-top"><span className="tot-lb">前端暂不支持展示的结果 · {result.kind}</span></div>
            <div className="tot-lim">这类结果还没有渲染分支，先按原始数据显示。</div>
            <pre className="tc-p" style={raw}>{JSON.stringify(result.body, null, 2)}</pre>
        </div>
    )
}

export default function SourceDetail({ result }: { result: CopilotResult }) {
    if (isBalanceLike(result)) return <BalanceDetail result={result} />
    if (isPrice(result)) return <PriceDetail result={result} />
    if (isKnowledge(result)) return <KnowledgeDetail result={result} />
    return <UnknownDetail result={result} />
}

/** 一条数据的折叠条：标题写明它有多少东西，展开才是数据本体，默认收起。 */
export function SourceFold({ result }: { result: CopilotResult }) {
    return (
        <details className="tcf">
            <summary>
                数据引用（{resultSummary(result)}）<span className="arw" />
            </summary>
            <SourceDetail result={result} />
        </details>
    )
}
