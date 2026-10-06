import axios, { AxiosError } from 'axios'

import type { ApiErrorBody } from '../types/api'

/**
 * 统一请求实例。
 *
 * - baseURL 默认 `/api/v1`，开发期由 vite proxy 转发到 127.0.0.1:8000（见 vite.config.ts）
 * - 响应拦截把后端的 `{ error: { code, message } }` 结构拍平成 ApiError，
 *   业务代码只需 catch 一次，不用每个调用点判断 error.response?.data。
 */
export const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
  timeout: 20_000,
})

export class ApiError extends Error {
  readonly code: string
  readonly status?: number

  constructor(message: string, code = 'unknown', status?: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiErrorBody>) => {
    const body = error.response?.data
    const message = body?.error?.message ?? error.message ?? '网络请求失败'
    const code = body?.error?.code ?? 'network_error'
    return Promise.reject(new ApiError(message, code, error.response?.status))
  },
)
