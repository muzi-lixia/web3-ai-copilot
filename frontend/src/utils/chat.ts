import type { ChatMessage } from '../types/api'

/** 消息按稳定 ID 合并，右侧较新数据优先；重复加载同一页不会重复插入。 */
export function mergeMessages(older: ChatMessage[], newer: ChatMessage[]) {
  const byId = new Map([...older, ...newer].map((message) => [message.id, message]))
  return [...byId.values()].sort((a, b) => a.seq - b.seq)
}

/** 执行阶段用于展示，不等同于数据库轮次状态；展示当前 queued/running 状态。 */
export function turnProgressText(phase?: string) {
  const labels: Record<string, string> = {
    queued: '正在等待模型额度…',
    running: '正在查询并生成回复…',
  }
  return labels[phase ?? 'queued'] ?? '正在处理回复…'
}
