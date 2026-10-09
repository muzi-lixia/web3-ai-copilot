/** 普通 JSON 响应校验与错误转换；Axios 和 SSE 建连失败共用，流事件单独解析。 */
import type { ApiResponse } from '../types/api'

export class ApiError extends Error {
  readonly code: number | string
  readonly status?: number

  constructor(message: string, code: number | string = 'unknown', status?: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

/** 校验约定，避免旧裸数据或代理 HTML 被页面误当成有效业务结果。 */
function isEnvelope(body: unknown): body is ApiResponse<unknown> {
  return typeof body === 'object' && body !== null &&
    'code' in body && typeof body.code === 'number' && Number.isInteger(body.code) &&
    'msg' in body && typeof body.msg === 'string' && 'data' in body
}

/** HTTP 2xx 仍须检查业务 code；保留真正数据的 null、空数组、0 等值，不用真假值判断。 */
export function unwrapResponse<T>(body: unknown, status?: number): T {
  if (!isEnvelope(body)) throw new ApiError('服务器响应格式不正确', 'invalid_response', status)
  if (body.code !== 0) throw new ApiError(body.msg || '请求失败', body.code, status)
  return body.data as T
}

/** HTTP 错误优先展示后端 msg，无法解析时保留网络原因和 HTTP 状态。 */
export function responseError(body: unknown, status?: number, fallback = '网络请求失败'): ApiError {
  if (isEnvelope(body) && body.code !== 0) return new ApiError(body.msg || fallback, body.code, status)
  return new ApiError(fallback, status === undefined ? 'network_error' : 'invalid_response', status)
}
