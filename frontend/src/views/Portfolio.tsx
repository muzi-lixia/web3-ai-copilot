import { Alert, Button, Card, Empty, Space, Switch, Table, Tag, Tooltip, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { DataQualityNotice } from '../components/DataQualityNotice'
import { fetchWalletAssets } from '../api/wallet'
import { ACCENT, DistributionBar } from '../components/DistributionBar'
import { useAuthStore } from '../stores/auth'
import type { Asset } from '../types/api'
import { formatAmount, formatPrice, formatValue, shorten } from '../utils/format'

/** 资产明细表。数据源：GET /wallet/{address}/assets */
export default function Portfolio() {
  const address = useAuthStore((state) => state.address)
  const [showZero, setShowZero] = useState(false)

  const { data, error, isFetching, refetch } = useQuery({
    queryKey: ['wallet-assets', address],
    queryFn: () => fetchWalletAssets(address!),
    enabled: Boolean(address),
  })

  const assets = data?.assets ?? []
  const held = assets.filter((asset) => asset.amount !== '0')
  const rows = showZero ? assets : held

  // 钱包里一个币都没有时总额确实是 $0.00；有币却算不出估值时不能这么显示。
  const totalKnown = Boolean(data) && (held.length === 0 || data!.has_valuation)

  const columns = [
    {
      title: '资产',
      dataIndex: 'symbol',
      render: (symbol: string, asset: Asset) => (
        <Space size={6}>
          <Typography.Text strong>{symbol}</Typography.Text>
          {asset.kind === 'native' && <Tag color="blue">原生</Tag>}
        </Space>
      ),
    },
    {
      title: '价格',
      dataIndex: 'price_usd',
      align: 'right' as const,
      width: 120,
      render: (price: string | null) =>
        price === null ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <Typography.Text>{formatPrice(price)}</Typography.Text>
        ),
    },
    {
      title: `数量（${assets.length} 个候选，${held.filter((asset) => asset.amount !== null).length} 个非零）`,
      dataIndex: 'amount',
      align: 'right' as const,
      render: (amount: string | null) => {
        const shown = formatAmount(amount)
        // 被裁掉的位数放进 Tooltip，需要核对时仍能看到完整值
        return shown === amount ? (
          <Typography.Text>{shown}</Typography.Text>
        ) : (
          <Tooltip title={amount}>
            <Typography.Text>{shown}</Typography.Text>
          </Tooltip>
        )
      },
      // 数量列比其它列更需要宽度：18 位小数的币种很常见
      width: 190,
    },
    {
      title: '估值',
      dataIndex: 'value_usd',
      align: 'right' as const,
      width: 130,
      render: (value: string | null) =>
        value === null ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <Typography.Text strong>{formatValue(value)}</Typography.Text>
        ),
    },
    {
      title: '占比',
      dataIndex: 'percentage',
      align: 'right' as const,
      width: 160,
      render: (percentage: number | null) =>
        percentage === null ? (
          <Typography.Text type="secondary">—</Typography.Text>
        ) : (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'flex-end' }}>
            <div
              style={{
                width: 60,
                height: 6,
                borderRadius: 3,
                background: '#f0f0f0',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${Math.min(percentage, 100)}%`,
                  height: '100%',
                  background: ACCENT,
                }}
              />
            </div>
            <Typography.Text style={{ fontSize: 13, minWidth: 56, textAlign: 'right' }}>
              {percentage.toFixed(2)}%
            </Typography.Text>
          </div>
        ),
    },
    {
      title: '合约',
      dataIndex: 'contract',
      render: (contract: string | null) =>
        contract && data ? (
          <Typography.Text style={{ fontSize: 13 }}>
            <a href={`${data.explorer}/token/${contract}`} target="_blank" rel="noreferrer">
              {shorten(contract)}
            </a>
          </Typography.Text>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            原生币，无合约
          </Typography.Text>
        ),
    },
  ]

  return (
    <Card
      title={
        <Space size={12}>
          <span>资产</span>
          {data && <Tag color="green">{data.chain_name}</Tag>}
        </Space>
      }
      extra={
        <Space size={16}>
          <Space size={6}>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              显示零余额
            </Typography.Text>
            <Switch size="small" checked={showZero} onChange={setShowZero} />
          </Space>
          <Button size="small" onClick={() => refetch()} loading={isFetching}>
            刷新
          </Button>
        </Space>
      }
    >
      <DataQualityNotice data={data} />
      {data && (
        <div style={{ marginBottom: 16 }}>
          <Space align="baseline" size={12}>
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              {data.status === 'complete' ? '钱包资产' : '钱包已知估值（部分）'}
            </Typography.Text>
            {totalKnown ? (
              <Typography.Title level={3} style={{ margin: 0 }}>
                {formatValue(data.total_value_usd)}
              </Typography.Title>
            ) : (
              <Typography.Text type="secondary">估值不可用</Typography.Text>
            )}
            {data.stale && <Tag color="orange">行情缓存</Tag>}
          </Space>
          {data.has_valuation && (
            <div style={{ marginTop: 12 }}>
              <DistributionBar assets={data.assets} />
            </div>
          )}
        </div>
      )}

      <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 16 }}>
        余额为链上实时读取，估值取自公开行情源
        {data && <> · 更新于 {new Date(data.computed_at).toLocaleTimeString()}</>}
        {address && <> · {shorten(address)}</>}
      </Typography.Paragraph>

      {error && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          title="读取资产失败"
          description={error instanceof Error ? error.message : String(error)}
        />
      )}

      {data && data.missing_price.length > 0 && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          title={`${data.missing_price.join('、')} 没有行情，未计入总资产`}
          description="报价可能缺失或数据源暂不可用，本次只展示已知估值。"
        />
      )}

      <Table
        rowKey={(asset) => asset.contract ?? 'native'}
        size="small"
        columns={columns}
        dataSource={rows}
        loading={isFetching && !data}
        pagination={false}
        scroll={{ x: 'max-content' }}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={error ? '数据不可用' : '候选清单里的币种余额均为 0'}
            />
          ),
        }}
      />
    </Card>
  )
}
