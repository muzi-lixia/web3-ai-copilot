import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Dropdown, Space, Typography } from 'antd'
import { useEffect, useRef } from 'react'
import { NavLink, Outlet, useLocation, Link } from 'react-router-dom'

import { getChains } from '../api/resources'
import { http } from '../api/client'
import { agentHttp } from '../api/chat'
import { useWalletLogin } from '../hooks/useWalletLogin'
import { useSessionProbe } from '../hooks/useSessionProbe'
import { useAuthStore } from '../stores/auth'
import { shorten } from '../utils/format'
import { readChatCache } from '../utils/chatCache'
import ModelSelector from './ModelSelector'

const groups: [string, [string, string, string][]][] = [
    ['Agent', [
        ['/copilot', '◉', '对话'],
        ['/login', '◎', '连接钱包'],
    ]],
    ['基础服务 · 独立可复用', [
        ['/portfolio', '▣', '代币余额'],
        ['/market', '◈', '行情'],
        ['/services', '◇', '服务与接口'],
    ]],
    ['知识库', [
        ['/knowledge', '▤', 'RAG 知识库'],
    ]],
]

/** 健康检查探针的固定顺序，与下方渲染的 index 一一对应。 */
const SERVICES = ['基础服务', 'Agent 服务']

export default function AppLayout() {
    const { pathname } = useLocation()
    const address = useAuthStore(s => s.address)
    const token = useAuthStore(s => s.token)
    const { logout, address: connectedAddress, isConnected } = useWalletLogin()
    useSessionProbe(token)

    useEffect(() => {
        const owner = address ?? readChatCache()?.owner
        if (isConnected && connectedAddress && owner && connectedAddress.toLowerCase() !== owner.toLowerCase()) {
            // 已连接钱包更换后，必须重新签名；旧 JWT 不能代表新钱包。
            useAuthStore.getState().clearAuth('switch')
        }
    }, [isConnected, connectedAddress, address])

    const cache = useQueryClient()
    const previous = useRef(address)
    useEffect(() => {
        if (previous.current !== address) {
            // 删除旧身份缓存；公共行情与网络目录可以保留。
            const keep = ['chains', 'market-quotes', 'service-health']
            cache.removeQueries({ predicate: q => !keep.includes(String(q.queryKey[0])) })
            previous.current = address
        }
    }, [address, cache])

    const chains = useQuery({ queryKey: ['chains'], queryFn: getChains })
    const health = useQuery({
        queryKey: ['service-health'],
        queryFn: async () => {
            const results = await Promise.allSettled([
                http.get(import.meta.env.VITE_FOUNDATION_HEALTH_URL ?? '/foundation-health', { baseURL: '' }),
                agentHttp.get(import.meta.env.VITE_AGENT_HEALTH_URL ?? '/agent-health', { baseURL: '' }),
            ])
            return results.map(r => r.status === 'fulfilled')
        },
        refetchInterval: 30000,
    })

    const title = groups.flatMap(g => g[1]).find(n => n[0] === pathname)?.[2] ?? '会话与设置'
    const networks = chains.data ?? []

    return (
        <div className="app-shell">
            <aside className="rail">
                <div className="brand">
                    <div className="logo">W3</div>
                    <div>
                        <strong>Web3 助手</strong>
                        <small>资产查询 · 只读 Agent</small>
                    </div>
                </div>

                <nav>
                    {groups.map(([label, entries]) => (
                        <div className="nav-group" key={label}>
                            <div className="nav-label">{label}</div>
                            {entries.map(([path, icon, text]) => (
                                <NavLink
                                    className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
                                    to={path}
                                    key={path}
                                >
                                    <span>{icon}</span>
                                    {text}
                                    {!token && ['/copilot', '/portfolio'].includes(path) && (
                                        <span className="lock">需登录</span>
                                    )}
                                </NavLink>
                            ))}
                        </div>
                    ))}
                </nav>

                <div className="rail-foot">
                    {SERVICES.map((name, i) => (
                        <div className="connection" key={name}>
                            <i className={health.data?.[i] ? 'dot ok' : 'dot warn'} />
                            {name} · {health.isFetching && !health.data ? '检查中' : health.data?.[i] ? '在线' : '未连接'}
                        </div>
                    ))}
                    <NavLink className="nav-item" to="/settings">⚙ 会话与设置</NavLink>
                </div>
            </aside>

            <div className="main-shell">
                <header className="topbar">
                    <strong>{title}</strong>
                    <Space>
                        <Dropdown
                            trigger={['click']}
                            disabled={!chains.data}
                            menu={{
                                items: networks.map((c) => ({
                                    key: String(c.chain_id),
                                    label: (
                                        <span className="net-item">
                                            <span>{c.name}</span>
                                            <span className="mono muted">{c.chain_id}</span>
                                        </span>
                                    ),
                                })),
                            }}
                        >
                            <button type="button" className="net-select" disabled={!chains.data}>
                                <span>{chains.data ? `${chains.data.length} 个网络` : '网络目录加载中'}</span>
                                <svg className="net-caret" width="10" height="10" viewBox="0 0 1024 1024" aria-hidden="true">
                                    <path
                                        d="M884 256h-75c-5.1 0-9.9 2.5-12.9 6.6L512 654.2 227.9 262.6c-3-4.1-7.8-6.6-12.9-6.6h-75c-6.5 0-10.3 7.4-6.5 12.7l352.6 486.1c12.8 17.6 39 17.6 51.7 0l352.6-486.1c3.9-5.3.1-12.7-6.4-12.7z"
                                        fill="currentColor"
                                    />
                                </svg>
                            </button>
                        </Dropdown>

                        <ModelSelector />

                        {address ? (
                            <>
                                <span className="chip readonly">
                                    <span className="muted">钱包 · 只读</span>
                                    <Typography.Text copyable={{ text: address }}>{shorten(address)}</Typography.Text>
                                </span>
                                <Button size="small" onClick={logout}>退出</Button>
                            </>
                        ) : (
                            <Link to="/login"><Button size="small" type="primary">连接钱包</Button></Link>
                        )}
                    </Space>
                </header>
                <main className={`page-content ${pathname === '/copilot' ? 'chat-page' : ''}`}>
                    <Outlet />
                </main>
            </div>
        </div>
    )
}
