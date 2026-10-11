/** 数据展示通道：金额只从基础服务读取，不依赖模型正文。 */
import { Fragment, type ReactNode } from 'react'
import { Alert, Button, Empty, Spin, Table, Tooltip } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useAuthStore } from '../stores/auth'
import { getChains } from '../api/resources'
import { http } from '../api/client'
import { formatAmount, formatUsd, formatValue } from '../utils/format'
import { Panel, Stat } from './PageParts'
import type { ApiResponse, WalletAssets, Asset } from '../types/api'
export interface AssetResult {
  chains: Omit<WalletAssets, 'address'>[]; total_value: string; currency: string; scope: string;
  isComplete: boolean; failed_chains: number[]; unpriced: { chain_id: number; symbol: string }[];
  queriedAt: string; exchange_rate_date?: string;
}
/** 对话内嵌卡片按原型只给「结论 → 限定 → 明细」；统计与异常区留给完整页。 */
const COMPACT_ROWS = 6
export default function BalanceResult({ id, compact = false, onRetry, say }: { id: string; compact?: boolean; onRetry?: (chain: number) => void; say?: ReactNode }) {
  const address = useAuthStore(s => s.address)
  const { data: chainDirectory } = useQuery({ queryKey: ['chains'], queryFn: getChains })
  const { data: result, error, isFetching, refetch } = useQuery({ queryKey: ['balance-result', address, id], enabled: !!address,
    queryFn: async ({ signal }) => (await http.get<ApiResponse<AssetResult>>(`/me/balance-results/${id}`, { signal })).data.data })
  if (error) return <Alert type="warning" title={error instanceof Error ? error.message : '结果读取失败'} action={<Button size="small" onClick={() => void refetch()}>重新读取</Button>} />
  if (!result) return <Spin size="small" />
  const rows = result.chains.flatMap(c => c.assets.map(a => ({ ...a, chain_id: c.chain_id, chain_name: c.chain_name, stale: c.stale })))
  const knownTotal = rows.every(a => a.amount === '0') || rows.some(a => a.value_usd !== null && a.amount !== null && Number(a.amount) !== 0)
  const scope = result.scope === 'specified_token' ? '指定代币范围' : '已登记代币范围'
  const count = rows.filter(a => a.amount !== '0').length
  /** 卡片里的总额/小计一律「到分」，与完整页的「已知估值」以及原型一致；
   *  6 位小数的 formatUsd 只用在完整表格的单币种价格列。 */
  const money = (value: string | null) => formatValue(value, result.currency)
  const total = money(result.total_value)
  /** 链级小计后端只按 USD 计算，非 USD 计价时宁可不显示也不混币种。 */
  const subtotal = (chain: Omit<WalletAssets, 'address'>) =>
    !chain.has_valuation ? '估值不可用' : result.currency === 'USD' ? `小计 ${formatValue(chain.total_value_usd, 'USD')}` : ''
  if (compact) {
    // 回答区两段：结果常显，数据来源与明细折叠；超出部分引导到完整页面，卡片不承担全量展示。
    const visible: { chain: Omit<WalletAssets, 'address'>; assets: Asset[] }[] = []
    let budget = COMPACT_ROWS
    for (const chain of result.chains) {
      const assets = chain.assets.slice(0, Math.max(0, budget))
      if (assets.length) visible.push({ chain, assets })
      budget -= assets.length
    }
    const hidden = rows.length - visible.reduce((n, item) => n + item.assets.length, 0)
    return <>
      <div className="blk">
        <div className="blk-b">
          {say}
          <div className="tot-top"><span className="tot-lb">{scope} · 可计价部分</span>{!result.isComplete && <span className="badge-part">部分结果</span>}</div>
          <div className="tot-num">{knownTotal ? total : '估值不可用'}</div>
          <div className="tot-lim">{count} 项可计价{!!result.unpriced.length && <> · <b>{result.unpriced.length} 项无价格，未计入</b></>}{result.failed_chains.length ? <> · <b>{result.failed_chains.length} 条网络失败，未计入</b></> : ' · 全部网络已返回'}</div>
          <div className="tot-time">数据截至 {new Date(result.queriedAt).toLocaleTimeString()} · 估值 = 余额 × 同一时刻价格 · 缓存过期时标注来源时间</div>
        </div>
      </div>
      <details className="blk">
        <summary className="blk-h">数据来源与工具调用<span className="rt">{result.chains.length} 条网络 · 不经模型</span><span className="arw">▸</span></summary>
        <div className="src">{result.chains.length} 条网络 <span className="mono">balanceOf</span> · 快照 {new Date(result.queriedAt).toLocaleTimeString()}</div>
        <div className="mini">
          <table>
            <thead><tr><th>代币</th><th className="num">余额</th><th className="num">估值</th></tr></thead>
            <tbody>
              {visible.map(({ chain, assets }) => <Fragment key={chain.chain_id}>
                <tr className="grp"><td>{chain.chain_name}</td><td className="s">{subtotal(chain)}</td></tr>
                {assets.map(asset => <tr key={`${chain.chain_id}:${asset.contract ?? 'native'}`}>
                  <td><div className="coin"><span className="cicon">{asset.symbol.slice(0, 4)}</span>{asset.symbol}</div></td>
                  <td className="num">{formatAmount(asset.amount)}</td>
                  <td className="num">{asset.value_usd === null ? <span className="nop">无价格</span> : formatValue(asset.value_usd, result.currency)}</td>
                </tr>)}
              </Fragment>)}
              {result.failed_chains.map(cid => <tr className="grp" key={`failed:${cid}`}>
                <td>{chainDirectory?.find(c => c.chain_id === cid)?.name ?? `链 ${cid}`} <span className="nop">查询失败</span></td>
                <td className="s">未计入</td>
              </tr>)}
            </tbody>
          </table>
          {hidden > 0 && <div className="more-row">还有 {hidden} 项 · 滚动加载，不分页<Link className="go" to="/portfolio">查看全部 →</Link></div>}
        </div>
        <div className="blk-b" style={{ borderTop: '1px solid var(--line)', padding: '2px 13px 11px' }}>
          <div className="blk-f"><span className="muted">模型只收到元数据；余额与估值由展示通道读取，不进入模型与 trace。</span></div>
        </div>
      </details>
    </>
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
    <div className="proto-grid four-cols" style={{ marginBottom: 14 }}><Stat value={knownTotal ? <Tooltip title={result.currency === 'USD' ? formatUsd(result.total_value) : formatValue(result.total_value, result.currency)}><span>{result.currency === 'USD' ? formatUsd(result.total_value) : formatValue(result.total_value, result.currency)}</span></Tooltip> : '估值不可用'} label="已知总估值（不含未计价）" /><Stat value={count} label="非零或未知余额代币数量" /><Stat value={`${result.chains.length} / ${result.chains.length + result.failed_chains.length}`} label="成功网络 / 查询网络" warning={!result.isComplete} /><Stat value={result.unpriced.length} label="未计价代币" warning={!!result.unpriced.length} /></div>
    <Panel title="余额明细" sub={scope}>
      <Table size="small" pagination={false} loading={isFetching} dataSource={rows}
        rowKey={a => `${a.chain_id}:${a.contract ?? 'native'}`} columns={columns} scroll={{ x: 'max-content' }} locale={{ emptyText: <Empty description="查询范围内暂无资产" /> }} />
      {!!result.unpriced.length && <div className="unpriced"><strong>未计价代币（{result.unpriced.length} 项）· 不计入总估值</strong><p>{result.unpriced.map(a => `${a.symbol} · ${result.chains.find(c => c.chain_id === a.chain_id)?.chain_name ?? a.chain_id}`).join('、')}</p><p>没有价格不等于价值为零，未知余额也不会按零计算。</p></div>}
      <div className="legend"><span>{scope} · {new Date(result.queriedAt).toLocaleString()}</span><span>已知估值：{knownTotal ? <Tooltip title={result.currency === 'USD' ? formatUsd(result.total_value) : formatValue(result.total_value, result.currency)}><span>{result.currency === 'USD' ? formatUsd(result.total_value) : formatValue(result.total_value, result.currency)}</span></Tooltip> : '不可用'}</span>{result.exchange_rate_date && <span>汇率日期：{result.exchange_rate_date}</span>}</div>
    </Panel>
    {!result.isComplete && <Panel title="部分查询结果" sub="isComplete=false" className="partial-result"><div className="panel-body"><Alert showIcon type="warning" title={result.failed_chains.length ? `以下网络查询失败：${result.failed_chains.map(cid => chainDirectory?.find(c => c.chain_id === cid)?.name ?? cid).join('、')}` : '存在未知余额、缺失行情或过期缓存'} description="合计只包含成功取得且可计价的部分，不代表全部资产。" />
      {result.failed_chains.map(cid => <Button key={cid} size="small" style={{ marginTop: 12, marginRight: 8 }} disabled={!onRetry} onClick={() => onRetry?.(cid)}>重试 {chainDirectory?.find(c => c.chain_id === cid)?.name ?? cid}</Button>)}</div></Panel>}
  </div>
}
