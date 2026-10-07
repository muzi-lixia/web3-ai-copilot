import { Alert, Card, Col, Empty, Row, Space, Tag, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'

import { fetchRiskReport } from '../api/risk'
import { LEVEL_META, RiskBadge } from '../components/RiskBadge'
import { useAuthStore } from '../stores/auth'
import { formatRatio } from '../utils/format'

/** 单项指标。value 为 null 表示"没测"，显示 — 而不是 0。 */
function Metric({ title, value, hint }: { title: string; value: string | null; hint?: string }) {
  // height: 100% 与 Group 里 Row 的 align="stretch" 是**配对**的，缺一个就不生效：
  // 少了 stretch，各 Col 按自己内容高；少了这个，卡片在拉齐后的 Col 里顶头。
  return (
    <Card size="small" style={{ height: '100%' }}>
      <Typography.Text type="secondary" style={{ fontSize: 13 }}>
        {title}
      </Typography.Text>
      <div style={{ fontSize: 24, fontWeight: 600, margin: '4px 0' }}>
        {value ?? <Typography.Text type="secondary">—</Typography.Text>}
      </div>
      {hint && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {hint}
        </Typography.Text>
      )}
    </Card>
  )
}

/** 一组指标带小标题。分组是必要的 —— 见下面两套切分的说明。 */
function Group({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return (
    <Space orientation="vertical" size={8} style={{ display: 'flex' }}>
      <Space size={8} wrap align="baseline">
        <Typography.Text strong style={{ fontSize: 13 }}>
          {title}
        </Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {note}
        </Typography.Text>
      </Space>
      {/* Row 默认 align="top"（即 flex-start），卡片按各自内容高度排 ——
          提示文案有长有短时，同一行里就会参差不齐。stretch 让本行 Col 全部等高。 */}
      <Row gutter={[16, 16]} align="stretch">
        {children}
      </Row>
    </Space>
  )
}

/**
 * 资产风险分析。数据源：GET /risk/{address}/report
 *
 * 四个比例分成**两套切分**，各自完整、回答的问题不同：
 *
 *     稳定币 + 高波动 = 100%     按价格波动性切
 *     质押   + 可动用 = 100%     按能否即时动用切
 *
 * 不分组地把五个数摆一排，"加起来不是 100%"会被当成漏算。
 */
export default function Risk() {
  const address = useAuthStore((state) => state.address)

  const { data, error, isFetching } = useQuery({
    queryKey: ['risk-report', address],
    queryFn: () => fetchRiskReport(address!),
    enabled: Boolean(address),
  })

  // unknown 表示输入不足（没有可估值的持仓），不是"低风险"的近义词。
  const evaluated = Boolean(data) && data!.risk_level !== 'unknown'
  const level = data ? LEVEL_META[data.risk_level] : null

  return (
    <Space orientation="vertical" size={16} style={{ display: 'flex' }}>
      <Card title="资产风险" loading={isFetching && !data}>
        {error && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 16 }}
            title="读取风险报告失败"
            description={error instanceof Error ? error.message : String(error)}
          />
        )}

        {data && !evaluated && (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="没有可估值的持仓，无法评估风险"
          />
        )}

        {data && evaluated && level && (
          <Space orientation="vertical" size={8} style={{ display: 'flex' }}>
            <Space align="center" size={10} wrap>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                风险等级
              </Typography.Text>
              <RiskBadge level={data.risk_level} />
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                {level.hint}
              </Typography.Text>
            </Space>

            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              最大持仓 {data.top_asset} · {formatRatio(data.top_asset_ratio)}（按估值，
              质押仓位按份额凭证单列）
            </Typography.Text>
          </Space>
        )}
      </Card>

      {data && evaluated && (
        <>
          <Card
            title="结构指标"
            extra={
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                每套切分内部各自合计 100%
              </Typography.Text>
            }
          >
            <Space orientation="vertical" size={20} style={{ display: 'flex' }}>
              <Group title="按价格波动性" note="回答「行情剧烈时有多大敞口」">
                <Col xs={24} sm={8}>
                  <Metric
                    title="风险资产集中度"
                    value={data.concentration_score.toFixed(1)}
                    hint="波动资产内部的 HHI，0-100，越高越集中"
                  />
                </Col>
                <Col xs={24} sm={8}>
                  <Metric
                    title="稳定币占比"
                    value={formatRatio(data.stablecoin_ratio)}
                    hint="行情剧烈时的可用缓冲"
                  />
                </Col>
                <Col xs={24} sm={8}>
                  <Metric
                    title="高波动资产占比"
                    value={formatRatio(data.volatile_ratio)}
                    hint="与稳定币互补"
                  />
                </Col>
              </Group>

              <Group
                title="按能否即时动用"
                note="质押部分在解绑期内取不出来，这部分与上一套不是同一刀"
              >
                <Col xs={24} sm={12}>
                  <Metric
                    title="质押占比"
                    value={formatRatio(data.staking_ratio)}
                    hint={
                      data.staking_ratio === null
                        ? '没测到：有仓位但取不到价，或资产侧读取失败'
                        : '质押价值 ÷（质押 + 未质押资产）'
                    }
                  />
                </Col>
                <Col xs={24} sm={12}>
                  <Metric
                    title="可动用占比"
                    value={formatRatio(data.liquid_ratio)}
                    hint="与质押占比互补"
                  />
                </Col>
              </Group>
            </Space>
          </Card>

          <Card size="small">
            <Space orientation="vertical" size={4}>
              <Space size={8}>
                <Tag>判据</Tag>
                <Typography.Text style={{ fontSize: 13 }}>
                  命中条数决定档位：风险资产集中度 ≥ 70 · 高波动 ≥ 80% · 稳定币 &lt; 10%
                </Typography.Text>
              </Space>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                命中 0 / 1 / 2+ 条分别对应低 / 中 / 高。数值全部由代码计算，可复现、可逐条复核；
                LLM 只负责把它写成自然语言（待接入）。 计算于{' '}
                {new Date(data.computed_at).toLocaleTimeString()}。
              </Typography.Text>
            </Space>
          </Card>
        </>
      )}
    </Space>
  )
}
