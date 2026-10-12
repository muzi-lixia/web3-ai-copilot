/** 结果卡分发：按 kind 选渲染器；未知 kind 兜底显示原始数据，不白屏。
 *
 * streaming 为真表示本轮还在流式输出：此时只给结果本体，数据来源与工具调用
 * 留到输出结束再出现 —— 免得用户在看正文时下面已经在跳工具调用。
 */
import type { ReactNode } from 'react'

import BalanceResult from './BalanceResult'
import ToolCalls from './ToolCalls'
import type { BalanceLikeResult, CopilotResult, PriceResult, ToolMeta } from '../types/api'

/** 余额与合并共用同一套渲染：后端两条 kind 的 body 结构相同。 */
const isBalanceLike = (result: CopilotResult): result is BalanceLikeResult =>
  result.kind === 'balance' || result.kind === 'aggregate'
const isPrice = (result: CopilotResult): result is PriceResult => result.kind === 'price'

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
  return <UnknownCard result={result} />
}
