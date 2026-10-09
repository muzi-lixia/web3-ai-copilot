import { http } from './client.ts'
import type { ApiResponse, TokenMarketList } from '../types/api'

/**
 * 查询 Token 行情。
 *
 * 不传 chainId：由后端按默认链决定，与 /wallet 的口径一致 ——
 * 前端不内置链清单，换链只改后端配置。
 * 不传 symbols：返回该链候选清单的全部，省掉前端再维护一份「要查哪些币」的副本
 * （两份清单迟早会不一致，而且不一致时不报错，只是少几个币）。
 */
export async function fetchMarketQuotes(
  options: { chainId?: number; symbols?: string[] } = {},
): Promise<TokenMarketList> {
  const { data } = await http.get<ApiResponse<TokenMarketList>>('/markets/quotes', {
    params: {
      chain_id: options.chainId,
      symbols: options.symbols?.length ? options.symbols.join(',') : undefined,
    },
  })
  return data.data
}
