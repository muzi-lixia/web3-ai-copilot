// 流解析回归：模拟任意网络分块，不依赖真实模型或浏览器。
import assert from 'node:assert/strict'
import test from 'node:test'
import { consumeSSE } from '../src/api/sse.ts'

// 按不同字节长度拆分数据，刻意制造中文多字节字符与 SSE 边界跨块的情况。
function bytesStream(text, chunkSize) {
  const bytes = new TextEncoder().encode(text)
  return new ReadableStream({ start(controller) {
    for (let i = 0; i < bytes.length; i += chunkSize) controller.enqueue(bytes.slice(i, i + chunkSize))
    controller.close()
  } })
}

// 验证不同网络块尺寸下 UTF-8、CRLF、心跳和多帧解析结果一致。
test('SSE handles every byte boundary, Chinese UTF-8, CRLF, heartbeats and multiple frames', async () => {
  for (const size of [1, 2, 7, 1000]) {
    const events = []
    await consumeSSE(bytesStream(': ping\r\n\r\nevent: delta\r\ndata: {"text":"你好\\n世界"}\r\n\r\n' +
      'event: done\ndata: {"type":"done"}\n\n', size), (data) => {
      const event = JSON.parse(data)
      events.push(event)
      return event.type === 'done'
    })
    assert.deepEqual(events, [{ text: '你好\n世界' }, { type: 'done' }])
  }
})

// 没有终态的 EOF 必须报错，不能静默接受半段回复。
test('missing terminal event rejects instead of treating partial answer as complete', async () => {
  await assert.rejects(consumeSSE(bytesStream('data: {"text":"半段"}\n\n', 1), () => false), /提前结束/)
})

// 回调异常必须传播，并取消 reader，防止连接资源泄漏。
test('stream errors propagate and cancel the reader', async () => {
  let cancelled = false
  const stream = new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode('data: {"type":"error"}\n\n'))
  }, cancel() { cancelled = true } })
  await assert.rejects(consumeSSE(stream, () => { throw new Error('model failure') }), /model failure/)
  assert.equal(cancelled, true)
})

// 业务 error 帧要立即展示原因，不能像网络断流一样进入退避重连。
test('business SSE error is distinct from retryable network EOF', async () => {
  const { parseSSEEvent, SSEStreamError } = await import('../src/api/sse.ts')
  assert.throws(() => parseSSEEvent('{"type":"error","message":"轮次不存在"}'), SSEStreamError)
  assert.deepEqual(parseSSEEvent('{"type":"done"}'), { type: 'done' })
})

// 重复分页和迟到旧历史不能把同一消息重复显示，也不能覆盖正在展示的新正文。
test('history merge deduplicates repeated pages and keeps the latest snapshot', async () => {
  const { mergeMessages } = await import('../src/utils/chat.ts')
  const older = [
    { id: 'answer', seq: 2, content: '旧正文' },
    { id: 'question', seq: 1, content: '问题' },
  ]
  const current = [{ id: 'answer', seq: 2, content: '最新正文' }]
  const once = mergeMessages(older, current)
  assert.deepEqual(mergeMessages(older, once), once)
  assert.deepEqual(once.map((item) => item.id), ['question', 'answer'])
  assert.equal(once[1].content, '最新正文')
})

test('execution phases match persisted queue and running states', async () => {
  const { turnProgressText } = await import('../src/utils/chat.ts')
  assert.match(turnProgressText('queued'), /等待模型/)
  assert.match(turnProgressText('running'), /查询.*生成/)
})

test('pending persistence snapshot keeps stream open until saved done', async () => {
  const frames = []
  const data = new TextEncoder().encode(
    'event: snapshot\ndata: {"type":"snapshot","status":"failed","persistence_pending":true}\n\n' +
    'event: done\ndata: {"type":"done","status":"completed","persistence_pending":false}\n\n',
  )
  await consumeSSE(new ReadableStream({ start(controller) { controller.enqueue(data); controller.close() } }), (raw) => {
    const event = JSON.parse(raw)
    frames.push(event)
    return event.type === 'done'
  })
  assert.equal(frames.length, 2)
  assert.equal(frames[0].persistence_pending, true)
  assert.equal(frames[1].status, 'completed')
})
