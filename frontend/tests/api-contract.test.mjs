// 真实 API 模块通过 Axios 测试适配器验证方法、路径、正文与解包，不请求外部服务。
import assert from 'node:assert/strict'
import test from 'node:test'
import { ApiError, responseError, unwrapResponse } from '../src/api/response.ts'

const storage = new Map()
globalThis.sessionStorage = {
  getItem: (key) => storage.get(key) ?? null,
  setItem: (key, value) => storage.set(key, value),
  removeItem: (key) => storage.delete(key),
}
const { http } = await import('../src/api/client.ts')
const { useAuthStore } = await import('../src/stores/auth.ts')
const auth = await import('../src/api/auth.ts')
const chat = await import('../src/api/chat.ts')
const { fetchWalletAssets } = await import('../src/api/wallet.ts')
const { fetchRiskReport } = await import('../src/api/risk.ts')
const { fetchStakingPositions } = await import('../src/api/staking.ts')
const { fetchMarketQuotes } = await import('../src/api/market.ts')
const { AxiosError } = await import('axios')

function mockReply(body, status = 200) {
  const calls = []
  http.defaults.adapter = async (config) => {
    calls.push(config)
    const response = { data: body, status, statusText: '', headers: {}, config }
    if (status >= 400) throw new AxiosError('HTTP failure', undefined, config, null, response)
    return response
  }
  chat.agentHttp.defaults.adapter = http.defaults.adapter
  return calls
}

const envelope = (data) => ({ code: 0, msg: 'success', data })

test('response unwrap preserves null, zero and arrays and rejects nonzero business codes', () => {
  for (const value of [null, 0, [], { amount: '12345678901234567890.000000000000000001' }]) {
    assert.deepEqual(unwrapResponse(envelope(value)), value)
  }
  assert.throws(() => unwrapResponse({ code: 40901, msg: '正在生成', data: null }, 409),
    (error) => error instanceof ApiError && error.code === 40901 && error.status === 409)
  assert.throws(() => unwrapResponse({ id: 'old response' }), /响应格式/)
  assert.throws(() => unwrapResponse({ code: '0', msg: 'success', data: {} }), /响应格式/)
  assert.equal(responseError({ code: 42201, msg: '参数错误', data: null }, 422).message, '参数错误')
})

test('login and business APIs use current resources and return unwrapped domain data', async () => {
  const cases = [
    [() => auth.fetchNonce('0x123'), 'post', '/auth/challenges', { nonce: 'n', message: '签名原文' }],
    [() => auth.verifySignature({ message: '签名原文', signature: 'signed' }), 'post', '/auth/tokens', { token: 'jwt', token_type: 'Bearer' }],
    [() => auth.fetchMe(), 'get', '/users/me', { address: '0x123' }],
    [() => fetchWalletAssets(), 'get', '/me/assets', { amount: '999999999999999999.0000001', price: null }],
    [() => fetchRiskReport(), 'get', '/me/risk-report', { risk_level: 'unknown' }],
    [() => fetchStakingPositions(), 'get', '/me/staking-positions', { positions: [] }],
    [() => fetchMarketQuotes({ chainId: 80094, symbols: ['BERA', 'WETH'] }), 'get', '/markets/quotes', { tokens: [] }],
  ]
  for (const [call, method, path, data] of cases) {
    const calls = mockReply(envelope(data), path === '/auth/challenges' ? 201 : 200)
    assert.deepEqual(await call(), data)
    assert.equal(calls[0].method, method)
    assert.equal(calls[0].url, path)
    if (path === '/markets/quotes') assert.deepEqual(calls[0].params, { chain_id: 80094, symbols: 'BERA,WETH' })
  }
})

