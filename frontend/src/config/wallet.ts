import { WagmiAdapter } from '@reown/appkit-adapter-wagmi'
import type { AppKitNetwork } from '@reown/appkit/networks'
import { berachain } from '@reown/appkit/networks'
import { createAppKit } from '@reown/appkit/react'

/**
 * 钱包连接配置（Reown AppKit + wagmi）。
 *
 * 为什么是 AppKit 而不是 wagmi 原生的 injected()：
 * injected 只能唤起浏览器插件钱包，**手机打不开、没有插件的浏览器也打不开**。
 * 而这个项目是要给出链接让别人点开看的，连不上钱包就等于打不开。
 * AppKit 额外带来 WalletConnect 扫码、账户弹窗（切号/断连/复制地址），
 * 并且它本身就是 wagmi 的适配层 —— 下游的 useAccount / useSignMessage 一行都不用改。
 *
 * 代价是必须有一个 Reown projectId（客户端公开标识，不是密钥）。
 * 没有它 AppKit 连弹窗都起不来，所以这里直接报错并说清怎么补，
 * 而不是让它以一个看不懂的运行时异常炸在浏览器控制台里。
 */
const projectId = import.meta.env.VITE_REOWN_PROJECT_ID

if (!projectId) {
  throw new Error(
    '缺少 VITE_REOWN_PROJECT_ID：把 frontend/.env.example 复制为 frontend/.env 并填入 projectId（在 https://cloud.reown.com 免费创建）。',
  )
}

/** 目前只登记 Berachain：后端链注册表也只有这一条（backend/app/constants/chains.py）。 */
const networks = [berachain] as [AppKitNetwork, ...AppKitNetwork[]]

export const wagmiAdapter = new WagmiAdapter({ projectId, networks })

createAppKit({
  adapters: [wagmiAdapter],
  networks,
  projectId,
  metadata: {
    name: 'Web3 AI Copilot',
    description: '基于 AI Agent 的链上资产分析与知识助手',
    url: window.location.origin,
    icons: [],
  },
  features: {
    analytics: false,
    // 只做钱包登录：留着邮箱/社交登录会让"我是谁"变成一堆与链上无关的身份，
    // 而本项目全链路都建立在「地址即身份」上。
    email: false,
    socials: false,
  },
})

export const wagmiConfig = wagmiAdapter.wagmiConfig
