import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

interface AuthState {
  token: string | null
  address: string | null
  setAuth: (token: string, address: string) => void
  clearAuth: () => void
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
      address: null,
      setAuth: (token, address) => set({ token, address }),
      clearAuth: () => set({ token: null, address: null }),
    }),
    {
      name: 'web3-copilot-auth',
      storage: createJSONStorage(() => sessionStorage),
    },
  ),
)
