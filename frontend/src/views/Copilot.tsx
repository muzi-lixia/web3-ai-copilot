import { Card, Col, Row, Typography } from 'antd'

const { Paragraph, Title } = Typography

/**
 * 核心页面：左侧对话，右侧 Agent 执行轨迹。
 *
 * 待接入：
 *   POST /api/v1/agent/chat        （对话，V2 起走 SSE 流式）
 *   POST /api/v1/agent/analyze-portfolio
 * 前端配套：hooks/useAgentStream.ts + stores/agentStore.ts + components/AgentTrace.tsx
 */
export default function Copilot() {
  return (
    <Row gutter={16}>
      <Col span={15}>
        <Card title="对话">
          <Title level={5}>Copilot</Title>
          <Paragraph type="secondary">
            自然语言提问，Agent 自动判断意图并选择工具。
          </Paragraph>
        </Card>
      </Col>
      <Col span={9}>
        <Card title="Agent 执行过程">
          <Paragraph type="secondary">
            工具调用时间轴：获取钱包资产 → 查询价格 → 计算风险 → 生成报告。
          </Paragraph>
        </Card>
      </Col>
    </Row>
  )
}
