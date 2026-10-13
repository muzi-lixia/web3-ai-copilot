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

/** 轮次失败时可能出现的错误码。取消与超时写的是中文说明，不在表内、原样展示。 */
const ERROR_TEXT: Record<string, string> = {
  agent_step_limit: '这次查询步骤超出上限，请把问题拆小一点再问',
  model_unavailable: '模型服务暂不可用，请稍后重试',
  model_context_too_large: '对话上下文已超出模型窗口，请清空对话后重试',
  upstream_error: '数据源暂不可用，请稍后重试',
  foundation_unavailable: '基础服务暂不可用，请稍后重试',
  business_query_failed: '上游查询未完成，请稍后重试',
  unauthorized: '登录状态已失效，请重新连接钱包',
  access_expired: '登录凭证已过期，请重新登录',
  forbidden: '当前身份无权进行这次查询',
  rate_limited: '请求过于频繁，请稍后重试',
  chat_busy: '当前对话已有执行中的轮次',
  chat_unavailable: '对话服务未就绪，请稍后重试',
  not_found: '要查询的轮次或资源不存在',
  unsupported_chain: '请求的网络不在支持列表内',
}

/** 错误码转可读文案；未知码原样返回，便于对着日志排查。 */
export function errorText(value?: string | null) {
  return value ? (ERROR_TEXT[value] ?? value) : null
}
