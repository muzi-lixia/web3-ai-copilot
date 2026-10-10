/** 一个标签页同时只刷新一次；SSE 与 Axios 共用此入口，刷新 token 每次轮换。 */
import axios from 'axios'
import { ApiError, unwrapResponse } from './response.ts'
import { useAuthStore } from '../stores/auth.ts'

let refreshing: Promise<string> | null = null
export function refreshSession() {
  if (refreshing) return refreshing
  const current = useAuthStore.getState()
  const refreshToken = current.refreshToken
  if (!refreshToken) return Promise.reject(new ApiError('请重新登录', 40101, 401))
  refreshing = axios.post((import.meta.env?.VITE_API_BASE_URL ?? '/api/v1') + '/auth/refresh', {
    refresh_token: refreshToken,
  }, { timeout: 20_000 }).then(({ data }) => {
    const credentials = unwrapResponse(data) as { token: string; address: string; refresh_token: string; expires_at: string; session_expires_at: string }
    // 钱包切换后迟到的刷新结果不能恢复旧身份。
    if (useAuthStore.getState().refreshToken !== refreshToken) throw new Error('登录身份已改变')
    useAuthStore.getState().setAuth(credentials.token, credentials.address, credentials.refresh_token, credentials)
    return credentials.token
  }).catch((error: unknown) => {
    const rejected = axios.isAxiosError(error) && [401, 403].includes(error.response?.status ?? 0) || error instanceof ApiError && [401, 403].includes(error.status ?? 0)
    // 只有凭证被明确拒绝才清身份；网络和 5xx 不证明登录会话失效。
    if (rejected) {
      if (useAuthStore.getState().refreshToken === refreshToken) useAuthStore.getState().clearAuth()
      throw new ApiError('登录会话已失效，请重新签名', 40101, 401)
    }
    throw new ApiError('登录续期暂时失败，请检查网络后重试', 'refresh_unavailable', 503)
  }).finally(() => { refreshing = null })
  return refreshing
}