test('chat uses PUT session/cancellation and creates retry turns with original reference', async () => {
  let calls = mockReply(envelope({ id: 'session' }), 201)
  assert.deepEqual(await chat.openSession(), { id: 'session' })
  assert.equal(calls[0].method, 'put')
  assert.equal(calls[0].url, '/chat/session')
  calls = mockReply(envelope({ turn_id: 'turn', created: true }), 202)
  assert.equal((await chat.submitTurn('你好', 'request')).turn_id, 'turn')
  assert.deepEqual(JSON.parse(calls[0].data), { message: '你好', client_msg_id: 'request' })
  calls = mockReply(envelope({ turn_id: 'retry', created: true }), 202)
  assert.equal((await chat.retryTurn('turn', 'new-request')).turn_id, 'retry')
  assert.equal(calls[0].url, '/chat/session/turns')
  assert.deepEqual(JSON.parse(calls[0].data), { retry_of: 'turn', client_msg_id: 'new-request' })
  calls = mockReply(envelope({ status: 'cancelled' }))
  assert.equal((await chat.cancelTurn('turn')).status, 'cancelled')
  assert.equal(calls[0].method, 'put')
  assert.equal(calls[0].url, '/chat/turns/turn/cancellation')
  calls = mockReply(envelope({ items: [], next_cursor: 2 }))
  assert.equal((await chat.loadMessages(4)).next_cursor, 2)
  assert.equal(calls[0].params.before_seq, 4)
  calls = mockReply(envelope({ status: 'running' }))
  assert.equal((await chat.getTurn('turn')).status, 'running')
  assert.equal(calls[0].url, '/chat/turns/turn')
})

test('HTTP failures and 2xx business failures reject, and stale 401 preserves a newer login', async () => {
  useAuthStore.getState().setAuth('old-token', 'owner')
  mockReply({ code: 40101, msg: '登录已过期', data: null }, 401)
  await assert.rejects(auth.fetchMe(), (error) => error.code === 40101 && error.status === 401)
  assert.equal(useAuthStore.getState().token, null)
  useAuthStore.getState().setAuth('old-token', 'owner')
  http.defaults.adapter = async (config) => {
    useAuthStore.getState().setAuth('new-token', 'new-owner')
    throw new AxiosError('unauthorized', undefined, config, null,
      { data: { code: 40101, msg: '旧凭据过期', data: null }, status: 401, headers: {}, config })
  }
  await assert.rejects(auth.fetchMe(), ApiError)
  assert.equal(useAuthStore.getState().token, 'new-token')
  mockReply({ code: 40901, msg: '会话忙碌', data: null })
  await assert.rejects(chat.submitTurn('问题', 'id'), (error) => error.code === 40901)
  mockReply({ id: 'legacy' })
  await assert.rejects(chat.openSession(), /响应格式/)
  useAuthStore.getState().clearAuth()
})

