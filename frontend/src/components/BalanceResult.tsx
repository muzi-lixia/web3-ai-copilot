/** 数据展示通道：金额只从基础服务读取，不依赖模型正文。 */
import { Alert, Button, Empty, Spin, Table, Tooltip } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { useAuthStore } from '../stores/auth'
import { getChains } from '../api/resources'
import { http } from '../api/client'
import { formatUsd, formatValue } from '../utils/format'
import { Panel, Stat } from './PageParts'
import type { ApiResponse, WalletAssets, Asset } from '../types/api'
export interface AssetResult {
  chains: Omit<WalletAssets, 'address'>[]; total_value: string; currency: string; scope: string;
  isComplete: boolean; failed_chains: number[]; unpriced: { chain_id: number; symbol: string }[];
  queriedAt: string; exchange_rate_date?: string;
}
/** compact 用于对话内嵌卡片；完整模式展示原型中的统计、明细与异常区。 */
export default function BalanceResult({ id, compact = false, onRetry }: { id: string; compact?: boolean; onRetry?: (chain: number) => void }) {
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
  const columns = [
    { title: '代币', dataIndex: 'symbol', render: (v: string) => <div className="coin"><span className="cicon">{v.slice(0, 4)}</span>{v}</div> },
    { title: '链', dataIndex: 'chain_name' },
    { title: '余额', dataIndex: 'amount', align: 'right' as const, render: (v: string | null) => <span className="mono">{v ?? '未知'}</span> },
    ...(!compact ? [{ title: '价格 USD', dataIndex: 'price_usd', align: 'right' as const, render: (v: string | null) => <Tooltip title={v === null ? '暂无价格' : formatUsd(v)}><span className="mono">{formatUsd(v)}</span></Tooltip> }] : []),
    { title: '估值 USD', dataIndex: 'value_usd', align: 'right' as const, render: (v: string | null) => <Tooltip title={v === null ? '估值不可用' : formatUsd(v)}><span className="mono">{formatUsd(v)}</span></Tooltip> },
    ...(!compact ? [{ title: '链内占比', dataIndex: 'percentage', render: (v: number | null) => v === null ? '—' : <div><span className="mono">{v.toFixed(2)}%</span><div className="bar"><i style={{ width: `${Math.min(100, v)}%` }} /></div></div> }, { title: '状态', render: (_: unknown, a: Asset & { stale: boolean }) => <span className={`proto-tag ${a.amount === null || a.value_usd === null || a.stale ? 'soon' : 'live'}`}>{a.amount === null ? '余额未知' : a.value_usd === null ? '暂无价格' : a.stale ? '行情缓存' : '正常'}</span> }] : []),
  ]
  return <div className={compact ? 'balance-compact' : 'balance-full'}>
    {!compact && <div className="proto-grid four-cols" style={{ marginBottom: 14 }}><Stat value={knownTotal ? <Tooltip title={result.currency === 'USD' ? formatUsd(result.total_value) : formatValue(result.total_value, result.currency)}><span>{result.currency === 'USD' ? formatUsd(result.total_value) : formatValue(result.total_value, result.currency)}</span></Tooltip> : '估值不可用'} label="已知总估值（不含未计价）" /><Stat value={count} label="非零或未知余额代币数量" /><Stat value={`${result.chains.length} / ${result.chains.length + result.failed_chains.length}`} label="成功网络 / 查询网络" warning={!result.isComplete} /><Stat value={result.unpriced.length} label="未计价代币" warning={!!result.unpriced.length} /></div>}
    <Panel title={compact ? '余额概览' : '余额明细'} sub={compact ? <Link to="/portfolio">查看完整明细 →</Link> : scope}>
      <Table size="small" pagination={compact && rows.length > 6 ? { pageSize: 6 } : false} loading={isFetching} dataSource={rows}
        rowKey={a => `${a.chain_id}:${a.contract ?? 'native'}`} columns={columns} scroll={{ x: 'max-content' }} locale={{ emptyText: <Empty description="查询范围内暂无资产" /> }} />
      {!!result.unpriced.length && <div className="unpriced"><strong>未计价代币（{result.unpriced.length} 项）· 不计入总估值</strong><p>{result.unpriced.map(a => `${a.symbol} · ${result.chains.find(c => c.chain_id === a.chain_id)?.chain_name ?? a.chain_id}`).join('、')}</p><p>没有价格不等于价值为零，未知余额也不会按零计算。</p></div>}
      <div className="legend"><span>{scope} · {new Date(result.queriedAt).toLocaleString()}</span><span>已知估值：{knownTotal ? <Tooltip title={result.currency === 'USD' ? formatUsd(result.total_value) : formatValue(result.total_value, result.currency)}><span>{result.currency === 'USD' ? formatUsd(result.total_value) : formatValue(result.total_value, result.currency)}</span></Tooltip> : '不可用'}</span>{result.exchange_rate_date && <span>汇率日期：{result.exchange_rate_date}</span>}</div>
    </Panel>
    {!result.isComplete && <Panel title="部分查询结果" sub="isComplete=false" className="partial-result"><div className="panel-body"><Alert showIcon type="warning" title={result.failed_chains.length ? `以下网络查询失败：${result.failed_chains.map(cid => chainDirectory?.find(c => c.chain_id === cid)?.name ?? cid).join('、')}` : '存在未知余额、缺失行情或过期缓存'} description="合计只包含成功取得且可计价的部分，不代表全部资产。" />
      {!compact && result.failed_chains.map(cid => <Button key={cid} size="small" style={{ marginTop: 12, marginRight: 8 }} disabled={!onRetry} onClick={() => onRetry?.(cid)}>重试 {chainDirectory?.find(c => c.chain_id === cid)?.name ?? cid}</Button>)}</div></Panel>}
  </div>
}
