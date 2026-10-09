/** 单会话工作区：每个钱包只有一段固定对话，历史、摘要和运行状态由服务器维护。 */
import { Alert, Button, Card, Empty, Input, Space, Spin, Typography } from 'antd'
import { useEffect, useRef, useState } from 'react'

import { cancelTurn, followTurn, getTurn, loadMessages, openSession, retryTurn, submitTurn } from '../api/chat'
import type { ChatMessage, TurnSnapshot } from '../types/api'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { contextIssueMessages, mergeMessages } from '../utils/chat'

const FAILED = ['failed', 'cancelled', 'interrupted']
const STATUS: Record<string, string> = {
  failed: '生成失败', cancelled: '已停止', interrupted: '回复已中断', truncated: '达到回复长度上限',
}

function Workspace() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [cursor, setCursor] = useState<number | null>(null)
  const [input, setInput] = useState('')
  const [active, setActive] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [sending, setSending] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [connectionFailed, setConnectionFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [summaryVersion, setSummaryVersion] = useState<number | null>(null)
  const [issues, setIssues] = useState<string[]>([])
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
    if (snapshot.version < previous && snapshot.status === 'running') return
    versions.current.set(snapshot.turn_id, snapshot.version)
    setMessages((current) => mergeMessages(current, [snapshot.message]))
    setActive(snapshot.status === 'running' ? snapshot.turn_id : null)
    setIssues(snapshot.context_info?.issues ?? [])
    setSummaryVersion(snapshot.summary_version ?? null)
    if (snapshot.status !== 'running') {
      setConnectionFailed(false)
      setReconnecting(false)
      setError(snapshot.error)
    }
  }

  /** 显式错误停止自动重连，并查询轮次状态；仍在生成时显示断线状态及手动恢复入口。 */
  function connect(turnId: string) {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setActive(turnId); setConnectionFailed(false); setReconnecting(false)
    void followTurn(turnId, controller.signal, (snapshot) => {
      if (!controller.signal.aborted) applySnapshot(snapshot)
    }, (value) => {
      if (!controller.signal.aborted && alive()) setReconnecting(value)
    }).catch(async (err) => {
      if (controller.signal.aborted || !alive()) return
      setReconnecting(false); setConnectionFailed(true)
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
    setLoading(true); setError(null); setReconnecting(false); setIssues([])
    try {
      const session = await openSession(signal)
      if (signal.aborted) return
      sessionId.current = session.id
      const page = await loadMessages(undefined, signal)
      if (signal.aborted) return
      setMessages(page.items); setCursor(page.next_cursor); setSummaryVersion(page.summary_version)
      setActive(page.active_turn_id); setConnectionFailed(false)
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
    return () => { controller.abort(); request.current?.abort() }
    // 钱包变化由父组件 key 重新挂载，清理旧身份的请求和状态。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { end.current?.scrollIntoView({ block: 'nearest' }) }, [messages])

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
    if (sendingLock.current || active || loading || connectionFailed || !input.trim()) return
    sendingLock.current = true; setSending(true); setError(null)
    const text = input.trim()
    if (pending.current?.text !== text) pending.current = { text, clientId: crypto.randomUUID() }
    try {
      const result = await submitTurn(text, pending.current.clientId)
      if (!alive()) return
      pending.current = null; setInput('')
      await followAccepted(result.turn_id)
    } catch (err) {
      if (alive()) setError(err instanceof Error ? err.message : '发送失败，可再次发送重试')
    } finally {
      sendingLock.current = false
      if (alive()) setSending(false)
    }
  }

  async function retry(turnId: string) {
    if (sendingLock.current || active || loading || connectionFailed) return
    sendingLock.current = true; setSending(true); setError(null)
    if (retryAttempt.current?.turnId !== turnId) retryAttempt.current = { turnId, clientId: crypto.randomUUID() }
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

  async function earlier() {
    if (earlierLock.current || loading || cursor == null) return
    earlierLock.current = true; setLoadingEarlier(true)
    try {
      const page = await loadMessages(cursor)
      if (!alive()) return
      setMessages((current) => mergeMessages(page.items, current)); setCursor(page.next_cursor)
    } catch (err) {
      if (alive()) setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      earlierLock.current = false
      if (alive()) setLoadingEarlier(false)
    }
  }

  const last = messages.filter((item) => item.role === 'assistant').at(-1)
  const blocked = Boolean(active) || sending || loading || connectionFailed
  return <Card title="Copilot">
    <Typography.Paragraph type="secondary">
      对话自动保存在服务器，可刷新恢复。长对话自动摘要；尚未接入实时钱包查询。
      {summaryVersion != null && ` 历史摘要 v${summaryVersion}。`}
    </Typography.Paragraph>
    <div role="log" aria-label="对话记录" aria-live="polite"
      style={{ minHeight: 240, maxHeight: '50vh', overflowY: 'auto', padding: 4 }}>
      {cursor != null && <Button loading={loadingEarlier} disabled={loading} onClick={() => void earlier()}>
        更早的消息
      </Button>}
      {loading ? <Spin /> : !messages.length && <Empty description="开始提问，例如：什么是 Berachain？" />}
      {messages.map((item) => <div key={item.id} style={{ padding: 14, marginBlock: 12,
        borderRadius: 8, background: item.role === 'user' ? '#e6f4ff' : '#f5f5f5' }}>
        <Typography.Text strong>{item.role === 'user' ? '你' : 'Copilot'}</Typography.Text>
        <div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', marginTop: 6 }}>{item.content}</div>
        {STATUS[item.status] && <Typography.Text type="secondary">{STATUS[item.status]}</Typography.Text>}
      </div>)}
      {active && (connectionFailed ? <Typography.Text type="warning">连接已断开，点击恢复对话确认生成状态。</Typography.Text> :
        <Space><Spin size="small" />{reconnecting ? '连接中断，正在恢复…' : '正在生成回复…'}</Space>)}
      <div ref={end} />
    </div>
    {error && <Alert type="warning" showIcon title={error} style={{ marginBlock: 12 }} />}
    {connectionFailed && <Button loading={loading} disabled={sending || loadingEarlier}
      onClick={() => { if (lifetime.current) void restore(lifetime.current.signal) }}>恢复对话</Button>}
    {issues.length > 0 && <Alert type="info" style={{ marginBlock: 12 }} title={contextIssueMessages(issues).join('；')} />}
    {!active && last && FAILED.includes(last.status) && <Button disabled={blocked}
      onClick={() => void retry(last.turn_id)}>重试上一轮</Button>}
    <Input.TextArea value={input} onChange={(event) => setInput(event.target.value)} maxLength={2000}
      disabled={blocked} autoSize={{ minRows: 3, maxRows: 6 }} placeholder="Enter 发送，Shift+Enter 换行"
      onKeyDown={(event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
          event.preventDefault(); void send()
        }
      }} />
    <Space style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 12 }}>
      {active && <Button disabled={loading} onClick={async () => {
        try { const snapshot = await cancelTurn(active); if (alive()) applySnapshot(snapshot) }
        catch (err) { if (alive()) setError(err instanceof Error ? err.message : '停止失败') }
      }}>停止生成</Button>}
      <Button type="primary" loading={sending} disabled={blocked || !input.trim()} onClick={() => void send()}>发送</Button>
    </Space>
  </Card>
}

/** 不同用户仍各自隔离；单会话不等于所有用户共享历史。 */
export default function Copilot() {
  const address = useAuthStore((state) => state.address)
  return address ? <Workspace key={address.toLowerCase()} /> : null
}
