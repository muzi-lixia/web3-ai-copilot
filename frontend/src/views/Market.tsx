import { Alert, Button, Card, Empty, Space, Table, Tag, Tooltip, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'

import { fetchMarketQuotes } from '../api/market'
import type { MarketSource, TokenMarket } from '../types/api'

/**
 * 金额格式化。
 *
 * 后端把金额序列化成**字符串**（防 float64 掉精度），这里为了排版才转成 number。
 * 价格与市值只用于显示、不参与记账，float64 的 15 位有效数字远超展示需要；
 * 真正要求精确的地方（余额、估值累加）不在这里算。
 */
function toNumber(value: string | null): number | null {
  if (value === null) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/** 价格按量级选精度：$0.2244 和 $83,776.23 套用同一套小数位都不好看。 */
function formatPrice(value: string): string {
  const amount = toNumber(value)
  if (amount === null) return value
  if (amount === 0) return '$0'
  if (amount >= 1000) return `$${amount.toLocaleString('en-US', { maximumFractionDigits: 2 })}`
  if (amount >= 1) return `$${amount.toFixed(4)}`
  // 小于 1 的币种用有效数字而不是固定小数位，否则 0.00001234 会显示成 $0.00
  return `$${amount.toPrecision(4)}`
}

/** 市值/成交量动辄七八位数，缩写后一列放得下；完整值挂 Tooltip 备查。 */
function formatCompact(value: string | null): string {
  const amount = toNumber(value)
  if (amount === null) return '—'
  for (const { limit, suffix } of [
    { limit: 1e12, suffix: 'T' },
    { limit: 1e9, suffix: 'B' },
    { limit: 1e6, suffix: 'M' },
    { limit: 1e3, suffix: 'K' },
  ]) {
    if (amount >= limit) return `$${(amount / limit).toFixed(2)}${suffix}`
  }
  return `$${amount.toFixed(2)}`
}

/**
 * 24h 涨跌。
 *
 * 颜色按国内习惯**涨红跌绿**（与欧美相反）。用内联色值而非 antd 的
 * success/danger 语义色：后者的红绿含义跟着主题走，换个主题就可能被翻转，
 * 而"涨是红"在这个项目里是硬约定，不该由主题决定。
 */
function Change24h({ value }: { value: number | null }) {
  if (value === null) return <Typography.Text type="secondary">—</Typography.Text>
  const color = value > 0 ? '#cf1322' : value < 0 ? '#3f8600' : '#8c8c8c'
  return (
    // tabular-nums 让数字等宽，一列数字才对得齐
    <Typography.Text style={{ color, fontVariantNumeric: 'tabular-nums' }}>
      {value > 0 ? '+' : ''}
      {value.toFixed(2)}%
    </Typography.Text>
  )
}

const SOURCE_TAG: Record<MarketSource, { label: string; color: string; hint: string }> = {
  dexscreener: {
    label: 'DEX',
    color: 'geek blue',
    hint: 'DexScreener · 链上流动性池报价，价格/涨跌/市值/成交量齐全',
  },
  defillama: {
    label: '聚合',
    color: 'purple',
    hint: 'DefiLlama · 只提供价格。该源没有涨跌与市值/成交量，故显示为 —',
  },
}

/** 空值统一显示成破折号：显示成 0 会被误读为"市值真的是 0"。 */
function Dash() {
  return <Typography.Text type="secondary">—</Typography.Text>
}

/** Token 行情表。数据源：GET /market/quotes */
export default function Market() {
  const { data, error, isFetching, refetch } = useQuery({
    queryKey: ['market-quotes'],
    queryFn: () => fetchMarketQuotes(),
    // 后端缓存 60 秒，前端跟着同一个节奏轮询即可 —— 更密只是重复命中同一份缓存
    refetchInterval: 60_000,
  })

  const tokens = data?.tokens ?? []
  const stale = tokens.some((token) => token.stale)
  const updatedAt = tokens.reduce<string | null>(
    (latest, token) => (latest === null || token.updated_at > latest ? token.updated_at : latest),
    null,
  )

  const columns = [
    {
      title: 'Token',
      dataIndex: 'symbol',
      render: (symbol: string, token: TokenMarket) => {
        const tag = SOURCE_TAG[token.source]
        return (
          <Space size={8}>
            <Typography.Text strong>{symbol}</Typography.Text>
            <Tooltip title={tag.hint}>
              <Tag color={tag.color} style={{ marginInlineEnd: 0 }}>
                {tag.label}
              </Tag>
            </Tooltip>
          </Space>
        )
      },
    },
    {
      title: '价格',
      dataIndex: 'price_usd',
      align: 'right' as const,
      render: (price: string) => (
        <Typography.Text style={{ fontVariantNumeric: 'tabular-nums' }}>
          {formatPrice(price)}
        </Typography.Text>
      ),
      width: 160,
    },
    {
      title: '24h 涨跌',
      dataIndex: 'change_24h',
      align: 'right' as const,
      render: (value: number | null) => <Change24h value={value} />,
      width: 130,
    },
    {
      title: '市值',
      dataIndex: 'market_cap',
      align: 'right' as const,
      render: (value: string | null) =>
        value === null ? (
          <Dash />
        ) : (
          <Tooltip title={`$${value}`}>
            <Typography.Text style={{ fontVariantNumeric: 'tabular-nums' }}>
              {formatCompact(value)}
            </Typography.Text>
          </Tooltip>
        ),
      width: 140,
    },
    {
      title: '24h 成交量',
      dataIndex: 'volume_24h',
      align: 'right' as const,
      render: (value: string | null) =>
        value === null ? (
          <Dash />
        ) : (
          <Tooltip title={`$${value}`}>
            <Typography.Text style={{ fontVariantNumeric: 'tabular-nums' }}>
              {formatCompact(value)}
            </Typography.Text>
          </Tooltip>
        ),
      width: 150,
    },
  ]

  return (
    <Card
      title={
        <Space size={12}>
          <span>Token 行情</span>
          {data && <Tag color="green">{data.chain_name}</Tag>}
        </Space>
      }
      extra={
        <Button size="small" onClick={() => refetch()} loading={isFetching}>
          刷新
        </Button>
      }
    >
      <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 16 }}>
        价格来自链上流动性池与聚合行情，不是交易所实时盘口。后端缓存 60 秒
        {updatedAt && <>，本批数据更新于 {new Date(updatedAt).toLocaleTimeString()}</>}。
      </Typography.Paragraph>

      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          title="读取行情失败"
          description={error instanceof Error ? error.message : String(error)}
        />
      )}

      {stale && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          title="行情源暂时不可用，当前显示的是过期缓存"
          description="价格可能已经变动。恢复后会自动刷新。"
        />
      )}

      {data && data.missing.length > 0 && (
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          title={`${data.missing.length} 个 symbol 没有行情：${data.missing.join('、')}`}
          description="所有行情源上都查不到它的报价（例如没有交易池的 BVT），也可能是拼写不在候选清单里。这是市场事实，不是取数失败 —— 所以单独列出，而不是当成价格为 0。"
        />
      )}

      <Table
        rowKey="symbol"
        size="small"
        columns={columns}
        dataSource={tokens}
        loading={isFetching && !data}
        pagination={false}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={error ? '数据不可用' : '暂无行情'}
            />
          ),
        }}
      />
    </Card>
  )
}
