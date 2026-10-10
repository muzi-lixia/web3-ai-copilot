import { Alert, Button, Card, Col, Empty, Row, Space, Tag, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { DataQualityNotice } from '../components/DataQualityNotice'
import { fetchRiskReport } from '../api/risk'
import { fetchStakingPositions } from '../api/staking'
import { fetchWalletAssets } from '../api/wallet'
import { DistributionBar } from '../components/DistributionBar'
import { HoldingsDonut } from '../components/HoldingsDonut'
import { RiskBadge } from '../components/RiskBadge'
import { LEVEL_META } from '../config/presentation'
import { useAuthStore } from '../stores/auth'
import { formatRatio, formatValue, shorten } from '../utils/format'

/** 主要持仓最多列几条。再多就该去 Portfolio 看全表了。 */
const TOP_HOLDINGS = 5

/**
 * 指标卡骨架。
 *
 * 同一行的四张卡必须共用它。风险等级那格的值是个 Tag、其余三格是 24px 数字，
 * 行高天生不一样 —— 各写各的结构，说明文字就会上下错开一格，看着参差。
 *
 * 值区固定高度让"数字/Tag"落在同一条基线上，说明文字用 marginTop:auto 贴底，
 * 于是文案将来变长变短，也推不动这个版式。
 */
function MetricCard({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children: ReactNode
}) {
  // height:100% 与下方 Row 的 align="stretch" 是**配对**的，缺一个就不生效：
  // 少了 stretch，各 Col 按自己内容高；少了这个，卡片在拉齐后的 Col 里顶头。
  return (
    <Card
      size="small"
      style={{ height: '100%' }}
      styles={{ body: { height: '100%', display: 'flex', flexDirection: 'column' } }}
    >
      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
        {title}
      </Typography.Text>
      <div style={{ minHeight: 34, display: 'flex', alignItems: 'center', margin: '4px 0' }}>
        {children}
      </div>
      <Typography.Text type="secondary" style={{ fontSize: 12, marginTop: 'auto' }}>
        {hint ?? ' '}
      </Typography.Text>
    </Card>
  )
}

/** 概览指标块。value 为 null 表示"没算出来"，显示 — 而不是 0。 */
function Stat({ title, value, hint }: { title: string; value: string | null; hint?: string }) {
  return (
    <MetricCard title={title} hint={hint}>
      <span style={{ fontSize: 24, fontWeight: 600 }}>
        {value ?? <Typography.Text type="secondary">—</Typography.Text>}
      </span>
    </MetricCard>
  )
}

/**
 * 总览页：把资产、风险、质押三块的**结论**聚在一屏，明细去各自页面。
 *
 * queryKey 与 Portfolio / Risk 页完全一致 —— react-query 按 key 共享缓存，
 * 从这里点进明细页不会重新请求，从明细页返回也直接命中缓存。
 *
 * 两个请求分开取、分开报错。余额来自自家 RPC、风险报告还要在余额之上再取一次行情，
 * 两者的失败原因和可恢复性都不一样：前者失败这页基本没意义，后者失败只是少了
 * 一层解读，不该把已经读到的总资产一起抹掉。
 */
export default function Dashboard() {
  const address = useAuthStore((state) => state.address)

  const assetsQuery = useQuery({
    queryKey: ['wallet-assets', address],
    queryFn: () => fetchWalletAssets(),
    enabled: Boolean(address),
  })
  const riskQuery = useQuery({
    queryKey: ['risk-report', address],
    queryFn: () => fetchRiskReport(),
    enabled: Boolean(address),
  })
  const stakingQuery = useQuery({
    queryKey: ['staking-positions', address],
    queryFn: () => fetchStakingPositions(),
    enabled: Boolean(address),
  })

  const data = assetsQuery.data
  const report = riskQuery.data
  const staking = stakingQuery.data

  const held = (data?.assets ?? []).filter((asset) => asset.amount !== '0')
  // 只把有估值的持仓喂给环形图。占比为 null 的（取不到行情）不进环 ——
  // "不知道该占多少"和"占 0%"是两回事，硬塞进去会让环上多一块没有依据的颜色。
  const valued = held.filter((asset) => (asset.percentage ?? 0) > 0)

  // 钱包确实一个币都没有时总额是 $0.00；有币却算不出估值时不能这么显示。
  const totalKnown = Boolean(data) && (held.length === 0 || data!.has_valuation)

  // unknown 是"输入不足"，不是"低风险"。此时风险指标一律显示 —，
  // 把 0 摆上去会读成"确实没有稳定币"，而我们其实一个都没测到。
  const evaluated = Boolean(report) && report!.risk_level !== 'unknown'

  // 质押那一格：链上确实没仓位时是 $0.00；有仓位却取不到价时才说「估值不可用」。
  const stakingValued = Boolean(staking) && staking!.missing_price.length === 0
    && !staking!.issues.some((issue) => issue.code === 'staking_read_failed')
  const stakingHint = !staking
    ? undefined
    : staking.issues.some((issue) => issue.code === 'staking_read_failed')
      ? '部分仓位读取失败'
      : staking.positions.length === 0
        ? '该链未接入质押模块'
        : staking.portfolio_ratio !== null
          ? `占组合 ${formatRatio(staking.portfolio_ratio, 1)}`
          : '估值不完整或组合为空，占比不可算'

  return (
    <Space orientation="vertical" size={16} style={{ display: 'flex' }}>
      <Card
        title={
          <Space size={12}>
            <span>总览</span>
            {data && <Tag color="green">{data.chain_name}</Tag>}
          </Space>
        }
        extra={
          <Space size={12}>
            <Button
              size="small"
              loading={assetsQuery.isFetching || riskQuery.isFetching || stakingQuery.isFetching}
              onClick={() => {
                void assetsQuery.refetch()
                void riskQuery.refetch()
                void stakingQuery.refetch()
              }}
            >
              刷新
            </Button>
            <Link to="/portfolio">查看全部持仓 →</Link>
          </Space>
        }
        loading={assetsQuery.isLoading}
      >
        {assetsQuery.error && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            title="读取资产失败"
            description={
              assetsQuery.error instanceof Error
                ? assetsQuery.error.message
                : String(assetsQuery.error)
            }
          />
        )}

        <DataQualityNotice data={data} />
        {data && (
          <>
            <Space align="baseline" size={12}>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                {data.status === 'complete' ? '钱包资产' : '钱包已知估值（部分）'}
              </Typography.Text>
              {totalKnown ? (
                <Typography.Title level={2} style={{ margin: 0 }}>
                  {formatValue(data.total_value_usd)}
                </Typography.Title>
              ) : (
                <Typography.Text type="secondary">估值不可用</Typography.Text>
              )}
              {data.stale && <Tag color="orange">行情缓存</Tag>}
            </Space>

            {data.has_valuation && (
              <div style={{ marginTop: 12, maxWidth: 560 }}>
                <DistributionBar assets={data.assets} height={12} />
              </div>
            )}

            <Typography.Paragraph
              type="secondary"
              style={{ fontSize: 13, marginTop: 12, marginBottom: 0 }}
            >
              余额为链上实时读取，估值取自公开行情源 · 更新于{' '}
              {new Date(data.computed_at).toLocaleTimeString()}
              {address && <> · {shorten(address)}</>}
            </Typography.Paragraph>
          </>
        )}

        {data && data.missing_price.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 16 }}
            title={`${data.missing_price.join('、')} 没有行情，未计入总资产`}
            description="报价可能缺失或数据源暂不可用，本次只展示已知估值。"
          />
        )}
      </Card>

      <Card
        title="风险与结构"
        extra={<Link to="/risk">查看判据 →</Link>}
        loading={riskQuery.isLoading}
      >
        {riskQuery.error && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            title="读取风险报告失败"
            description={
              riskQuery.error instanceof Error ? riskQuery.error.message : String(riskQuery.error)
            }
          />
        )}

        <DataQualityNotice data={report} />
        <DataQualityNotice data={staking} />
        {report && !evaluated && (
          <Typography.Paragraph type="secondary" style={{ fontSize: 13, marginBottom: 16 }}>
            持仓为空或数据不完整，风险指标无法计算。
          </Typography.Paragraph>
        )}

        {/* align="stretch" 让本行 Col 等高，配合 MetricCard 的 height:100% 才拉齐。 */}
        <Row gutter={[16, 16]} align="stretch">
          <Col xs={24} sm={12} lg={6}>
            <MetricCard title="风险等级" hint={report ? LEVEL_META[report.risk_level].hint : ' '}>
              {report ? (
                <RiskBadge level={report.risk_level} />
              ) : (
                <Typography.Text type="secondary">—</Typography.Text>
              )}
            </MetricCard>
          </Col>
          <Col xs={24} sm={12} lg={6}>
            <Stat
              title="稳定币占比"
              value={evaluated ? formatRatio(report!.stablecoin_ratio) : null}
              hint="行情剧烈时的可用缓冲"
            />
          </Col>
          <Col xs={24} sm={12} lg={6}>
            <Stat
              title="高波动资产占比"
              value={evaluated ? formatRatio(report!.volatile_ratio) : null}
              hint="与稳定币互补"
            />
          </Col>
          <Col xs={24} sm={12} lg={6}>
            <Stat
              title="质押价值"
              value={
                !staking
                  ? null
                  : staking.positions.length === 0
                    ? formatValue('0')
                    : stakingValued
                      ? formatValue(staking.staked_value_usd)
                      : null
              }
              hint={stakingHint}
            />
          </Col>
        </Row>
      </Card>

      <Card
        title="主要持仓"
        extra={<Link to="/portfolio">查看全部 →</Link>}
        loading={assetsQuery.isLoading}
      >
        {!data || valued.length === 0 ? (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              held.length === 0 ? '候选清单里的币种余额均为 0' : '持仓没有行情，无法计算构成'
            }
          />
        ) : (
          <HoldingsDonut
            assets={valued}
            totalValueUsd={data.total_value_usd}
            topCount={TOP_HOLDINGS}
          />
        )}
      </Card>
    </Space>
  )
}
