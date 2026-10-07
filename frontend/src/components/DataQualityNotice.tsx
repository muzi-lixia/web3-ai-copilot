import { Alert } from 'antd'
import type { DataQuality } from '../types/api'

export function DataQualityNotice({ data }: { data?: DataQuality }) {
  if (!data || data.status === 'complete') return null
  return <Alert type="warning" showIcon style={{ marginBottom: 16 }}
    title={data.status === 'unavailable' ? '数据不足，无法评估' : '数据不完整'}
    description={[...new Set(data.issues.map((issue) => issue.message))].join('；')} />
}
