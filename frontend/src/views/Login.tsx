import { Alert, Button } from 'antd'
import { useEffect } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useWalletLogin } from '../hooks/useWalletLogin'
import { useAuthStore } from '../stores/auth'
import { Note } from '../components/PageParts'
import { shorten } from '../utils/format'
export default function Login() {
  const wallet = useWalletLogin()
  const auth = useAuthStore()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from
  useEffect(() => { if (auth.token && from) navigate(from, { replace: true }) }, [auth.token, from, navigate])
  const stages = ['nonce', 'signing', 'verifying']
  const step = stages.indexOf(wallet.stage)
  return <div className="login-wrap"><div className="login-card">
    {auth.token && <Note title="已连接">当前会话地址 {auth.address && shorten(auth.address)}。切换钱包前请退出当前会话。</Note>}
    {from && <div className="login-continue">连接后继续前往 <b>{from === '/copilot' ? '对话' : from === '/portfolio' ? '代币余额' : '本人数据页面'}</b></div>}
    <div className="login-head"><div className="logo">W3</div><h2>连接钱包</h2><p>用钱包签名证明地址归属，无需密码</p></div>
    <div className="steps">{['请求 nonce', '钱包签名', '校验并签发凭证'].map((label, i) => <div className={`step ${auth.token || i < step ? 'done' : i === step ? 'active' : ''}`} key={label}>{i + 1} · {label}<span>{auth.token || i < step ? '已完成' : i === step ? wallet.stageText : '等待执行'}</span></div>)}</div>
    <div className="siwe"><div className="siwe-h">待签名消息（SIWE / EIP-4361）<span className="proto-tag core">服务端生成</span></div><pre>{wallet.signingMessage ?? `连接钱包后，服务端将生成签名原文。
消息包含登录域名、地址、nonce 和有效期。
请核对钱包中的消息，不签署来源不明的请求。`}</pre></div>
    {wallet.isConnected && wallet.address && <p className="login-address mono">已连接 {wallet.address}</p>}
    {auth.token ? <Button block danger onClick={wallet.logout}>退出当前会话</Button> : <Button type="primary" block loading={wallet.busy || wallet.isConnecting} disabled={wallet.busy} onClick={() => void wallet.login()}>{wallet.busy ? wallet.stageText : wallet.isConnected ? '在钱包中签名' : '连接钱包'}</Button>}
    <div className="login-foot">签名仅验证身份，不发起链上交易，也不消耗 Gas</div>
    {wallet.error && <Alert type="error" showIcon title={wallet.error} description="本次登录未完成。确认钱包与网络后，可重新发起签名。" style={{ marginTop: 14 }} />}
    <div style={{ marginTop: 14 }}><Note title="为什么安全">凭证只在服务器验证签名后签发，钱包地址由签名确认；前端不能声明另一个钱包的归属。</Note></div>
    <div style={{ marginTop: 12 }}><Note title="切换钱包" warning>切换钱包意味着切换身份，会清除旧登录与个人数据缓存。访问凭证自动续期，登录会话失效后需要重新签名。</Note></div>
  </div></div>
}
