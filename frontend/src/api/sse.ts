/**
 * 解析 SSE 帧，不假设一次 HTTP read 就是一条事件。
 * TextDecoder 的流模式保留跨块 UTF-8 字节，避免中文被拆成乱码；缓冲区保留尚未完成的帧。
 * 空行是帧边界，兼容 LF/CRLF；同一帧的多行 data 用换行拼接，注释心跳自动忽略。
 * onData 返回 true 表示已收到业务终态；未见终态就 EOF 必须报错，不能把半段回复标为完成。
 */
export async function consumeSSE(
  stream: ReadableStream<Uint8Array>,
  onData: (data: string) => boolean,
) {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read()
      // 流结束时刷新解码器中尚未完成的字节，再检查剩余完整帧。
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      let match: RegExpExecArray | null
      // 一个网络块可能包含多帧，也可能只有半帧；循环消费完整帧，把尾部留到下一次 read。
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, match.index)
        buffer = buffer.slice(match.index + match[0].length)
        const data = frame.split(/\r?\n/).filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).replace(/^ /, '')).join('\n')
        if (data && onData(data)) return
      }
      if (done) throw new Error('连接提前结束，回复未完成，请重试')
    }
  } finally {
    // 终态、解析错误和用户取消都必须释放 reader，避免连接与浏览器资源持续占用。
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}


/** 服务端已经明确报告的业务错误，不等同于网络 EOF；应展示原因而不是自动重连。 */
export class SSEStreamError extends Error {
  code?: string
  constructor(message: string, code?: string) {
    super(message)
    this.name = 'SSEStreamError'
    this.code = code
  }
}

export function parseSSEEvent(data: string) {
  const event = JSON.parse(data)
  if (event.type === 'error') throw new SSEStreamError(event.message ?? '对话连接失败', event.code)
  return event
}