test('SSE HTTP errors read msg/code without retrying permanent resource failures', async () => {
  const originalFetch = globalThis.fetch
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    return new Response(JSON.stringify({ code: 40401, msg: '轮次不存在', data: null }),
      { status: 404, headers: { 'Content-Type': 'application/json' } })
  }
  try {
    await assert.rejects(chat.followTurn('missing', new AbortController().signal, () => {}, () => {}),
      (error) => error instanceof ApiError && error.message === '轮次不存在' && error.code === 40401)
    assert.equal(calls, 1)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('a changed login credential does not reuse the previous successful authentication probe', async () => {
  const React = await import('react')
  const { renderToString } = await import('react-dom/server')
  const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
  const { useSessionProbe } = await import('../src/hooks/useSessionProbe.ts')
  const queryClient = new QueryClient()
  queryClient.setQueryData(['auth', 'me', 'old-token'], { address: 'old-owner' })
  function Probe() {
    const probe = useSessionProbe('new-token')
    return React.createElement('span', null, probe.data?.address ?? 'unverified')
  }
  const rendered = renderToString(React.createElement(QueryClientProvider, { client: queryClient }, React.createElement(Probe)))
  assert.match(rendered, /unverified/)
  assert.ok(!rendered.includes('old-owner'))
  assert.equal(queryClient.getQueryCache().getAll().length, 2)
  queryClient.clear()
})

test('a non-stream 200 response stops SSE recovery instead of reconnecting forever', async () => {
  const originalFetch = globalThis.fetch
  let calls = 0
  globalThis.fetch = async (url) => {
    calls++
    assert.ok(!url.includes('/api/v1//'))
    return new Response(JSON.stringify({ code: 0, msg: 'success', data: {} }),
      { headers: { 'Content-Type': 'application/json' } })
  }
  const originalBase = http.defaults.baseURL
  http.defaults.baseURL = '/api/v1/'
  try {
    await assert.rejects(chat.followTurn('turn', new AbortController().signal, () => {}, () => {}), /有效对话流/)
    assert.equal(calls, 1)
  } finally {
    globalThis.fetch = originalFetch
    http.defaults.baseURL = originalBase
  }
})


test('new resource calls separate public business reads and private Agent settings', async () => {
  const resources = await import('../src/api/resources.ts')
  let calls = mockReply(envelope([{ chain_id: 80094, name: 'Berachain' }]))
  await resources.getChains()
  assert.equal(calls[0].url, '/chains')
  calls = mockReply(envelope({ result_id: 'balance-result' }), 201)
  await resources.queryAssets(80094, 'BERA', 'CNY')
  assert.equal(calls[0].url, '/me/balance-results')
  assert.deepEqual(JSON.parse(calls[0].data), { chain_id: 80094, symbol: 'BERA', currency: 'CNY' })
  assert.equal(calls[0].method, 'post')
  calls = mockReply(envelope({ provider: 'ollama' }))
  await resources.getModel()
  assert.equal(calls[0].url, '/chat/session/model')
  await resources.selectModel('deepseek', true)
  assert.deepEqual(JSON.parse(calls[1].data), { provider: 'deepseek', accept_external: true })
  calls = mockReply(envelope({ price: '0.123456789012345678' }))
  assert.equal((await resources.getPublicPrice(80094, 'BERA', 'CNY')).price, '0.123456789012345678')
  assert.deepEqual(calls[0].params, { chain_id: 80094, symbol: 'BERA', currency: 'CNY' })
})

test('refresh network failure retains identity, explicit session rejection clears it', async () => {
  const axios = (await import('axios')).default
  const original = axios.defaults.adapter
  useAuthStore.getState().setAuth('access-old', 'wallet-a', 'refresh-a')
  http.defaults.adapter = async config => {
    const response = { data: { code: 40103, msg: 'access expired', data: null }, status: 401, statusText: '', headers: {}, config }
    throw new AxiosError('access expired', undefined, config, null, response)
  }
  try {
    axios.defaults.adapter = async () => { throw new AxiosError('network unavailable') }
    await assert.rejects(http.get('/users/me'), error => error.status === 503)
    assert.equal(useAuthStore.getState().token, 'access-old')
    assert.equal(useAuthStore.getState().refreshToken, 'refresh-a')
    axios.defaults.adapter = async config => {
      const response = { data: { code: 40101, msg: 'session expired', data: null }, status: 401, statusText: '', headers: {}, config }
      throw new AxiosError('session expired', undefined, config, null, response)
    }
    await assert.rejects(http.get('/users/me'), error => error.status === 401)
    assert.equal(useAuthStore.getState().token, null)
  } finally { axios.defaults.adapter = original }
})

test('offline safe history survives expiry but is removed on wallet switch or logout', async () => {
  const cache = await import('../src/utils/chatCache.ts')
  const messages = [{ id: 'm', role: 'assistant', content: '查询完成', result_refs: [{ result_id: 'r' }] }]
  useAuthStore.getState().setAuth('token-a', 'wallet-a', 'refresh-a')
  cache.saveChatCache('wallet-a', messages)
  useAuthStore.getState().clearAuth()
  assert.equal(cache.readChatCache().owner, 'wallet-a')
  useAuthStore.getState().setAuth('token-b', 'wallet-b', 'refresh-b')
  assert.equal(cache.readChatCache(), null)
  cache.saveChatCache('wallet-b', messages)
  useAuthStore.getState().clearAuth('logout')
  assert.equal(cache.readChatCache(), null)
})
