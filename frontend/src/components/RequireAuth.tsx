import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { useSessionProbe } from '../hooks/useSessionProbe'
import { useAuthStore } from '../stores/auth'

/**
 * 登录守卫。
 *
 * 拦住未登录访问，并把"原本想去哪"记在 state.from 里，
 * 登录成功后把人送回那一页，而不是一律丢回首页。
 */
export default function RequireAuth({ children }: { children: ReactNode }) {
  const token = useAuthStore((state) => state.token)
  const location = useLocation()

  // 进受保护路由时静默验一次本地凭证。服务端已经不认时（过期 / 密钥轮换），
  // 请求层会清掉凭证，下面的判断随即改判为未登录并把人送回登录页
  // —— 而不是让页面完整显示，直到用户点了某个接口才吃到 401。
  useSessionProbe(token)

  if (!token) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <>{children}</>
}
