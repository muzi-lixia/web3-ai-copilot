import { http } from './client.ts'
import type { ApiResponse, StakingSummary } from '../types/api'

/**
 * 查询质押仓位。
 *
 * 不传 chainId：与 /wallet、/risk、/market 同一口径 —— 由后端按默认链决定，
 * 前端不内置链清单，换链只改后端配置。
 *
 * 这个接口会顺带回一个 `liquid_value_usd`（未质押资产的估值），
 * 用来解释 `portfolio_ratio` 的分母。它与资产接口用的是同一个后端口径，
 * 所以前端不要再自己拿两个接口的数相除 —— 那会变成第二份定义。
 */
export async function fetchStakingPositions(): Promise<StakingSummary> {
  const { data } = await http.get<ApiResponse<StakingSummary>>('/me/staking-positions')
  return data.data
}
