import assert from 'node:assert/strict'
import test from 'node:test'
import { parseCatalog } from '../src/utils/openapi.ts'

test('catalog follows registered operations, ignores path metadata and includes future endpoints', () => {
  const document = { openapi: '3.1.0', info: { title: 'Foundation' }, paths: {
    '/new-feature': { parameters: [], post: { tags: ['新增能力'], summary: '新增接口', deprecated: true } },
    '/assets': { get: { tags: ['资产查询'], summary: '本人资产', security: [{ Wallet: [] }] } },
  }, components: { securitySchemes: { Wallet: { type: 'http', scheme: 'bearer' } } } }
  const result = parseCatalog(document)
  assert.equal(result.title, 'Foundation')
  assert.equal(result.entries.length, 2)
  assert.equal(result.entries.find(e => e.path === '/new-feature').deprecated, true)
  assert.equal(result.entries.find(e => e.path === '/assets').auth, 'Bearer 凭证')
})

test('security inheritance, public overrides, optional auth and refresh body credentials remain distinct', () => {
  const document = { openapi: '3.0.3', paths: {
    '/private': { get: {} },
    '/public': { get: { security: [] } },
    '/optional': { get: { security: [{}, { Wallet: [] }] } },
    '/refresh': { post: { security: [], 'x-auth-description': 'Refresh Token（请求体）' } },
  }, security: [{ Wallet: [] }], components: { securitySchemes: { Wallet: { type: 'http', scheme: 'bearer' } } } }
  const entries = Object.fromEntries(parseCatalog(document).entries.map(e => [e.path, e]))
  assert.equal(entries['/private'].auth, 'Bearer 凭证')
  assert.equal(entries['/public'].auth, '公开访问')
  assert.equal(entries['/optional'].auth, '匿名访问 或 Bearer 凭证')
  assert.equal(entries['/refresh'].auth, 'Refresh Token（请求体）')
})

test('invalid documents fail explicitly instead of generating a fake catalog', () => {
  for (const value of [null, '<html>proxy error</html>', { code: 0, data: {} }, { openapi: '3.1.0', paths: [] }]) {
    assert.throws(() => parseCatalog(value), /有效的 OpenAPI/)
  }
  assert.deepEqual(parseCatalog({ openapi: '3.1.0', paths: {} }).entries, [])
})
