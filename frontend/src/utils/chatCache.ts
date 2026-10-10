/** 标签页内的展示历史缓存（包含用户原文）：不缓存基础服务资产结果、凭证或模型状态。 */
import type { ChatMessage } from '../types/api'
const KEY = 'web3-safe-chat-cache'
export interface CachedChat { owner: string; messages: ChatMessage[]; updatedAt: string }
export function readChatCache(): CachedChat | null {
  try {
    const data = JSON.parse(sessionStorage.getItem(KEY) ?? 'null')
    return data && typeof data.owner === 'string' && Array.isArray(data.messages) && typeof data.updatedAt === 'string' ? data : null
  } catch { return null }
}
export function saveChatCache(owner: string, messages: ChatMessage[]) {
  try { sessionStorage.setItem(KEY, JSON.stringify({ owner: owner.toLowerCase(), messages, updatedAt: new Date().toISOString() })) } catch { /* 存储不可用不影响服务端持久化 */ }
}
export function clearChatCache() { try { sessionStorage.removeItem(KEY) } catch { /* 受限浏览器 */ } }
export function clearDifferentChatCache(owner: string) {
  const cache = readChatCache()
  if (cache && cache.owner !== owner.toLowerCase()) clearChatCache()
}
