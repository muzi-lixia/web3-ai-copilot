import { clearChatCache, clearDifferentChatCache } from '../utils/chatCache.ts'
import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

interface AuthState {
  token: string | null
  refreshToken: string | null
  address: string | null
  expiresAt: string | null
  sessionExpiresAt: string | null
  setAuth: (token: string, address: string, refreshToken: string, metadata?: { expires_at?: string; session_expires_at?: string }) => void
  clearAuth: (reason?: 'expired' | 'logout' | 'switch') => void
}

/**
 * 登录态。
 *
 * 用 sessionStorage 而非 localStorage：关掉标签页即失效，共用电脑时不会把
 * 上一个用户的登录态留给下一个人。token 里其实已经含了地址，单独存一份是为了
 * 顶部栏直接读，不必每次解析 JWT。
 */
export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      token: null,
      refreshToken: null,
      address: null,
      expiresAt: null, sessionExpiresAt: null,
      setAuth: (token, address, refreshToken, metadata) => { clearDifferentChatCache(address); set({ token, address, refreshToken, expiresAt: metadata?.expires_at ?? null, sessionExpiresAt: metadata?.session_expires_at ?? null }) },
      clearAuth: (reason = 'expired') => { if (reason !== 'expired') clearChatCache(); set({ token: null, address: null, refreshToken: null, expiresAt: null, sessionExpiresAt: null }) },
    }),
    {
      name: 'web3-copilot-auth',
      storage: createJSONStorage(() => sessionStorage),
    },
  ),
)
