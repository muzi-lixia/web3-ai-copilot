import { Alert, Card, Empty, Space, Table, Tag, Tooltip, Typography } from 'antd'
import type { TableProps } from 'antd'
import { useQuery } from '@tanstack/react-query'

import { DataQualityNotice } from '../components/DataQualityNotice'
import { fetchStakingPositions } from '../api/staking'
import { useAuthStore } from '../stores/auth'
import type { PendingWithdrawal, StakingPosition } from '../types/api'
import { formatAmount, formatRatio, formatValue } from '../utils/format'

/** 把秒数说成人话。解绑期是 7 天，用秒表达对用户没有任何意义。 */
function humanDuration(seconds: number): string {
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  if (days > 0) return hours > 0 ? `${days} 天 ${hours} 小时` : `${days} 天`
  const minutes = Math.round((seconds % 3600) / 60)
  return hours > 0 ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟`
}

/**
 * 距可提取还有多久。
 *
 * 由前端按 `unlock_at` 现算，而不是直接用服务端的 `ready`：那个布尔值是
 * **取数那一刻**的结论，页面开着几小时就会过期。纯展示判断，不参与任何计算。
 */
function remainingText(unlockAt: string): string {
  const seconds = (new Date(unlockAt).getTime() - Date.now()) / 1000
  return seconds <= 0 ? '已可提取' : `剩余 ${humanDuration(seconds)}`
}

const positionColumns: TableProps<StakingPosition>['columns'] = [
  {
    title: '协议',
    key: 'name',
    render: (_, row) => (
      <Space orientation="vertical" size={0}>
        <Typography.Text strong>{row.name}</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {row.protocol}
        </Typography.Text>
      </Space>
    ),
  },
  {
    title: (
      <Tooltip title="收益是「每份更值钱」而不是「发更多份」，所以这个数会一直变大">
        <span>份额 / 折算</span>
      </Tooltip>
    ),
    key: 'shares',
    align: 'right',
    render: (_, row) => (
      <Space orientation="vertical" size={0} style={{ alignItems: 'flex-end' }}>
        <Typography.Text>
          {formatAmount(row.shares)} {row.share_symbol}
        </Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          = {formatAmount(row.underlying_amount)} {row.underlying_symbol}
        </Typography.Text>
      </Space>
    ),
  },
  {
    title: '每份值多少',
    key: 'exchange_rate',
    align: 'right',
    render: (_, row) =>
      row.exchange_rate === null ? (
        <Typography.Text type="secondary">—</Typography.Text>
      ) : (
        <Typography.Text>
          {formatAmount(row.exchange_rate)} {row.underlying_symbol}
        </Typography.Text>
      ),
  },
  {
    title: '年化',
    key: 'apy',
    align: 'right',
    render: (_, row) => (
      <Space orientation="vertical" size={0} style={{ alignItems: 'flex-end' }}>
        {row.apy === null ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <Typography.Text>{formatRatio(row.apy, 2)}</Typography.Text>
        )}
        {row.apy_interval && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {row.apy_interval === 'ONE_DAY' ? '近 24 小时口径' : `${row.apy_interval} 口径`}
          </Typography.Text>
        )}
      </Space>
    ),
  },
  {
    title: '累计收益',
    key: 'earnings',
    align: 'right',
    render: (_, row) =>
      row.earnings === null ? (
        <Tooltip title="上游表示该地址的历史流水不足以精确回溯，所以算不出来 —— 不是零">
          <Typography.Text type="secondary">—</Typography.Text>
        </Tooltip>
      ) : (
        <Space orientation="vertical" size={0} style={{ alignItems: 'flex-end' }}>
          <Typography.Text>
            {formatAmount(row.earnings.total)} {row.underlying_symbol}
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            已落袋 {formatAmount(row.earnings.realized)} · 浮动{' '}
            {formatAmount(row.earnings.unrealized)}
          </Typography.Text>
        </Space>
      ),
  },
  {
    title: '估值',
    key: 'value_usd',
    align: 'right',
    render: (_, row) => (
      <Space orientation="vertical" size={0} style={{ alignItems: 'flex-end' }}>
        <Typography.Text strong>{formatValue(row.value_usd)}</Typography.Text>
        {row.price_usd !== null && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            1 {row.underlying_symbol} = {formatValue(row.price_usd)}
          </Typography.Text>
        )}
      </Space>
    ),
  },
]

const withdrawalColumns: TableProps<PendingWithdrawal>['columns'] = [
  {
    title: '锁定量 / 已烧份额',
    key: 'assets',
    render: (_, row) => (
      <Space orientation="vertical" size={0}>
        <Typography.Text>{formatAmount(row.assets)}</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          份额 {formatAmount(row.shares)}
        </Typography.Text>
      </Space>
    ),
  },
  {
    title: '估值',
    key: 'value_usd',
    align: 'right',
    render: (_, row) => formatValue(row.value_usd),
  },
  {
    title: '发起时间',
    key: 'requested_at',
    render: (_, row) => new Date(row.requested_at).toLocaleString(),
  },
  {
    title: '可提取',
    key: 'unlock',
    render: (_, row) => (
      <Space orientation="vertical" size={0}>
        <Typography.Text>{new Date(row.unlock_at).toLocaleString()}</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {remainingText(row.unlock_at)}
        </Typography.Text>
      </Space>
    ),
  },
  {
    title: '状态',
    key: 'ready',
    align: 'right',
    render: (_, row) => (row.ready ? <Tag color="green">可提取</Tag> : <Tag>解绑中</Tag>),
  },
]

/**
 * 质押仓位。数据源：GET /staking/{address}/positions
 *
 * 这一页只**展示**。质押与赎回都要用户签名交易，那是钱包的事 ——
 * 后台不持钥，也不该摆一个不签名的"操作"按钮出来。
 * 页面的职责是把仓位讲清楚：存了多少、现在值多少、收益从哪来、什么时候能取。
 */
export default function Staking() {
  const address = useAuthStore((state) => state.address)

  const { data, error, isFetching } = useQuery({
    queryKey: ['staking-positions', address],
    queryFn: () => fetchStakingPositions(address!),
    enabled: Boolean(address),
  })

  const positions = data?.positions ?? []
  const hasPosition = positions.length > 0
  // 有仓位却取不到价 → 显示「估值不可用」；链上确实什么都没有 → $0.00。两回事。
  const valued = Boolean(data) && data!.missing_price.length === 0
    && !data!.issues.some((issue) => issue.code === 'staking_read_failed')
  const unbonding = positions[0]?.unbonding_seconds

  return (
    <Space orientation="vertical" size={16} style={{ display: 'flex' }}>
      <Card
        title={
          <Space size={12}>
            <span>质押</span>
            {data && <Tag color="green">{data.chain_name}</Tag>}
            {data?.stale && <Tag color="orange">行情缓存</Tag>}
          </Space>
        }
        loading={isFetching && !data}
      >
        <DataQualityNotice data={data} />
        {error && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            message="读取质押仓位失败"
            description={error instanceof Error ? error.message : String(error)}
          />
        )}

        {data && !hasPosition && (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={data.issues.some((issue) => issue.code === 'staking_read_failed')
              ? "仓位读取不完整" : "该链尚未接入质押模块，或该地址没有质押仓位"}
          />
        )}

        {data && hasPosition && (
          <>
            <Space align="baseline" size={12} wrap>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                质押价值
              </Typography.Text>
              {valued ? (
                <Typography.Title level={2} style={{ margin: 0 }}>
                  {formatValue(data.staked_value_usd)}
                </Typography.Title>
              ) : (
                <Typography.Text type="secondary">估值不可用</Typography.Text>
              )}
              {data.portfolio_ratio !== null && (
                <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                  占组合 {formatRatio(data.portfolio_ratio, 2)}
                  {data.liquid_value_usd !== null && (
                    <> · 未质押部分 {formatValue(data.liquid_value_usd)}</>
                  )}
                </Typography.Text>
              )}
            </Space>

            <Typography.Paragraph
              type="secondary"
              style={{ fontSize: 13, marginTop: 12, marginBottom: 0 }}
            >
              收益以「每份更值钱」的形式发放 —— 协议把 PoL 激励回购成底层资产注入金库并自动复利，
              所以份额数量不变、单价持续上涨，上面的「每份值多少」就是这个结果。
              {unbonding !== undefined && (
                <> 解绑期 {humanDuration(unbonding)}，排队中的份额已烧掉、不再计息。</>
              )}{' '}
              计算于 {new Date(data.computed_at).toLocaleTimeString()}。
            </Typography.Paragraph>
          </>
        )}

        {data && data.missing_price.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 16 }}
            message={`${data.missing_price.join('、')} 没有行情，质押价值未计入`}
            description="有质押仓位但底层资产取不到价 —— 此时数量是准的，金额不是。"
          />
        )}
      </Card>

      {hasPosition && (
        <Card title="仓位明细" loading={isFetching && !data}>
          <Table
            rowKey="module_key"
            columns={positionColumns}
            dataSource={positions}
            pagination={false}
            size="small"
          />
        </Card>
      )}

      {hasPosition && (
        <Card title="提款队列" loading={isFetching && !data}>
          {positions.every((position) => position.pending_withdrawals.length === 0) ? (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={data?.issues.some((issue) => issue.code === 'staking_read_failed')
              ? "已读取的仓位没有赎回；失败模块的提款队列未知" : "没有排队中的赎回"} />
          ) : (
            positions
              .filter((position) => position.pending_withdrawals.length > 0)
              .map((position) => (
                <div key={position.module_key} style={{ marginBottom: 16 }}>
                  <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                    {position.name} · 锁定的 {position.underlying_symbol}
                    不在上面的份额里（份额已烧掉），但已计入质押价值
                  </Typography.Text>
                  <Table
                    rowKey="request_id"
                    columns={withdrawalColumns}
                    dataSource={position.pending_withdrawals}
                    pagination={false}
                    size="small"
                    style={{ marginTop: 8 }}
                  />
                </div>
              ))
          )}
        </Card>
      )}
    </Space>
  )
}
