import { Card, Typography } from 'antd'

const { Paragraph, Title } = Typography

/** 资产明细表 + 占比/稳定币比例。数据源：GET /wallet/{address}/assets + GET /market/tokens */
export default function Portfolio() {
  return (
    <Card>
      <Title level={4}>Portfolio</Title>
      <Paragraph type="secondary">
        资产列表（Token / Balance / Price / Value / 24h）、代币分布、稳定币比例。
        <br />
        待接入：<code>GET /api/v1/wallet/&#123;address&#125;/assets</code>
      </Paragraph>
    </Card>
  )
}
