import { useQuery } from '@tanstack/react-query'

import { fetchMe } from '../api/auth'

/**
 * 启动期校验本地凭证是否仍然有效。
 *
 * 为什么需要它：token 存在 sessionStorage 里，服务端可能已经不认它了
 * —— 密钥轮换、过期、或者后端重启换了签名密钥。此时本地看上去"已登录"，
 * 页面完整显示，直到用户点了某个接口才吃到 401。
 *
 * 这里在进入受保护路由时静默验一次，让失效**立刻**反映到界面上：
 * 探针拿到 401 → 请求层清掉本地凭证 → 路由守卫改判为未登录 → 回登录页。
 *
 * 用 TanStack Query 而不是裸 useEffect：它自带请求去重与缓存，
 * 多个受保护页面来回切换不会反复打这个接口。
 * 失败不回传给调用方也不弹错误——探针的职责只是"触发一次真实请求"，
 * 后续动作由请求层的统一处理完成。
 */
export function useSessionProbe(token: string | null) {
  return useQuery({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
    enabled: Boolean(token),
    // 凭证无效时重试只是把同一个 401 打三遍，没有意义。
    retry: false,
    staleTime: 5 * 60_000,
  })
}
