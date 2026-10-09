import type { ChatMessage } from '../types/api'

/** 消息按稳定 ID 合并，右侧较新数据优先；重复加载同一页不会重复插入。 */
export function mergeMessages(older: ChatMessage[], newer: ChatMessage[]) {
  const byId = new Map([...older, ...newer].map((message) => [message.id, message]))
  return [...byId.values()].sort((a, b) => a.seq - b.seq)
}

/** 逐项说明上下文降级原因，摘要生成失败不能被误报为压缩已经成功。 */
export function contextIssueMessages(issues: string[]) {
  const messages: Record<string, string> = {
    summary_failed: '本轮摘要生成失败，保留原历史和已有摘要继续回答',
    context_trimmed: '为满足模型输入预算，本轮省略了部分较早历史，原文仍保存在服务器',
    summary_omitted: '摘要仍超出预算，本轮未使用摘要，原文仍保存在服务器',
  }
  return [...new Set(issues)].map((issue) => messages[issue] ?? `上下文处理提示：${issue}`)
}
