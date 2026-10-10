import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Card, Input, Select, Typography } from 'antd'
import { useRef, useState } from 'react'
import { useAuthStore } from '../stores/auth'
import { shorten } from '../utils/format'
import { PageHeading, Note } from '../components/PageParts'
import { getChains, queryAssets } from '../api/resources'
import BalanceResult from '../components/BalanceResult'
export default function Assets() {
  const address = useAuthStore(s => s.address)
  const { data: chains } = useQuery({ queryKey: ['chains'], queryFn: getChains })
  const [chain, setChain] = useState<number | undefined>()
  const [symbol, setSymbol] = useState('')
  const [currency, setCurrency] = useState('USD')
  const [result, setResult] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const lock = useRef(false)
  async function query(retryChain?: number) {
    if (lock.current) return
    if (retryChain !== undefined) setChain(retryChain)
    lock.current = true; setBusy(true); setError(null); setResult(null)
    try { setResult((await queryAssets(retryChain ?? chain, symbol.trim(), currency)).result_id) }
    catch (e) { setError(e instanceof Error ? e.message : '查询失败') }
    finally { lock.current = false; setBusy(false) }
  }
  return <><PageHeading title="代币余额" description="基础服务直接提供余额与估值，不经过 Agent" extra={<span className="chip readonly">查询地址（只读）· {address && shorten(address)}</span>} />
    <div className="scope-bar"><b>查询范围：{chain ? chains?.find(c => c.chain_id === chain)?.symbols.length ?? '—' : chains?.reduce((n, c) => n + c.symbols.length, 0) ?? '—'} 个已登记代币条目</b>，不代表钱包全部代币。<br />清单外代币可指定网络与合约地址查询；同名代币按链与合约区分。</div>
    <Card><div className="filters"><Select aria-label="资产网络" style={{ width: 180 }} placeholder="全部已接入网络" allowClear value={chain} onChange={setChain}
      options={chains?.map(c => ({ value: c.chain_id, label: c.name }))} disabled={busy} />
      <Input aria-label="币种筛选" placeholder="币种或代币合约地址" value={symbol} onChange={e => setSymbol(e.target.value)} style={{ width: 240 }} disabled={busy} />
      <Select aria-label="估值币种" value={currency} onChange={setCurrency} options={[{ value: 'USD', label: 'USD 美元' }, { value: 'CNY', label: 'CNY 人民币' }]} disabled={busy} />
      <Button type="primary" loading={busy} onClick={() => void query()}>查询最新余额</Button></div>
      <Typography.Paragraph type="secondary">不填币种时仅查询服务器登记的代币，不代表钱包全部资产。指定币种时严格按网络和币种筛选；BERA 与 WBERA 分开查询。</Typography.Paragraph>
      {error && <Alert type="error" title={error} showIcon />}
      {result ? <BalanceResult key={result} id={result} onRetry={cid => void query(cid)} /> : !busy && <Typography.Paragraph type="secondary">选择查询范围后点击查询。未知余额和无行情资产不会按零处理。</Typography.Paragraph>}
    </Card><div style={{ marginTop: 14 }}><Note title="边界">查询地址只从登录凭证解析，前端不接收钱包地址入参。结果归属和权限由基础服务独立校验。</Note></div></>
}
