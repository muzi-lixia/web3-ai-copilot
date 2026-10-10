/** Axios 公共请求：附带 JWT、校验 code/msg/data、统一异常并处理失效凭据。 */
import axios, { AxiosError, type AxiosInstance } from 'axios'

import { useAuthStore } from '../stores/auth.ts'
import { ApiError, responseError, unwrapResponse } from './response.ts'
import { refreshSession } from './session.ts'
import type { ApiErrorBody } from '../types/api'

export { ApiError } from './response.ts'

export const http = axios.create({
  baseURL: import.meta.env?.VITE_API_BASE_URL ?? '/api/v1',
  timeout: 20_000,
})

export function configureHttp(client: AxiosInstance) {
client.interceptors.request.use((config) => {
  const { token } = useAuthStore.getState()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

/** 迟到的旧 token 请求失败不能把用户刚建立的新登录状态清掉。 */
function clearExpiredAuth(error: ApiError, authorization: unknown) {
  const token = useAuthStore.getState().token
  if ((error.status === 401 || error.code === 40101) && authorization === `Bearer ${token}`) {
    useAuthStore.getState().clearAuth()
  }
}

client.interceptors.response.use(
  (response) => {
    // 204 按协议没有正文；其他普通 JSON 成功响应必须是 code/msg/data。
    if (response.status === 204) return response
    try {
      unwrapResponse(response.data, response.status)
    } catch (error) {
      if (error instanceof ApiError) clearExpiredAuth(error, response.config.headers.Authorization)
      throw error
    }
    // 保留 AxiosResponse，api 模块显式读取 data.data，类型与运行行为一致。
    return response
  },
  async (error: AxiosError<ApiErrorBody>) => {
    if (axios.isCancel(error)) return Promise.reject(error)
    if (error.response?.status === 401 && error.response.data?.code === 40103 && error.config) {
      const config = error.config as typeof error.config & { refreshed?: boolean }
      if (!config.refreshed) {
        config.refreshed = true
        try {
          const token = await refreshSession()
          config.headers.Authorization = `Bearer ${token}`
          return await client.request(config)
        } catch (refreshError) {
          // 续期网络故障应向页面提示可重试，不能继续处理原始 401 并清掉登录态。
          return Promise.reject(refreshError)
        }
      }
    }
    const failure = responseError(error.response?.data, error.response?.status, error.message)
    clearExpiredAuth(failure, error.config?.headers.Authorization)
    return Promise.reject(failure)
  },
)

}
configureHttp(http)
