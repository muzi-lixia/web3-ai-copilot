import { Alert, Button, Card, Steps, Typography } from 'antd'
import { useEffect, useMemo } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { type LoginStage, useWalletLogin } from '../hooks/useWalletLogin'
import { useAuthStore } from '../stores/auth'
import { shorten } from '../utils/format'

/** 三个阶段在 Steps 里的顺序。idle / done 不在流程中间，不占步骤位。 */
const STEP_ORDER: LoginStage[] = ['nonce', 'signing', 'verifying']

const STEP_LABEL: Record<string, string> = {
  nonce: '获取消息',
  signing: '签名确认',
  verifying: '验证身份',
}

export default function Login() {
  const { login, stage, stageText, error, busy, isConnected, address, isConnecting } =
    useWalletLogin()
  const token = useAuthStore((state) => state.token)
  const navigate = useNavigate()
  const location = useLocation()

  /** 被守卫拦下来的原目标地址，登录后送回去。 */
  const from = (location.state as { from?: string } | null)?.from ?? '/dashboard'

  useEffect(() => {
    if (token) {
      navigate(from, { replace: true })
    }
  }, [token, from, navigate])

  const currentStep = useMemo(() => STEP_ORDER.indexOf(stage), [stage])

  const handleLogin = async () => {
    const loggedIn = await login()
    if (loggedIn) {
      navigate(from, { replace: true })
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: '#f5f5f5',
        padding: 24,
      }}
    >
      <Card style={{ width: 440 }} styles={{ body: { padding: 32 } }}>
        <Typography.Title level={4} style={{ marginBottom: 8 }}>
          Web3 AI Copilot
        </Typography.Title>
        <Typography.Paragraph type="secondary" style={{ marginBottom: 24 }}>
          连接钱包完成身份验证。签名仅用于确认钱包归属，
          <strong>不会发起链上交易，也不消耗 gas</strong>。
        </Typography.Paragraph>

        <Button
          type="primary"
          size="large"
          block
          loading={busy || isConnecting}
          onClick={handleLogin}
          disabled={busy}
        >
          {busy ? stageText : isConnected ? '签名并登录' : '连接钱包'}
        </Button>

        {/* 连上之后把地址显示出来：多账号用户需要先确认"签的是哪一个"，
            否则会在钱包里签完才发现拿错了号。 */}
        {isConnected && address && !busy && (
          <Alert
            type="info"
            showIcon
            style={{ marginTop: 16 }}
            title={`已连接 ${shorten(address)}`}
            description="点击上方按钮签名，即可进入。"
          />
        )}

        {busy && currentStep >= 0 && (
          <Steps
            size="small"
            orientation="vertical"
            current={currentStep}
            style={{ marginTop: 24 }}
            items={STEP_ORDER.map((key) => ({ title: STEP_LABEL[key] }))}
          />
        )}

        {error && <Alert type="error" showIcon title={error} style={{ marginTop: 16 }} />}
      </Card>
    </div>
  )
}
