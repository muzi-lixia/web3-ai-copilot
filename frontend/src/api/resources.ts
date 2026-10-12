/** 基础服务拥有真实资产；Agent 仅调度与返回引用。 */
import { http } from './client.ts'
import { agentHttp } from './chat.ts'
import type { ApiResponse, MarketQuote, ResultEnvelope } from '../types/api'
export interface Chain { chain_id: number; name: string; key: string; symbols: string[] }
export interface ModelOption { provider: string; model: string | null; enabled: boolean; external: boolean; context_window?: number }
export interface ModelInfo extends ModelOption { available: ModelOption[]; context_ttl_hours: number }
export const getChains = async () => (await http.get<ApiResponse<Chain[]>>('/chains')).data.data
export const getModel = async () => (await agentHttp.get<ApiResponse<ModelInfo>>('/chat/session/model')).data.data
export const selectModel = async (provider: string, acceptExternal: boolean) =>
  (await agentHttp.put<ApiResponse<ModelInfo>>('/chat/session/model', { provider, accept_external: acceptExternal })).data.data
/** 创建一次资产查询；同时返回结果信封（含 result_id 与业务数据）。 */
export const queryAssets = async (chainId?: number, symbol?: string, currency = 'USD') =>
  (await http.post<ApiResponse<{ summary: { result_id: string }; result: ResultEnvelope }>>('/me/balance-results', {
    chain_id: chainId, symbol: symbol || undefined, currency,
  })).data.data.result
export const getPublicPrice = async (chainId: number, symbol: string, currency: string) =>
  (await http.get<ApiResponse<MarketQuote>>('/markets/prices', { params: { chain_id: chainId, symbol, currency } })).data.data
