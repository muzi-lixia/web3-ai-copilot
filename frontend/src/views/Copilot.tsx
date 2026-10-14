/** 单会话工作区：每个钱包只有一段固定对话，安全历史和运行状态由服务器维护。 */
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Space, Spin } from 'antd'
import { useEffect, useRef, useState } from 'react'

import { cancelTurn, clearConversation, followTurn, getTurn, loadMessages, openSession, retryTurn, submitTurn } from '../api/chat'
import { getModel } from '../api/resources'
import type { ChatMessage, TurnSnapshot } from '../types/api'
import { formatClock, shorten } from '../utils/format'
import Markdown from '../components/Markdown'
import TurnResult from '../components/TurnResult'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { saveChatCache } from '../utils/chatCache'
import { errorText, mergeMessages } from '../utils/chat'

const FAILED = ['failed', 'cancelled']
const RUNNING = ['queued', 'running']
const STATUS: Record<string, string> = {
    failed: '生成失败',
    cancelled: '已停止',
}

function Workspace() {
    const address = useAuthStore(s => s.address)
    const { data: model } = useQuery({ queryKey: ['agent-model', address], queryFn: getModel })

    const [messages, setMessages] = useState<ChatMessage[]>([])
    const [cursor, setCursor] = useState<number | null>(null)
    const [input, setInput] = useState('')
    const [active, setActive] = useState<string | null>(null)
    const [loading, setLoading] = useState(true)
    const [loadingEarlier, setLoadingEarlier] = useState(false)
    const [sending, setSending] = useState(false)
    const [, setReconnecting] = useState(false)
    const [connectionFailed, setConnectionFailed] = useState(false)
    const [error, setError] = useState<string | null>(null)
    const [pendingTurn, setPendingTurn] = useState<string | null>(null)
    /** 最近一次终态失败：凭证过期要自动重放，不能让用户自己点。 */
    const [failed, setFailed] = useState<{ turnId: string; error: string | null } | null>(null)

    const autoRetried = useRef(false)
    const sessionId = useRef<string | null>(null)
    const lifetime = useRef<AbortController | null>(null)
    const request = useRef<AbortController | null>(null)
    const pending = useRef<{ text: string; clientId: string } | null>(null)
    const retryAttempt = useRef<{ turnId: string; clientId: string } | null>(null)
    const sendingLock = useRef(false)
    const earlierLock = useRef(false)
    const versions = useRef(new Map<string, number>())
    const end = useRef<HTMLDivElement>(null)

    const alive = () => Boolean(lifetime.current && !lifetime.current.signal.aborted)

    /** 快照替换同一消息，慢连接跳过中间版本也不会重复拼接文本。 */
    function applySnapshot(snapshot: TurnSnapshot) {
        if (!alive() || snapshot.session_id !== sessionId.current) return
        const previous = versions.current.get(snapshot.turn_id) ?? -1
        // 崩溃后数据库可能只保留较低版本的快照；已持久化终态仍是权威结果。
        if (snapshot.version < previous && RUNNING.includes(snapshot.status)) return
        versions.current.set(snapshot.turn_id, snapshot.version)
        setMessages((current) => mergeMessages(current, [snapshot.message]))
        setActive(RUNNING.includes(snapshot.status) ? snapshot.turn_id : null)
        setPendingTurn(snapshot.persistence_pending ? snapshot.turn_id : null)
        if (!RUNNING.includes(snapshot.status) && !snapshot.persistence_pending) {
            setConnectionFailed(false)
            setReconnecting(false)
            setError(errorText(snapshot.error))
            setFailed({ turnId: snapshot.turn_id, error: snapshot.error })
        } else if (snapshot.persistence_pending) {
            setError(null)
        }
    }

    /** 显式错误停止自动重连，并查询轮次状态；仍在生成时显示断线状态及手动恢复入口。 */
    function connect(turnId: string) {
        request.current?.abort()
        const controller = new AbortController()
        request.current = controller
        setActive(turnId)
        setConnectionFailed(false)
        setReconnecting(false)

        void followTurn(
            turnId,
            controller.signal,
            (snapshot) => {
                if (!controller.signal.aborted) applySnapshot(snapshot)
            },
            (value) => {
                if (!controller.signal.aborted && alive()) setReconnecting(value)
            },
        ).catch(async (err) => {
            if (controller.signal.aborted || !alive()) return
            setReconnecting(false)
            setConnectionFailed(true)
            setError(err instanceof Error ? err.message : '连接失败')
            try {
                const snapshot = await getTurn(turnId, controller.signal)
                if (!controller.signal.aborted) applySnapshot(snapshot)
            } catch (stateError) {
                if (controller.signal.aborted || !alive()) return
                // 资源已删除时解除本地生成锁；数据库暂不可用则保留未知状态，等待用户恢复。
                if (stateError instanceof ApiError && stateError.status === 404) setActive(null)
            }
        })
    }

    /** 首次打开与手动恢复使用同一条链路，每个失败分支都结束 loading。 */
    async function restore(signal: AbortSignal) {
        request.current?.abort()
        versions.current.clear()
        setLoading(true)
        setError(null)
        setReconnecting(false)
        setPendingTurn(null)
        setFailed(null)
        autoRetried.current = false
        try {
            const session = await openSession(signal)
            if (signal.aborted) return
            sessionId.current = session.id
            const page = await loadMessages(undefined, signal)
            if (signal.aborted) return
            setMessages(page.items)
            setCursor(page.next_cursor)
            setActive(page.active_turn_id)
            setConnectionFailed(false)
            if (page.active_turn_id) connect(page.active_turn_id)
        } catch (err) {
            if (!signal.aborted) {
                setConnectionFailed(true)
                setError(err instanceof Error ? err.message : '历史加载失败')
            }
        } finally {
            if (!signal.aborted) setLoading(false)
        }
    }

    useEffect(() => {
        const controller = new AbortController()
        lifetime.current = controller
        void restore(controller.signal)
        return () => {
            controller.abort()
            request.current?.abort()
        }
        // 钱包变化由父组件 key 重新挂载，清理旧身份的请求和状态。
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [])

    useEffect(() => {
        if (address && !loading) saveChatCache(address, messages)
    }, [address, messages, loading])

    useEffect(() => {
        end.current?.scrollIntoView({ block: 'nearest' })
    }, [messages])

    /** 接受成功后即使历史请求失败，也必须订阅生成；不能清空问题后把活动轮次丢掉。 */
    async function followAccepted(turnId: string) {
        setActive(turnId)
        try {
            const page = await loadMessages()
            if (alive()) {
                setMessages((current) => mergeMessages(page.items, current))
                setCursor(page.next_cursor)
            }
        } finally {
            if (alive()) connect(turnId)
        }
    }

    async function send() {
        if (sendingLock.current || active || pendingTurn || loading || connectionFailed || !input.trim()) return
        sendingLock.current = true
        setSending(true)
        setError(null)
        autoRetried.current = false
        // trim 仅判断空消息；发送、保存和展示都保留用户原文（包括空格与换行）。
        const text = input
        if (pending.current?.text !== text) pending.current = { text, clientId: crypto.randomUUID() }
        try {
            const result = await submitTurn(text, pending.current.clientId)
            if (!alive()) return
            pending.current = null
            setInput('')
            await followAccepted(result.turn_id)
        } catch (err) {
            if (alive()) setError(err instanceof Error ? err.message : '发送失败，可再次发送重试')
        } finally {
            sendingLock.current = false
            if (alive()) setSending(false)
        }
    }

    async function retry(turnId: string) {
        if (sendingLock.current || active || pendingTurn || loading || connectionFailed) return
        sendingLock.current = true
        setSending(true)
        setError(null)
        if (retryAttempt.current?.turnId !== turnId) {
            retryAttempt.current = { turnId, clientId: crypto.randomUUID() }
        }
        try {
            const result = await retryTurn(turnId, retryAttempt.current.clientId)
            if (!alive()) return
            retryAttempt.current = null
            await followAccepted(result.turn_id)
        } catch (err) {
            if (alive()) setError(err instanceof Error ? err.message : '重试失败')
        } finally {
            sendingLock.current = false
            if (alive()) setSending(false)
        }
    }

    /**
     * 凭证过期是「重放一定成功」的失败：自动重放一次，用户不必自己点「重试上一轮」。
     * 每个提问只自动重放一次 —— 重放后再失败就交给用户，避免同一轮被无限重放。
     */
    useEffect(() => {
        if (!failed || failed.error !== 'access_expired' || autoRetried.current) return
        autoRetried.current = true
        setError(null)
        void retry(failed.turnId)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [failed])

    async function earlier() {
        if (earlierLock.current || loading || cursor == null) return
        earlierLock.current = true
        setLoadingEarlier(true)
        try {
            const page = await loadMessages(cursor)
            if (!alive()) return
            setMessages((current) => mergeMessages(page.items, current))
            setCursor(page.next_cursor)
        } catch (err) {
            if (alive()) setError(err instanceof Error ? err.message : '加载失败')
        } finally {
            earlierLock.current = false
            if (alive()) setLoadingEarlier(false)
        }
    }

    async function clearAll() {
        try {
            await clearConversation()
            if (lifetime.current) await restore(lifetime.current.signal)
        } catch (err) {
            setError(err instanceof Error ? err.message : '清空失败')
        }
    }

    async function stop() {
        if (!active) return
        try {
            const snapshot = await cancelTurn(active)
            if (alive()) applySnapshot(snapshot)
        } catch (err) {
            if (alive()) setError(err instanceof Error ? err.message : '停止失败')
        }
    }

    const last = messages.filter((item) => item.role === 'assistant').at(-1)
    const blocked = Boolean(active) || Boolean(pendingTurn) || sending || loading || connectionFailed
    const sessionFacts = [
        ['绑定地址', address ? shorten(address) : '—'],
        ['来源', '登录凭证 · 只读'],
        ['模型', model?.model ?? '读取中'],
        ['上下文窗口', model?.context_window ? `${model.context_window} tokens` : '—'],
        ['上下文用量', '暂未提供统计'],
    ]

    return (
        <div className="chat-layout">
            <section className="chat-main">
                <div role="log" aria-label="对话记录" aria-live="polite" className="chat-log">
                    {cursor != null && (
                        <Button loading={loadingEarlier} disabled={loading} onClick={() => void earlier()}>
                            更早的消息
                        </Button>
                    )}
                    {loading ? <Spin /> : !messages.length && <Empty description="开始提问，例如：查询我的 BERA 余额" />}

                    {messages.map((item) => {
                        const itemResults = item.results ?? []
                        // 用户气泡右下角的时间来自提问时刻，与回答共用同一个值。
                        const sentAt = item.role === 'user' ? formatClock(item.created_at) : ''

                        if (item.role === 'user') {
                            return (
                                <div className="msg me" key={item.id}>
                                    <div className="av me">我</div>
                                    <div className="me-col">
                                        <div className="bub">
                                            <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                                                {item.content}
                                            </div>
                                        </div>
                                        {sentAt && <div className="me-t">{sentAt}</div>}
                                    </div>
                                </div>
                            )
                        }

                        const speaking = RUNNING.includes(item.status)
                        // 「结果」块顶部那句模型话。三点跟着生成状态走：还没吐字时它就是唯一内容，
                        // 出了正文后接在正文下方继续表示还在生成 —— 模型调工具期间正文会停住不动。
                        const say = (item.content || speaking) ? (
                            <div className="res-say">
                                {item.content && <Markdown text={item.content} />}
                                {speaking && <span className="stream-cursor"><i /><i /><i /></span>}
                            </div>
                        ) : null

                        // 回答区固定两段：结果常显，数据来源与明细折叠；纯文字回答只有结果块。
                        // 流式期间只给模型正文，数据来源与工具调用等输出结束再出现。
                        return (
                            <div className="msg" key={item.id}>
                                <div className="av ai">AI</div>
                                <div className="ans">
                                    {/* 流式期间只给模型正文：数据与工具条目等输出结束再出现，免得正文还在写、
                                        下面已经跳起来了。没有结果时同样只有正文块。 */}
                                    {!speaking && itemResults.length ? (
                                        <TurnResult say={say} results={itemResults} toolMeta={item.tool_meta} />
                                    ) : (
                                        <div className="blk"><div className="blk-b">{say}</div></div>
                                    )}
                                    {STATUS[item.status] && item.turn_id !== pendingTurn && (
                                        <div className="message-status">{STATUS[item.status]}</div>
                                    )}
                                </div>
                            </div>
                        )
                    })}

                    {connectionFailed && (
                        <div className="stream-bad">
                            <span className="dot" style={{ background: '#FF8A9B', flex: 'none' }} />
                            <span>{error ?? '连接已断开'}，已生成的内容不会丢失。</span>
                            <Button
                                size="small"
                                loading={loading}
                                disabled={sending || loadingEarlier}
                                onClick={() => { if (lifetime.current) void restore(lifetime.current.signal) }}
                            >
                                恢复对话
                            </Button>
                        </div>
                    )}
                    <div ref={end} />
                </div>

                {error && !connectionFailed && (
                    <Alert type="warning" showIcon title={error} style={{ marginBlock: 12 }} />
                )}
                {pendingTurn && (
                    <Alert
                        type="info"
                        showIcon
                        title={connectionFailed
                            ? '回答状态尚未保存，连接已断开，请恢复对话确认结果。'
                            : '回答状态尚未保存，正在补写。保存完成后会自动更新。'}
                        style={{ marginBlock: 12 }}
                    />
                )}
                {!active && !pendingTurn && last && FAILED.includes(last.status) && (
                    <Button disabled={blocked} onClick={() => void retry(last.turn_id)}>重试上一轮</Button>
                )}

                <div className="composer">
                    <div className="composer-box">
                        <Input.TextArea
                            value={input}
                            onChange={(event) => setInput(event.target.value)}
                            maxLength={2000}
                            disabled={blocked}
                            autoSize={{ minRows: 2, maxRows: 6 }}
                            placeholder="问点什么…例如「我的 BERA 余额」「ETH 现在多少钱」"
                            onKeyDown={(event) => {
                                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                                    event.preventDefault()
                                    void send()
                                }
                            }}
                        />
                        <Space
                            className="composer-bar"
                            style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 6 }}
                        >
                            <span className="pill">▤ 知识库检索</span>
                            <span className="pill">⌘ 工具：资产 / 行情</span>
                            <Button disabled={blocked || !messages.length} onClick={() => void clearAll()}>
                                清空对话
                            </Button>
                            {active && (
                                <Button disabled={loading} onClick={() => void stop()}>停止生成</Button>
                            )}
                            <Button
                                type="primary"
                                loading={sending}
                                disabled={blocked || !input.trim()}
                                onClick={() => void send()}
                            >
                                发送
                            </Button>
                        </Space>
                    </div>
                </div>
            </section>

            <aside className="chat-side">
                <div className="side-block">
                    <div className="side-t">本次会话</div>
                    {sessionFacts.map(([key, value]) => (
                        <div className="side-row" key={key}>
                            <span className="k">{key}</span>
                            <span className="v">{value}</span>
                        </div>
                    ))}
                </div>
            </aside>
        </div>
    )
}

/** 不同用户仍各自隔离；单会话不等于所有用户共享历史。 */
export default function Copilot() {
    const address = useAuthStore((state) => state.address)
    return address ? <Workspace key={address.toLowerCase()} /> : null
}
