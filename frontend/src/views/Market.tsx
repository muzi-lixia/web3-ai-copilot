import { useQueries, useQuery } from '@tanstack/react-query'
import { Alert, Button, Input, Select, Table } from 'antd'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { getChains, getPublicPrice } from '../api/resources'
import { fetchMarketQuotes } from '../api/market'
import { formatUsd } from '../utils/format'
import { useAuthStore } from '../stores/auth'
import { PageHeading, Panel, Stat } from '../components/PageParts'
/** 涨跌按原型使用涨红跌绿；金额只展示服务端字符串，不在前端记账或换汇。 */
function Change({ value }: { value: number | string | null | undefined }) {
  if (value === null || value === undefined) return <span className="muted">—</span>
  const number = Number(value)
  return <span className="mono" style={{ color: number > 0 ? '#F6465D' : number < 0 ? '#2EBD85' : '#98A7B8' }}>{number > 0 ? '+' : ''}{number.toFixed(2)}%</span>
}
export default function Market() {
  const token = useAuthStore(s => s.token)
  const [chainId, setChainId] = useState<number | undefined>()
  const [symbol, setSymbol] = useState('')
  const [currency, setCurrency] = useState('USD')
  const chains = useQuery({ queryKey: ['chains'], queryFn: getChains })
  const quotes = useQuery({ queryKey: ['market-quotes', chainId], queryFn: () => fetchMarketQuotes({ chainId }), refetchInterval: 60000 })
  const tokens = quotes.data?.tokens ?? []
  // 七日变化和 CNY 均由单币接口提供，缓存按网络、币种、币种单位分开。
  const details = useQueries({ queries: tokens.map(t => ({ queryKey: ['public-price', quotes.data!.chain_id, t.symbol, currency],
    queryFn: () => getPublicPrice(quotes.data!.chain_id, t.symbol, currency), staleTime: 60000, retry: false })) })
  const rows = tokens.map((t, i) => ({ ...t, detail: details[i]?.data, detailLoading: details[i]?.isFetching, detailError: details[i]?.error }))
    .filter(t => t.symbol.toLowerCase().includes(symbol.trim().toLowerCase()))
  const sources = [...new Set(tokens.map(t => t.source))].join(' · ')
  async function refresh() { await quotes.refetch(); await Promise.all(details.map(d => d.refetch())) }
  return <><PageHeading title="行情" description="基础服务公开数据 · 允许匿名访问，无需凭证" extra={<span className="chip">涨 <span style={{ color: '#F6465D' }}>红</span> / 跌 <span style={{ color: '#2EBD85' }}>绿</span></span>} />
    {!token && <div className="anon-bar"><span>当前为匿名浏览。行情是公开数据；本人余额和对话需要连接钱包。</span><Link to="/login"><Button size="small">连接钱包</Button></Link></div>}
    <div className="proto-grid three-cols" style={{ marginBottom: 14 }}><Stat value="—" label="全市场总市值（未接入）" /><Stat value="—" label="全市场 24h 交易量（未接入）" /><Stat value={<span style={{ fontSize: 13 }}>{sources || '等待报价'}</span>} label="当前列表实际数据来源" /></div>
    <div className="filters"><Select aria-label="行情网络" allowClear value={chainId} placeholder="默认网络" onChange={setChainId} style={{ width: 180 }} options={chains.data?.map(c => ({ value: c.chain_id, label: c.name }))} /><Input aria-label="行情币种筛选" placeholder="筛选币种" value={symbol} onChange={e => setSymbol(e.target.value)} style={{ width: 220 }} /><Select aria-label="行情计价单位" value={currency} onChange={setCurrency} options={[{ value: 'USD' }, { value: 'CNY' }]} /><Button loading={quotes.isFetching || details.some(d => d.isFetching)} onClick={() => void refresh()}>刷新</Button></div>
    {quotes.error && <Alert type="error" showIcon title={quotes.error instanceof Error ? quotes.error.message : '行情查询失败'} style={{ marginBottom: 14 }} />}
    <Panel title="行情列表" sub={quotes.data?.chain_name ?? '正在读取网络'}><Table size="small" pagination={false} dataSource={rows} loading={quotes.isFetching && !quotes.data} rowKey="symbol" scroll={{ x: 'max-content' }} columns={[
      { title: '代币', dataIndex: 'symbol', render: v => <div className="coin"><span className="cicon">{v.slice(0, 4)}</span>{v}</div> },
      { title: `价格 ${currency}`, align: 'right', render: (_, row) => <span className="mono">{currency === 'USD' ? formatUsd(row.price_usd) : row.detail?.price ?? '—'}</span> },
      { title: '24h', align: 'right', render: (_, row) => <Change value={row.change_24h} /> },
      { title: '7d', align: 'right', render: (_, row) => row.detailLoading ? <span className="muted">读取中</span> : <Change value={row.detail?.change_7d} /> },
      { title: `市值 ${currency}`, align: 'right', render: (_, row) => currency === 'USD' ? formatUsd(row.market_cap) : row.detail?.market_cap ?? '—' },
      { title: `24h 交易量 ${currency}`, align: 'right', render: (_, row) => currency === 'USD' ? formatUsd(row.volume_24h) : row.detail?.volume_24h ?? '—' },
      { title: '来源 / 状态', render: (_, row) => <div>{row.source}<div className="market-row-meta">{row.detailError ? '详细行情暂不可用' : row.stale ? '过期缓存' : new Date(row.updated_at).toLocaleTimeString()}{row.detail?.exchange_rate_date && ` · 汇率 ${row.detail.exchange_rate_date}`}</div></div> },
    ]} />{!!quotes.data?.missing.length && <div className="unpriced"><strong>暂无报价：{quotes.data.missing.join('、')}</strong><p>未取得报价不代表价格为零。</p></div>}<div className="legend"><span>缺失价格或统计字段显示为 —，不猜测数值</span><span>报价来源、更新时间和过期缓存状态按实际响应展示</span></div></Panel></>
}
