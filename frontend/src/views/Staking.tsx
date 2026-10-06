import { Card, Typography } from 'antd'

const { Paragraph, Title } = Typography

/** 质押仓位表（Protocol / Token / Amount / APY / Reward）。数据源：GET /staking/{address}/positions */
export default function Staking() {
  return (
    <Card>
      <Title level={4}>Staking</Title>
      <Paragraph type="secondary">
        质押仓位列表与质押占比，含「AI 分析」入口（调用 Agent 解读仓位）。
        <br />
        待接入：<code>GET /api/v1/staking/&#123;address&#125;/positions</code>
      </Paragraph>
    </Card>
  )
}
