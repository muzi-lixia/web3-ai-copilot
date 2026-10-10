import { useAppKit } from '@reown/appkit/react'
import { useCallback, useState } from 'react'
import { useAccount, useDisconnect, useSignMessage } from 'wagmi'

import { http } from '../api/client'
import { fetchNonce, verifySignature } from '../api/auth'
import { useAuthStore } from '../stores/auth'

/** 登录流程的阶段。页面按它显示"现在卡在哪一步"。 */
export type LoginStage = 'idle' | 'nonce' | 'signing' | 'verifying' | 'done'

const STAGE_TEXT: Record<LoginStage, string> = {
  idle: '签名并登录',
  nonce: '正在获取签名消息…',
  signing: '请在钱包中确认签名',
  verifying: '正在验证签名…',
  done: '登录成功',
}

/**
 * 钱包登录：连接 → 取 nonce → 签名 → 换 token。
 *
 * 拆成 4 个阶段而不是一个 loading，是因为**卡住的位置不同，原因完全不同**：
 * 停在 signing 是用户没在钱包里点确认，停在 nonce 是后端没响应。
 * 用户看得到阶段名，就能自己判断该做什么。
 *
 * 连接与登录**分成两次点击**（未连接时这一步只弹钱包，连接成功后再点才签名）：
 * 钱包弹窗是异步的，用户可能切账号、可能直接关掉。要在一个函数里等出结果，
 * 就得处理"弹窗关闭/切换账号/超时"一整串分支；交给 wagmi 维护连接状态，
 * 下一次点击时它自然是最新的，这段逻辑就整块消失了。
 */
export function useWalletLogin() {
  const { open } = useAppKit()
  const { address, isConnected, isConnecting } = useAccount()
  const { disconnectAsync } = useDisconnect()
  const { signMessageAsync } = useSignMessage()
  const setAuth = useAuthStore((state) => state.setAuth)

  const [signingMessage, setSigningMessage] = useState<string | null>(null)
  const [stage, setStage] = useState<LoginStage>('idle')
  const [error, setError] = useState<string | null>(null)

  const login = useCallback(async () => {
    setError(null)
    setSigningMessage(null)

    // 1. 还没连钱包：只负责把弹窗打开，本轮到此为止。
    if (!isConnected || !address) {
      open()
      return null
    }

    try {
      // 2. 取 nonce（后端连签名原文一起下发）
      setStage('nonce')
      const nonce = await fetchNonce(address)
      setSigningMessage(nonce.message)

      // 3. 签名 —— 签的是后端下发的原文，不是前端拼的。
      //    两端各拼一次模板，迟早会因为空格/换行不一致而"莫名"验签失败。
      setStage('signing')
      const signature = await signMessageAsync({ account: address, message: nonce.message })

      // 4. 换登录凭证
      setStage('verifying')
      const result = await verifySignature({ message: nonce.message, signature })
      setAuth(result.token, result.address, result.refresh_token, result)
      setStage('done')
      return result.address
    } catch (err) {
      setStage('idle')
      const message = err instanceof Error ? err.message : '登录失败，请重试'
      // 用户在钱包里点了"拒绝"：原样抛出会是一长串英文，这里给一句人话。
      setError(/user rejected|denied/i.test(message) ? '你取消了签名' : message)
      return null
    }
  }, [address, isConnected, open, signMessageAsync, setAuth])

  const logout = useCallback(async () => {
    try { await http.delete('/auth/session') } catch { /* 服务不可达时仍清除本地身份 */ }
    useAuthStore.getState().clearAuth('logout')
    try {
      await disconnectAsync()
    } catch {
      // 断开失败不影响本地登出：凭证已经清了，页面会回到登录页。
    }
    setStage('idle')
    setError(null)
  }, [disconnectAsync])

  const busy = stage !== 'idle' && stage !== 'done'

  return {
    login,
    logout,
    stage,
    stageText: STAGE_TEXT[stage],
    signingMessage,
    error,
    busy,
    /** 已连接钱包但还没换到凭证——页面据此把按钮文案切成"签名并登录"。 */
    isConnected,
    address,
    isConnecting,
  }
}
