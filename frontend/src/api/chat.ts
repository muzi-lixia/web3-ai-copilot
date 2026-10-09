/**
 * 持久化聊天的前端请求层。
 * 普通请求复用 Axios 的鉴权/错误拦截；SSE 使用 fetch，必须自行处理鉴权失效。
 * 前端不提交历史或地址，只传资源 ID、当前问题与幂等 ID，由后端构造上下文和校验归属。
 */
import { ApiError, http } from './client.ts'
import { responseError } from './response.ts'
import { consumeSSE, parseSSEEvent, SSEStreamError } from './sse.ts'
import { useAuthStore } from '../stores/auth.ts'

import type { ApiResponse, ChatSession, MessagePage, TurnAccepted, TurnSnapshot } from '../types/api'

/** 打开本人固定会话：重复请求返回同一 ID，不再提供新建多会话接口。 */
export async function openSession(signal?: AbortSignal) {
  return (await http.put<ApiResponse<ChatSession>>('/chat/session', {}, { signal })).data.data
}
/** 历史只有一个入口，身份来自 JWT；游标用于向前加载消息。 */
export async function loadMessages(beforeSeq?: number, signal?: AbortSignal) {
  return (await http.get<ApiResponse<MessagePage>>('/chat/session/messages', {
    params: { before_seq: beforeSeq }, signal,
  })).data.data
}
/** 接受确认丢失后复用同一 clientId，不重复生成。 */
export async function submitTurn(message: string, clientId: string) {
  return (await http.post<ApiResponse<TurnAccepted>>('/chat/session/turns', {
    message, client_msg_id: clientId,
  })).data.data
}
/** 显式流错误后查询真实状态；不能把连接断开当作模型已经停止。 */
export async function getTurn(id: string, signal?: AbortSignal) {
  return (await http.get<ApiResponse<TurnSnapshot>>(`/chat/turns/${id}`, { signal })).data.data
}
/** 主动重试是一轮新生成，用新的 clientId；服务器复用原问题及原模型输入。 */
export async function retryTurn(id: string, clientId: string) {
  return (await http.post<ApiResponse<TurnAccepted>>('/chat/session/turns', { retry_of: id, client_msg_id: clientId })).data.data
}
/** 停止生成必须调用这个接口；仅 AbortController.abort() 只会结束本地订阅。 */
export async function cancelTurn(id: string) {
  return (await http.put<ApiResponse<TurnSnapshot>>(`/chat/turns/${id}/cancellation`, {}, { timeout: 190_000 })).data.data
}

/** 可被取消的重连退避；离开页面时清理定时器和事件监听，不再继续发请求。 */
async function delay(ms: number, signal: AbortSignal) {
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
  await new Promise<void>((resolve, reject) => {
    const aborted = () => {
      clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', aborted)
      resolve()
    }, ms)
    signal.addEventListener('abort', aborted, { once: true })
  })
}

/**
 * 订阅轮次并在网络中断后自动重连。
 * 每次连接先收完整快照，因此恢复时调用方替换正文，不能把快照当增量追加。
 * done 是唯一正常结束标志；没有终态就断开会进入退避重连，最多等待 15 秒后再次尝试。
 * 4xx 参数、身份或资源错误，不无限重试；用户主动 abort 则正常退出。
 */
export async function followTurn(
  id: string, signal: AbortSignal,
  onSnapshot: (snapshot: TurnSnapshot) => void,
  onReconnect: (reconnecting: boolean) => void,
) {
  let failures = 0
  while (!signal.aborted) {
    try {
      // 每次重连取当前 token；旧请求的 401 不能清掉用户刚换上的新凭证。
      const token = useAuthStore.getState().token
      const baseURL = (http.defaults.baseURL ?? "/api/v1").replace(/\/+$/, "")
      const response = await fetch(`${baseURL}/chat/turns/${id}/events`, {
        headers: { Authorization: `Bearer ${token ?? ''}` }, signal,
      })
      if (!response.ok) {
        if (response.status === 401 && useAuthStore.getState().token === token) {
          useAuthStore.getState().clearAuth()
        }
        const body = await response.json().catch(() => null)
        throw responseError(body, response.status, '无法连接会话')
      }
      if (!response.headers.get('Content-Type')?.includes('text/event-stream') || !response.body) {
        throw new SSEStreamError('服务器没有返回有效对话流，请检查连接后重试')
      }
      onReconnect(false)
      // SSE 解析器负责 HTTP 分块与中文 UTF-8 拼接，这里只处理已经完整的一帧 JSON。
      await consumeSSE(response.body, (data) => {
        const event = parseSSEEvent(data)
        if (event.type === 'snapshot' || event.type === 'done') {
          failures = 0
          onSnapshot(event)
        }
        return event.type === 'done'
      })
      return
    } catch (error) {
      if (signal.aborted) return
      if (error instanceof SSEStreamError) throw error
      if (error instanceof ApiError && error.status !== undefined && error.status >= 400 &&
        error.status < 500 && ![408, 429].includes(error.status)) throw error
      // 指数退避避免断网时密集请求；收到有效快照后把失败次数归零。
      onReconnect(true)
      await delay(Math.min(1000 * 2 ** Math.min(failures++, 5), 15000), signal)
    }
  }
}
