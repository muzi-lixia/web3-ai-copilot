import { http } from './client.ts'
import type { ApiResponse, WalletAssets } from '../types/api'

/**
 * 查询钱包资产。
 *
 * 不传 chain_id：由后端按 `DEFAULT_CHAIN_ID` 决定，并把实际使用的链
 * （chain_id / chain_name / explorer）随响应带回来。前端因此不需要内置
 * 一份链清单 —— 换链只改后端配置，前端不动。
 * 需要做链切换时，把 chain_id 传进来即可，接口已经支持。
 */
export async function fetchWalletAssets(address: string): Promise<WalletAssets> {
  const { data } = await http.get<ApiResponse<WalletAssets>>(`/wallets/${address}/assets`)
  return data.data
}
