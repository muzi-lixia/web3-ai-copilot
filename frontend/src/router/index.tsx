import type { ReactNode } from 'react'
import { createBrowserRouter, Navigate, useLocation } from 'react-router-dom'

import AppLayout from '../components/AppLayout'
import { useSessionProbe } from '../hooks/useSessionProbe'
import { useAuthStore } from '../stores/auth'
import Copilot from '../views/Copilot'
import Dashboard from '../views/Dashboard'
import Knowledge from '../views/Knowledge'
import Login from '../views/Login'
import Market from '../views/Market'
import Portfolio from '../views/Portfolio'
import Risk from '../views/Risk'
import Staking from '../views/Staking'

/**
 * 登录守卫。
 *
 * 拦住未登录访问，并把"原本想去哪"记在 state.from 里，
 * 登录成功后把人送回那一页，而不是一律丢回首页。
 */
function RequireAuth({ children }: { children: ReactNode }) {
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

export const router = createBrowserRouter([
  { path: '/login', element: <Login /> },
  {
    path: '/',
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', element: <Dashboard /> },
      { path: 'portfolio', element: <Portfolio /> },
      { path: 'market', element: <Market /> },
      { path: 'risk', element: <Risk /> },
      { path: 'staking', element: <Staking /> },
      { path: 'copilot', element: <Copilot /> },
      { path: 'knowledge', element: <Knowledge /> },
    ],
  },
])
