import { Card, Typography } from 'antd'

const { Paragraph, Title } = Typography

/** Web3 协议知识库问答。数据源：POST /knowledge/query（V2） */
export default function Knowledge() {
  return (
    <Card>
      <Title level={4}>Knowledge</Title>
      <Paragraph type="secondary">
        文档列表、索引状态、知识库问答（返回答案 + 引用来源）。
        <br />
        待接入：<code>POST /api/v1/knowledge/query</code>（V2，pgvector 检索）
      </Paragraph>
    </Card>
  )
}
