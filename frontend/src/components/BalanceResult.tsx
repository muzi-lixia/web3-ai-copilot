/** 余额结果完整页：按 id 读取，带统计与异常区。
 *
 * 对话内嵌的余额明细不走这里 —— 它在产出它的工具条目下由 SourceDetail 渲染。
 */
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Spin, Table, Tooltip } from 'antd'

import { useAuthStore } from '../stores/auth'
import { getChains } from '../api/resources'
import { http } from '../api/client'
import { formatUsd, formatValue } from '../utils/format'
import { Panel, Stat } from './PageParts'
import type { ApiResponse, BalanceLikeResult, BalanceResult as BalanceResultData, Asset } from '../types/api'

/** 已知总估值：余额全部为零，或至少有一项既知道余额又知道价格。 */
function hasKnownTotal(rows: (Asset & { stale: boolean })[]) {
    if (rows.every(a => a.amount === '0')) return true
    return rows.some(a => a.value_usd !== null && a.amount !== null && Number(a.amount) !== 0)
}

export default function BalanceResult({ id, result: provided, onRetry }: {
    id?: string
    result?: BalanceLikeResult
    onRetry?: (chain: number) => void
}) {
    const address = useAuthStore(s => s.address)
    const { data: chainDirectory } = useQuery({ queryKey: ['chains'], queryFn: getChains })

    // 数据随消息下发时直接用，只有按 id 打开的完整页才需要单独读取。
    const { data: fetched, error, isFetching, refetch } = useQuery({
        queryKey: ['balance-result', address, id],
        enabled: !!address && !provided && !!id,
        queryFn: async ({ signal }) =>
            (await http.get<ApiResponse<BalanceResultData>>(`/me/balance-results/${id}`, { signal })).data.data,
    })

    const result = provided ?? fetched
    if (error) {
        return (
            <Alert
                type="warning"
                title={error instanceof Error ? error.message : '结果读取失败'}
                action={<Button size="small" onClick={() => void refetch()}>重新读取</Button>}
            />
        )
    }
    if (!result) return <Spin size="small" />

    const body = result.body
    const currency = result.currency ?? 'USD'
    const complete = result.status === 'complete'
    const failedChains = (result.failed ?? []).filter(f => f.type === 'chain').map(f => Number(f.ref))
    const rows = body.chains.flatMap(c =>
        c.assets.map(a => ({ ...a, chain_id: c.chain_id, chain_name: c.chain_name, stale: c.stale })),
    )
    const knownTotal = hasKnownTotal(rows)
    const scope = body.scope === 'specified_token' ? '指定代币范围' : '已登记代币范围'
    const count = rows.filter(a => a.amount !== '0').length

    /** 总计用的格式：USD 走 $ 前缀，其他计价单位走货币符号。 */
    const totalText = currency === 'USD' ? formatUsd(body.total_value) : formatValue(body.total_value, currency)

    const columns = [
        {
            title: '代币',
            dataIndex: 'symbol',
            render: (v: string) => <div className="coin"><span className="cicon">{v.slice(0, 4)}</span>{v}</div>,
        },
        { title: '链', dataIndex: 'chain_name' },
        {
            title: '余额',
            dataIndex: 'amount',
            align: 'right' as const,
            render: (v: string | null) => <span className="mono">{v ?? '未知'}</span>,
        },
        {
            title: '价格 USD',
            dataIndex: 'price_usd',
            align: 'right' as const,
            render: (v: string | null) => (
                <Tooltip title={v === null ? '暂无价格' : formatUsd(v)}>
                    <span className="mono">{formatUsd(v)}</span>
                </Tooltip>
            ),
        },
        {
            title: '估值 USD',
            dataIndex: 'value_usd',
            align: 'right' as const,
            render: (v: string | null) => (
                <Tooltip title={v === null ? '估值不可用' : formatUsd(v)}>
                    <span className="mono">{formatUsd(v)}</span>
                </Tooltip>
            ),
        },
        {
            title: '链内占比',
            dataIndex: 'percentage',
            render: (v: number | null) => v === null ? '—' : (
                <div>
                    <span className="mono">{v.toFixed(2)}%</span>
                    <div className="bar"><i style={{ width: `${Math.min(100, v)}%` }} /></div>
                </div>
            ),
        },
        {
            title: '状态',
            render: (_: unknown, a: Asset & { stale: boolean }) => {
                const state = a.amount === null
                    ? '余额未知'
                    : a.value_usd === null ? '暂无价格' : a.stale ? '行情缓存' : '正常'
                const tone = a.amount === null || a.value_usd === null || a.stale ? 'soon' : 'live'
                return <span className={`proto-tag ${tone}`}>{state}</span>
            },
        },
    ]

    return (
        <div className="balance-full">
            <div className="proto-grid four-cols" style={{ marginBottom: 14 }}>
                <Stat
                    value={knownTotal ? (
                        <Tooltip title={totalText}><span>{totalText}</span></Tooltip>
                    ) : '估值不可用'}
                    label="已知总估值（不含未计价）"
                />
                <Stat value={count} label="非零或未知余额代币数量" />
                <Stat
                    value={`${body.chains.length} / ${body.chains.length + failedChains.length}`}
                    label="成功网络 / 查询网络"
                    warning={!complete}
                />
                <Stat value={body.unpriced.length} label="未计价代币" warning={!!body.unpriced.length} />
            </div>

            <Panel title="余额明细" sub={scope}>
                <Table
                    size="small"
                    pagination={false}
                    loading={isFetching}
                    dataSource={rows}
                    rowKey={a => `${a.chain_id}:${a.contract ?? 'native'}`}
                    columns={columns}
                    scroll={{ x: 'max-content' }}
                    locale={{ emptyText: <Empty description="查询范围内暂无资产" /> }}
                />
                {!!body.unpriced.length && (
                    <div className="unpriced">
                        <strong>未计价代币（{body.unpriced.length} 项）· 不计入总估值</strong>
                        <p>
                            {body.unpriced
                                .map(a => `${a.symbol} · ${body.chains.find(c => c.chain_id === a.chain_id)?.chain_name ?? a.chain_id}`)
                                .join('、')}
                        </p>
                        <p>没有价格不等于价值为零，未知余额也不会按零计算。</p>
                    </div>
                )}
                <div className="legend">
                    <span>{scope} · {new Date(result.queriedAt).toLocaleString()}</span>
                    <span>
                        已知估值：{knownTotal
                            ? <Tooltip title={totalText}><span>{totalText}</span></Tooltip>
                            : '不可用'}
                    </span>
                    {body.exchange_rate_date && <span>汇率日期：{body.exchange_rate_date}</span>}
                </div>
            </Panel>

            {!complete && (
                <Panel title="部分查询结果" sub="status=partial" className="partial-result">
                    <div className="panel-body">
                        <Alert
                            showIcon
                            type="warning"
                            title={failedChains.length
                                ? `以下网络查询失败：${failedChains.map(cid => chainDirectory?.find(c => c.chain_id === cid)?.name ?? cid).join('、')}`
                                : '存在未知余额、缺失行情或过期缓存'}
                            description="合计只包含成功取得且可计价的部分，不代表全部资产。"
                        />
                        {failedChains.map(cid => (
                            <Button
                                key={cid}
                                size="small"
                                style={{ marginTop: 12, marginRight: 8 }}
                                disabled={!onRetry}
                                onClick={() => onRetry?.(cid)}
                            >
                                重试 {chainDirectory?.find(c => c.chain_id === cid)?.name ?? cid}
                            </Button>
                        ))}
                    </div>
                </Panel>
            )}
        </div>
    )
}
