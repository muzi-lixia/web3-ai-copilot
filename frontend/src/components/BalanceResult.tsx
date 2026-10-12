/** 余额结果：对话内嵌（compact，数据随消息下发）与完整页（按 id 读取）共用同一个信封 + body。 */
import { Fragment, type ReactNode } from 'react'
import { Alert, Button, Empty, Spin, Table, Tooltip } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { useAuthStore } from '../stores/auth'
import { getChains } from '../api/resources'
import { http } from '../api/client'
import { formatAmount, formatUsd, formatValue } from '../utils/format'
import { Panel, Stat } from './PageParts'
import ToolCalls from './ToolCalls'
import type { ApiResponse, BalanceLikeResult, BalanceResult as BalanceResultData, WalletAssets, Asset, ToolMeta } from '../types/api'
/** 来源行是后端拼好的一整串，其中 balanceOf 是标识符 —— 按原型用等宽字体与正文区分。 */
function SourceLine({ desc }: { desc: string }) {
  const parts = desc.split('balanceOf')
  return <>{parts.map((part, i) => <Fragment key={i}>{i > 0 && <span className="mono">balanceOf</span>}{part}</Fragment>)}</>
}

/** 对话内嵌卡片按原型只给「结论 → 限定 → 明细」；统计与异常区留给完整页。 */
export default function BalanceResult({ id, result: provided, compact = false, onRetry, say, toolMeta, streaming = false }: {
  id?: string; result?: BalanceLikeResult; compact?: boolean; onRetry?: (chain: number) => void;
  say?: ReactNode; toolMeta?: ToolMeta; streaming?: boolean
}) {
  const address = useAuthStore(s => s.address)
  const { data: chainDirectory } = useQuery({ queryKey: ['chains'], queryFn: getChains })
  // 对话内的卡片数据随消息下发，只有完整页才需要按 id 单独读取。
  const { data: fetched, error, isFetching, refetch } = useQuery({ queryKey: ['balance-result', address, id],
    enabled: !!address && !provided && !!id,
    queryFn: async ({ signal }) => (await http.get<ApiResponse<BalanceResultData>>(`/me/balance-results/${id}`, { signal })).data.data })
  const result = provided ?? fetched
  if (error) return <Alert type="warning" title={error instanceof Error ? error.message : '结果读取失败'} action={<Button size="small" onClick={() => void refetch()}>重新读取</Button>} />
  if (!result) return <Spin size="small" />
  const body = result.body
  const currency = result.currency ?? 'USD'
  const complete = result.status === 'complete'
  const failedChains = (result.failed ?? []).filter(f => f.type === 'chain').map(f => Number(f.ref))
  const rows = body.chains.flatMap(c => c.assets.map(a => ({ ...a, chain_id: c.chain_id, chain_name: c.chain_name, stale: c.stale })))
  const knownTotal = rows.every(a => a.amount === '0') || rows.some(a => a.value_usd !== null && a.amount !== null && Number(a.amount) !== 0)
  const scope = body.scope === 'specified_token' ? '指定代币范围' : '已登记代币范围'
  const count = rows.filter(a => a.amount !== '0').length
  /** 卡片金额一律「到分」，与完整页的「已知估值」以及原型一致；
   *  6 位小数的 formatUsd 只用在完整表格的单币种价格列。
   *  链级小计后端只按 USD 计算，非 USD 计价时宁可不显示也不混币种。 */
  const subtotal = (chain: Omit<WalletAssets, 'address'>) =>
    !chain.has_valuation ? '估值不可用' : currency === 'USD' ? `小计 ${formatValue(chain.total_value_usd, 'USD')}` : ''
  if (compact) {
    // 回答区两段：结果常显，数据来源与明细折叠。明细不做截断 —— 数据本就在 body 里，
    // 掐掉前 N 条只会造成「有结果却看不到」，卡片长就长，过长时由折叠区内部滚动。
    const visible = body.chains.map(chain => ({ chain, assets: chain.assets }))
    return <div className="blk">
      <div className="blk-b">{say}</div>
      {/* 「数据来源与工具调用」是同一张卡下沿的折叠区，不是第二张卡；默认收起。
          流式输出期间整块不出现，等模型说完再显示。 */}
      {!streaming && <details className="fold">
        <summary>数据来源与工具调用
          <span className="rt">{toolMeta ? `${toolMeta.calls.length} 次调用 · ${toolMeta.total_duration_ms}ms` : `${body.chains.length} 条网络`}</span>
          <span className="arw" />
        </summary>
        <div className="src"><SourceLine desc={result.source_desc ?? `${body.chains.length} 条网络 balanceOf`} /></div>
        {/* 只有明细表滚动：来源行与工具调用固定在折叠区首尾，不跟着一起滚。 */}
        <div className="blk-scroll">
          <div className="mini">
            <table>
              <thead><tr><th>资产</th><th className="num">余额</th><th className="num">估值</th></tr></thead>
              <tbody>
                {visible.map(({ chain, assets }) => <Fragment key={chain.chain_id}>
                  {/* 分组行留一个空列，小计才对到「估值」列下面，而不是压在余额列。 */}
                  <tr className="grp"><td><span className="cdot" />{chain.chain_name}</td><td /><td className="s">{subtotal(chain)}</td></tr>
                  {assets.map(asset => <tr className="tk" key={`${chain.chain_id}:${asset.contract ?? 'native'}`}>
                    <td>{asset.symbol}</td>
                    <td className="num">{formatAmount(asset.amount)}</td>
                    <td className="num">{asset.value_usd === null ? <span className="nop">无价格</span> : formatValue(asset.value_usd, currency)}</td>
                  </tr>)}
                </Fragment>)}
                {failedChains.map(cid => <tr className="grp" key={`failed:${cid}`}>
                  <td><span className="cdot" />{chainDirectory?.find(c => c.chain_id === cid)?.name ?? `链 ${cid}`} <span className="nop">查询失败</span></td>
                  <td /><td className="s">未计入</td>
                </tr>)}
              </tbody>
            </table>
          </div>
        </div>
        {!!toolMeta?.calls.length && <div className="blk-b" style={{ borderTop: '1px solid var(--line)', padding: '2px 13px 11px' }}>
          <ToolCalls meta={toolMeta} />
        </div>}
      </details>}
    </div>
  }
  const columns = [
    { title: '代币', dataIndex: 'symbol', render: (v: string) => <div className="coin"><span className="cicon">{v.slice(0, 4)}</span>{v}</div> },
    { title: '链', dataIndex: 'chain_name' },
    { title: '余额', dataIndex: 'amount', align: 'right' as const, render: (v: string | null) => <span className="mono">{v ?? '未知'}</span> },
    { title: '价格 USD', dataIndex: 'price_usd', align: 'right' as const, render: (v: string | null) => <Tooltip title={v === null ? '暂无价格' : formatUsd(v)}><span className="mono">{formatUsd(v)}</span></Tooltip> },
    { title: '估值 USD', dataIndex: 'value_usd', align: 'right' as const, render: (v: string | null) => <Tooltip title={v === null ? '估值不可用' : formatUsd(v)}><span className="mono">{formatUsd(v)}</span></Tooltip> },
    { title: '链内占比', dataIndex: 'percentage', render: (v: number | null) => v === null ? '—' : <div><span className="mono">{v.toFixed(2)}%</span><div className="bar"><i style={{ width: `${Math.min(100, v)}%` }} /></div></div> },
    { title: '状态', render: (_: unknown, a: Asset & { stale: boolean }) => <span className={`proto-tag ${a.amount === null || a.value_usd === null || a.stale ? 'soon' : 'live'}`}>{a.amount === null ? '余额未知' : a.value_usd === null ? '暂无价格' : a.stale ? '行情缓存' : '正常'}</span> },
  ]
  return <div className="balance-full">
    <div className="proto-grid four-cols" style={{ marginBottom: 14 }}><Stat value={knownTotal ? <Tooltip title={currency === 'USD' ? formatUsd(body.total_value) : formatValue(body.total_value, currency)}><span>{currency === 'USD' ? formatUsd(body.total_value) : formatValue(body.total_value, currency)}</span></Tooltip> : '估值不可用'} label="已知总估值（不含未计价）" /><Stat value={count} label="非零或未知余额代币数量" /><Stat value={`${body.chains.length} / ${body.chains.length + failedChains.length}`} label="成功网络 / 查询网络" warning={!complete} /><Stat value={body.unpriced.length} label="未计价代币" warning={!!body.unpriced.length} /></div>
    <Panel title="余额明细" sub={scope}>
      <Table size="small" pagination={false} loading={isFetching} dataSource={rows}
        rowKey={a => `${a.chain_id}:${a.contract ?? 'native'}`} columns={columns} scroll={{ x: 'max-content' }} locale={{ emptyText: <Empty description="查询范围内暂无资产" /> }} />
      {!!body.unpriced.length && <div className="unpriced"><strong>未计价代币（{body.unpriced.length} 项）· 不计入总估值</strong><p>{body.unpriced.map(a => `${a.symbol} · ${body.chains.find(c => c.chain_id === a.chain_id)?.chain_name ?? a.chain_id}`).join('、')}</p><p>没有价格不等于价值为零，未知余额也不会按零计算。</p></div>}
      <div className="legend"><span>{scope} · {new Date(result.queriedAt).toLocaleString()}</span><span>已知估值：{knownTotal ? <Tooltip title={currency === 'USD' ? formatUsd(body.total_value) : formatValue(body.total_value, currency)}><span>{currency === 'USD' ? formatUsd(body.total_value) : formatValue(body.total_value, currency)}</span></Tooltip> : '不可用'}</span>{body.exchange_rate_date && <span>汇率日期：{body.exchange_rate_date}</span>}</div>
    </Panel>
    {!complete && <Panel title="部分查询结果" sub="status=partial" className="partial-result"><div className="panel-body"><Alert showIcon type="warning" title={failedChains.length ? `以下网络查询失败：${failedChains.map(cid => chainDirectory?.find(c => c.chain_id === cid)?.name ?? cid).join('、')}` : '存在未知余额、缺失行情或过期缓存'} description="合计只包含成功取得且可计价的部分，不代表全部资产。" />
      {failedChains.map(cid => <Button key={cid} size="small" style={{ marginTop: 12, marginRight: 8 }} disabled={!onRetry} onClick={() => onRetry?.(cid)}>重试 {chainDirectory?.find(c => c.chain_id === cid)?.name ?? cid}</Button>)}</div></Panel>}
  </div>
}
