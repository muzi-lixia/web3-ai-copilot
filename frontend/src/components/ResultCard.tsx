/** 结果卡分发：按 kind 选渲染器；未知 kind 兜底显示原始数据，不白屏。
 *
 * streaming 为真表示本轮还在流式输出：此时只给结果本体，数据来源与工具调用
 * 留到输出结束再出现 —— 免得用户在看正文时下面已经在跳工具调用。
 */
import type { ReactNode } from 'react'

import BalanceResult from './BalanceResult'
import Markdown from './Markdown'
import ToolCalls from './ToolCalls'
import type { BalanceLikeResult, CopilotResult, KnowledgeHit, KnowledgeResult, PriceResult, ToolMeta } from '../types/api'

/** 余额与合并共用同一套渲染：后端两条 kind 的 body 结构相同。 */
const isBalanceLike = (result: CopilotResult): result is BalanceLikeResult =>
  result.kind === 'balance' || result.kind === 'aggregate'
const isPrice = (result: CopilotResult): result is PriceResult => result.kind === 'price'
const isKnowledge = (result: CopilotResult): result is KnowledgeResult => result.kind === 'knowledge'

/** 行情只出正文与数据来源，价格本身由模型在正文里陈述，不再重复一张报价卡。 */
function PriceCard({ result, say, toolMeta, streaming }: {
  result: PriceResult; say?: ReactNode; toolMeta?: ToolMeta; streaming?: boolean
}) {
  const token = result.body.token
  return <div className="blk">
    <div className="blk-b">{say}</div>
    {/* 与余额卡同构：折叠区挂在同一张卡下沿，不另起第二张卡。流式期间整体不出现。 */}
    {!streaming && <details className="fold">
      <summary>数据来源与工具调用
        <span className="rt">{toolMeta ? `${toolMeta.calls.length} 次调用 · ${toolMeta.total_duration_ms}ms` : ''}</span>
        <span className="arw" />
      </summary>
      <div className="src">{result.source_desc ?? '公开行情'}{token.change_7d ? ` · 七日 ${token.change_7d}%` : ''}</div>
      {!!toolMeta?.calls.length && <div className="blk-b" style={{ borderTop: '1px solid var(--line)', padding: '2px 13px 11px' }}><ToolCalls meta={toolMeta} /></div>}
    </details>}
  </div>
}

/** 出处一行：编号 + 文档名 · 标题路径 · 页码 · 相似度；缺的段直接不出现，不留空占位。
 *
 * 编号摆在最前，是为了跟正文里引用的 [1][2] 对上；编号由后端分配，前端不重排。
 */
function hitLabel(hit: KnowledgeHit): string {
  const pages = hit.page_start === null || hit.page_end === null
    ? '' : hit.page_start === hit.page_end ? ` · 第 ${hit.page_start} 页` : ` · 第 ${hit.page_start}-${hit.page_end} 页`
  return `[${hit.index}] ▤ ${hit.title}${hit.heading ? ` · ${hit.heading}` : ''}${pages} · 相似度 ${hit.score.toFixed(3)}`
}

/** 命中片段归在「数据来源与工具调用」折叠区里：正文只留模型的话，核对原文时展开看。
 *
 * 片段全部展示、不做截断；编号一次由后端分配，与正文引用的 [1][2] 是同一套，前端不重排。
 */
function KnowledgeCard({ result, say, toolMeta, streaming }: {
  result: KnowledgeResult; say?: ReactNode; toolMeta?: ToolMeta; streaming?: boolean
}) {
  const { query, hits } = result.body
  return <div className="blk">
    <div className="blk-b">{say}</div>
    {!streaming && <details className="fold">
      <summary>数据来源与工具调用
        <span className="rt">{toolMeta ? `${toolMeta.calls.length} 次调用 · ${toolMeta.total_duration_ms}ms` : `${hits.length} 个片段`}</span>
        <span className="arw" />
      </summary>
      <div className="src">{result.source_desc ?? '知识库检索'} · 检索词「{query}」</div>
      {hits.length === 0
        ? <div className="blk-b"><div className="tot-lim">知识库里没有命中任何片段。</div></div>
        : <div className="cite-list">{hits.map(hit => <div className="cite" key={`${hit.document_id}:${hit.ordinal}`}>
          <strong>{hitLabel(hit)}</strong>
          <Markdown text={hit.content} />
        </div>)}</div>}
      {!!toolMeta?.calls.length && <div className="blk-b" style={{ borderTop: '1px solid var(--line)', padding: '2px 13px 11px' }}><ToolCalls meta={toolMeta} /></div>}
    </details>}
  </div>
}

/** 未知 kind：给出可诊断的原始数据，后端先上线、前端后跟也不会白屏。 */
function UnknownCard({ result }: { result: CopilotResult }) {
  return <div className="blk"><div className="blk-b">
    <div className="tot-top"><span className="tot-lb">前端暂不支持展示的结果 · {result.kind}</span></div>
    <div className="tot-lim">这类结果还没有渲染分支，先按原始数据显示。</div>
    <pre className="tc-p" style={{ marginTop: 10, padding: 10, background: 'var(--panel)', borderRadius: 8, overflow: 'auto' }}>{JSON.stringify(result.body, null, 2)}</pre>
  </div></div>
}

export default function ResultCard({ result, say, toolMeta, streaming }: {
  result: CopilotResult; say?: ReactNode; toolMeta?: ToolMeta; streaming?: boolean
}) {
  if (isBalanceLike(result)) return <BalanceResult result={result} compact say={say} toolMeta={toolMeta} streaming={streaming} />
  if (isPrice(result)) return <PriceCard result={result} say={say} toolMeta={toolMeta} streaming={streaming} />
  if (isKnowledge(result)) return <KnowledgeCard result={result} say={say} toolMeta={toolMeta} streaming={streaming} />
  return <UnknownCard result={result} />
}
