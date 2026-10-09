import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { WagmiProvider } from 'wagmi'

// 必须排在 App 之前：这个模块在导入时就调用 createAppKit() 完成初始化，
// 晚于任何 useAppKit() 的渲染时机都会拿到未初始化的实例。
import { wagmiConfig } from './config/wallet'
import App from './App.tsx'
import './index.css'

/**
 * 服务端数据的统一缓存中枢。
 * 看板查询由 TanStack Query 缓存；登录态用 zustand，对话页面维护订阅和临时展示状态。
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {/* WagmiProvider 必须在 QueryClientProvider 外层：wagmi 内部复用同一个 QueryClient */}
    <WagmiProvider config={wagmiConfig}>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </WagmiProvider>
  </StrictMode>,
)
