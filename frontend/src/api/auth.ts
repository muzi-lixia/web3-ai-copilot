import { http } from './client'
import type { MeResponse, NonceResponse, TokenResponse, VerifyRequest } from '../types/api'

/** 取签名用的随机数（含要签的完整原文）。 */
export async function fetchNonce(address: string): Promise<NonceResponse> {
  const { data } = await http.post<NonceResponse>('/auth/nonce', { address })
  return data
}

/** 提交签名，换取登录凭证。 */
export async function verifySignature(payload: VerifyRequest): Promise<TokenResponse> {
  const { data } = await http.post<TokenResponse>('/auth/verify', payload)
  return data
}

/** 校验当前凭证是否仍然有效，返回其代表的地址。 */
export async function fetchMe(): Promise<MeResponse> {
  const { data } = await http.get<MeResponse>('/auth/me')
  return data
}
