import { http } from './client.ts'
import type { ApiResponse, MeResponse, NonceRequest, NonceResponse, TokenResponse, VerifyRequest } from '../types/api'

/** 取签名用的随机数（含要签的完整原文）。 */
export async function fetchNonce(address: string): Promise<NonceResponse> {
  const { data } = await http.post<ApiResponse<NonceResponse>>('/auth/challenges', { address } satisfies NonceRequest)
  return data.data
}

/** 提交签名，换取登录凭证。 */
export async function verifySignature(payload: VerifyRequest): Promise<TokenResponse> {
  const { data } = await http.post<ApiResponse<TokenResponse>>('/auth/tokens', payload)
  return data.data
}

/** 校验当前凭证是否仍然有效，返回其代表的地址。 */
export async function fetchMe(): Promise<MeResponse> {
  const { data } = await http.get<ApiResponse<MeResponse>>('/users/me')
  return data.data
}
