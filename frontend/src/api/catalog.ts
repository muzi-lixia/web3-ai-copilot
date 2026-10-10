/** OpenAPI 是原始文档，不使用业务响应拦截器，也不发送用户凭证。 */
import axios from 'axios'
import { parseCatalog } from '../utils/openapi.ts'

export type ServiceName = 'foundation' | 'agent'
export async function getCatalog(service: ServiceName, signal?: AbortSignal) {
  const url = service === 'foundation'
    ? import.meta.env?.VITE_FOUNDATION_OPENAPI_URL ?? '/foundation-openapi'
    : import.meta.env?.VITE_AGENT_OPENAPI_URL ?? '/agent-openapi'
  const response = await axios.get(url, { signal, timeout: 20000, withCredentials: false })
  return parseCatalog(response.data)
}
