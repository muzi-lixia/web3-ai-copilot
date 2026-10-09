import { http } from './client.ts'
import type { ApiResponse, RiskReport } from '../types/api'

/**
 * 查询资产风险报告。
 *
 * 不传 chainId：与 /wallet、/market 同一口径 —— 由后端按默认链决定，
 * 前端不内置链清单，换链只改后端配置。
 */
export async function fetchRiskReport(address: string): Promise<RiskReport> {
  const { data } = await http.get<ApiResponse<RiskReport>>(`/wallets/${address}/risk-report`)
  return data.data
}
