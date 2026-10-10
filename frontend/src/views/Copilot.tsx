/** 单会话工作区：每个钱包只有一段固定对话，安全历史和运行状态由服务器维护。 */
import { Alert, Button, Empty, Input, Space, Spin, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { getModel } from '../api/resources'
import { Note } from '../components/PageParts'
import { shorten } from '../utils/format'
import { useEffect, useRef, useState } from 'react'

import { cancelTurn, clearConversation, followTurn, getTurn, loadMessages, openSession, retryTurn, submitTurn } from '../api/chat'
import type { ChatMessage, TurnSnapshot } from '../types/api'
import BalanceResult from '../components/BalanceResult'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { saveChatCache } from '../utils/chatCache'
import { mergeMessages, turnProgressText } from '../utils/chat'

const FAILED = ['failed', 'cancelled']
const STATUS: Record<string, string> = {
  failed: '生成失败', cancelled: '已停止',
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
  const [reconnecting, setReconnecting] = useState(false)
  const [connectionFailed, setConnectionFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [phase, setPhase] = useState<string>('queued')
  const [pendingTurn, setPendingTurn] = useState<string | null>(null)
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
    if (snapshot.version < previous && ['queued', 'running'].includes(snapshot.status)) return
    versions.current.set(snapshot.turn_id, snapshot.version)
    setMessages((current) => mergeMessages(current, [snapshot.message]))
    setActive(['queued', 'running'].includes(snapshot.status) ? snapshot.turn_id : null)
    setPhase(snapshot.context_info?.execution_phase ?? 'queued')
    setPendingTurn(snapshot.persistence_pending ? snapshot.turn_id : null)
    if (!['queued', 'running'].includes(snapshot.status) && !snapshot.persistence_pending) {
      setConnectionFailed(false)
      setReconnecting(false)
      setError(snapshot.error)
    } else if (snapshot.persistence_pending) {
      setError(null)
    }
  }

  /** 显式错误停止自动重连，并查询轮次状态；仍在生成时显示断线状态及手动恢复入口。 */
  function connect(turnId: string) {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setActive(turnId); setPhase('queued'); setConnectionFailed(false); setReconnecting(false)
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
    setLoading(true); setError(null); setReconnecting(false); setPendingTurn(null)
    try {
      const session = await openSession(signal)
      if (signal.aborted) return
      sessionId.current = session.id
      const page = await loadMessages(undefined, signal)
      if (signal.aborted) return
      setMessages(page.items); setCursor(page.next_cursor)
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

  useEffect(() => {
    if (address && !loading) saveChatCache(address, messages)
  }, [address, messages, loading])

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
    if (sendingLock.current || active || pendingTurn || loading || connectionFailed || !input.trim()) return
    sendingLock.current = true; setSending(true); setError(null)
    // trim 仅判断空消息；发送、保存和展示都保留用户原文（包括空格与换行）。
    const text = input
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
    if (sendingLock.current || active || pendingTurn || loading || connectionFailed) return
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
  const blocked = Boolean(active) || Boolean(pendingTurn) || sending || loading || connectionFailed
  const refs = messages.flatMap(m => m.result_refs ?? [])
  return <div className="chat-layout"><section className="chat-main">

    <div role="log" aria-label="对话记录" aria-live="polite"
      className="chat-log">
      {cursor != null && <Button loading={loadingEarlier} disabled={loading} onClick={() => void earlier()}>
        更早的消息
      </Button>}
      {loading ? <Spin /> : !messages.length && <Empty description="开始提问，例如：查询我的 BERA 余额" />}
      {messages.map((item) => <div className={`msg ${item.role === 'user' ? 'me' : ''}`} key={item.id}><div className={`av ${item.role === 'user' ? 'me' : 'ai'}`}>{item.role === 'user' ? '我' : 'AI'}</div><div className="message-body">
        {!!item.result_refs?.length && item.result_refs.map(ref => <div className="tool" key={ref.result_id}><div className="tool-h"><i className={`dot ${ref.isComplete ? 'ok' : 'warn'}`} /><span className="nm">资产查询结果</span><span className="meta">{ref.isComplete ? '已完成' : '部分结果'}</span></div><div className="tool-b"><div className="kv"><span className="k">查询范围</span><span className="v">{ref.scope === 'specified_token' ? '指定代币' : '已登记代币'} · {ref.chain_ids?.length ?? '—'} 个网络</span></div><div className="kv"><span className="k">回灌模型</span><span className="v">仅元数据 · {ref.symbols?.join(' / ') ?? '结果引用'} · 未计价 {ref.unpriced_count ?? '—'} 项</span></div><div className="kv"><span className="k">真实数据</span><span className="v">通过展示通道读取，不进入模型与 Trace</span></div></div></div>)}
        {item.content && <div className="bub"><div style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{item.content}</div>{['queued', 'running'].includes(item.status) && <span className="stream-cursor" />}</div>}
        {item.result_refs?.map(ref => <BalanceResult compact key={ref.result_id} id={ref.result_id} />)}
        {STATUS[item.status] && item.turn_id !== pendingTurn && <div className="message-status">{STATUS[item.status]}</div>}
      </div></div>)}
      {active && (connectionFailed ? <Typography.Text type="warning">连接已断开，点击恢复对话确认生成状态。</Typography.Text> :
        <Space><Spin size="small" />{reconnecting ? '连接中断，正在恢复…' : turnProgressText(phase)}</Space>)}
      <div ref={end} />
    </div>
    {error && <Alert type="warning" showIcon title={error} style={{ marginBlock: 12 }} />}
    {pendingTurn && <Alert type="info" showIcon title={connectionFailed
      ? '回答状态尚未保存，连接已断开，请恢复对话确认结果。'
      : '回答状态尚未保存，正在补写。保存完成后会自动更新。'}
      style={{ marginBlock: 12 }} />}
    {connectionFailed && <Button loading={loading} disabled={sending || loadingEarlier}
      onClick={() => { if (lifetime.current) void restore(lifetime.current.signal) }}>恢复对话</Button>}
    {!active && !pendingTurn && last && FAILED.includes(last.status) && <Button disabled={blocked}
      onClick={() => void retry(last.turn_id)}>重试上一轮</Button>}
    <div className="composer"><div className="composer-box"><Input.TextArea value={input} onChange={(event) => setInput(event.target.value)} maxLength={2000}
      disabled={blocked} autoSize={{ minRows: 2, maxRows: 6 }} placeholder="问点什么…例如「我的 BERA 余额」「ETH 现在多少钱」"
      onKeyDown={(event) => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
          event.preventDefault(); void send()
        }
      }} />
    <Space className="composer-bar" style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 6 }}><span className="pill">▤ 知识库检索（规划中）</span><span className="pill">⌘ 工具：资产 / 行情</span>
      <Button disabled={blocked || !messages.length} onClick={async () => {
        try {
          await clearConversation()
          if (lifetime.current) await restore(lifetime.current.signal)
        } catch (err) { setError(err instanceof Error ? err.message : '清空失败') }
      }}>清空对话</Button>
      {active && <Button disabled={loading} onClick={async () => {
        try { const snapshot = await cancelTurn(active); if (alive()) applySnapshot(snapshot) }
        catch (err) { if (alive()) setError(err instanceof Error ? err.message : '停止失败') }
      }}>停止生成</Button>}
      <Button type="primary" loading={sending} disabled={blocked || !input.trim()} onClick={() => void send()}>发送</Button>
    </Space>
    </div><div className="composer-foot">Enter 发送 · Shift+Enter 换行 · 余额以数据卡片为准</div></div></section><aside className="chat-side">
      <div className="side-block"><div className="side-t">本次会话</div>{[
        ['绑定地址', address ? shorten(address) : '—'], ['来源', '登录凭证 · 只读'], ['模型', model?.model ?? '读取中'],
        ['结果引用', `${refs.length} 份（当前已加载历史）`], ['上下文窗口', model?.context_window ? `${model.context_window} tokens` : '—'],
        ['上下文用量', '暂未提供统计'], ['执行状态', active ? turnProgressText(phase) : pendingTurn ? '正在补写' : '等待提问'],
      ].map(([key, value]) => <div className="side-row" key={key}><span className="k">{key}</span><span className="v">{value}</span></div>)}</div>
      <div className="side-block"><div className="side-t">工具结果轨迹 · 仅元数据</div>{refs.length ? refs.slice(-8).map((ref, i) => <div className="trace" key={`${ref.result_id}:${i}`}><i className={`dot ${ref.isComplete ? 'ok' : 'warn'}`} /><div><div className="tn">资产结果 · {ref.isComplete ? '完整' : '部分'}</div><div className="tt">{ref.chain_ids?.length ?? '—'} 网络 · {ref.symbols?.length ?? '—'} 代币 · {ref.queriedAt ? new Date(ref.queriedAt).toLocaleTimeString() : '时间未提供'}</div></div></div>) : <p className="empty-trace">尚无资产结果。公开行情调用没有资产结果引用，不计入此列表。</p>}</div>
      <div className="side-block"><div className="side-t">能力入口（扩展位）</div><Note title="▤">知识库检索后续通过 search_knowledge 工具接入，复用当前调度和权限边界。</Note></div>
    </aside></div>
}

/** 不同用户仍各自隔离；单会话不等于所有用户共享历史。 */
export default function Copilot() {
  const address = useAuthStore((state) => state.address)
  return address ? <Workspace key={address.toLowerCase()} /> : null
}
