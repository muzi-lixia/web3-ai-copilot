/** 基础服务拥有真实资产；Agent 仅调度与返回引用。 */
import { http } from './client.ts'
import { agentHttp } from './chat.ts'
import type { ApiResponse } from '../types/api'
export interface Chain { chain_id: number; name: string; key: string; symbols: string[] }
export interface ModelOption { provider: string; model: string | null; enabled: boolean; external: boolean; context_window?: number }
export interface ModelInfo extends ModelOption { available: ModelOption[]; context_ttl_hours: number }
export const getChains = async () => (await http.get<ApiResponse<Chain[]>>('/chains')).data.data
export const getModel = async () => (await agentHttp.get<ApiResponse<ModelInfo>>('/chat/session/model')).data.data
export const selectModel = async (provider: string, acceptExternal: boolean) =>
  (await agentHttp.put<ApiResponse<ModelInfo>>('/chat/session/model', { provider, accept_external: acceptExternal })).data.data
export const queryAssets = async (chainId?: number, symbol?: string, currency = 'USD') =>
  (await http.post<ApiResponse<{ result_id: string }>>('/me/balance-results', { chain_id: chainId, symbol: symbol || undefined, currency })).data.data

export interface PublicPrice { symbol: string; currency: string; price: string; market_cap: string | null;
  volume_24h: string | null; change_24h: number | null; change_7d: string | null; source: string;
  queriedAt: string; stale: boolean; exchange_rate_date: string | null }
export const getPublicPrice = async (chainId: number, symbol: string, currency: string) =>
  (await http.get<ApiResponse<PublicPrice>>('/markets/prices', { params: { chain_id: chainId, symbol, currency } })).data.data
