import type { ReactNode } from 'react'
import { useAccount } from 'wagmi'
import { Button } from 'antd'
import { Link, useLocation } from 'react-router-dom'
import { readChatCache } from '../utils/chatCache'
import { useAuthStore } from '../stores/auth'
/** 仅保护个人数据页面；匿名用户仍可访问行情与服务目录。 */
export default function RequireAuth({ children }: { children: ReactNode }) {
  const { token } = useAuthStore()
  const { address: connectedAddress, isConnected } = useAccount()
  const { pathname } = useLocation()
  if (token) return <>{children}</>
  const chat = pathname === '/copilot'
  const cache = chat ? readChatCache() : null
  if (cache?.messages.length && (!isConnected || !connectedAddress || connectedAddress.toLowerCase() === cache.owner)) return <div className="chat-layout"><section className="chat-main"><div className="anon-bar offline-bar"><span>当前登录已失效。以下是本地离线缓存，可能不完整；服务端历史和资产结果尚未读取。缓存更新：{new Date(cache.updatedAt).toLocaleString()}</span><Link to="/login" state={{ from: pathname }}><Button size="small">重新登录</Button></Link></div><div className="chat-log">{cache.messages.map(m => <div className={`msg ${m.role === 'user' ? 'me' : ''}`} key={m.id}><div className={`av ${m.role === 'user' ? 'me' : 'ai'}`}>{m.role === 'user' ? '我' : 'AI'}</div><div className="message-body"><div className="bub">{m.content}{!!m.result_refs?.length && <p className="muted">资产结果需重新登录后读取，离线缓存不包含真实余额。</p>}</div></div></div>)}</div><div className="composer"><Button disabled block>离线缓存只读，重新登录后继续对话</Button></div></section></div>
  return <div className="auth-gate"><div className="guard-card"><div className="guard-icon">{chat ? '◉' : '▣'}</div><h3>{chat ? '连接钱包后开始对话' : '连接钱包后查看本人数据'}</h3>
    <p>{chat ? '对话里的每一次追问，都需要知道「在问谁的钱包」。' : '查询目标是当前用户，没有登录凭证就没有可查询的钱包身份。'}</p>
    <div className="guard-list"><div><b>读什么</b>已登记代币范围内的链上余额与估值</div><div><b>不读什么</b>私钥、助记词；只读查询，不构造或发送交易</div><div><b>地址从哪来</b>钱包签名验证后绑定到凭证，不接受手工填写</div></div>
    <Link to="/login" state={{ from: pathname }}><Button type="primary" block>连接钱包</Button></Link><p>用签名证明地址归属 · 无需密码</p></div></div>
}
