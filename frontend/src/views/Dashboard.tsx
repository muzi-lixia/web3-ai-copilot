import { Card, Typography } from 'antd'

const { Paragraph, Title } = Typography

/** 总资产 / 资产分布 / 风险等级 / 质押价值。数据源：GET /wallet/{address}/assets */
export default function Dashboard() {
  return (
    <Card>
      <Title level={4}>Dashboard</Title>
      <Paragraph type="secondary">
        总资产、资产分布饼图、风险等级、质押价值概览。
        <br />
        待接入：<code>GET /api/v1/wallet/&#123;address&#125;/assets</code>
      </Paragraph>
    </Card>
  )
}
