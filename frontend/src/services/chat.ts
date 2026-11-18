import { ApiError, getApiBaseUrl, resolveErrorMessage } from '@/api'
import type { ChatRequest, ChatResponse } from '@/types/api'

interface ChatOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

export interface StreamHandlers {
  onMeta: (meta: { request_id: string; session_id: string }) => void
  onDelta: (text: string) => void
  onDone: (response: ChatResponse) => void
}

function apiError(response: Response, payload: unknown): ApiError {
  const data = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {}
  const code = typeof data.error_code === 'string' ? data.error_code : `http_${response.status}`
  const detail = typeof data.detail === 'string' ? data.detail : undefined
  return new ApiError({
    status: response.status,
    code,
    message: resolveErrorMessage(code, response.status, detail),
    requestId: typeof data.request_id === 'string' ? data.request_id : null,
    body: payload,
  })
}

export async function streamChat(
  payload: ChatRequest,
  handlers: StreamHandlers,
  options: ChatOptions = {},
): Promise<ChatResponse> {
  const response = await fetch(`${getApiBaseUrl()}/chat/stream`, {
    method: 'POST',
    headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: options.signal,
  })

  if (!response.ok) {
    let payload: unknown = null
    try { payload = await response.json() } catch { /* use HTTP fallback */ }
    throw apiError(response, payload)
  }
  if (!response.body) throw new Error('后端未返回 SSE 响应流')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventName = 'message'
  let data = ''
  let done: ChatResponse | null = null
  let pendingDelta = ''
  let frame = 0

  const flush = () => {
    frame = 0
    if (pendingDelta) {
      handlers.onDelta(pendingDelta)
      pendingDelta = ''
    }
  }
  const dispatch = () => {
    if (!data) return
    let parsed: Record<string, unknown>
    try { parsed = JSON.parse(data) as Record<string, unknown> } catch { throw new Error('SSE 事件数据格式无效') }
    if (eventName === 'meta') {
      handlers.onMeta({ request_id: String(parsed.request_id ?? ''), session_id: String(parsed.session_id ?? '') })
    } else if (eventName === 'delta') {
      pendingDelta += String(parsed.text ?? '')
      if (!frame) frame = requestAnimationFrame(flush)
    } else if (eventName === 'done') {
      if (frame) cancelAnimationFrame(frame)
      flush()
      done = {
        request_id: String(parsed.request_id ?? ''),
        session_id: String(parsed.session_id ?? ''),
        response: String(parsed.response ?? ''),
        latency_ms: 0,
        input_tokens: null,
        output_tokens: null,
      }
      handlers.onDone(done)
    } else if (eventName === 'error') {
      throw apiError(response, parsed)
    }
    eventName = 'message'
    data = ''
  }

  try {
    while (true) {
      const result = await reader.read()
      buffer += decoder.decode(result.value, { stream: !result.done })
      let boundary: number
      while ((boundary = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, boundary).replace(/\r$/, '')
        buffer = buffer.slice(boundary + 1)
        if (!line) dispatch()
        else if (line.startsWith('event:')) eventName = line.slice(6).trim()
        else if (line.startsWith('data:')) data += `${data ? '\n' : ''}${line.slice(5).trimStart()}`
      }
      if (result.done) break
    }
    if (buffer.trim()) {
      if (buffer.startsWith('data:')) data = buffer.slice(5).trimStart()
      dispatch()
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined)
    throw error
  } finally {
    if (frame) cancelAnimationFrame(frame)
    await reader.cancel().catch(() => undefined)
  }
  if (!done) throw new Error('SSE 连接结束，但没有收到完成事件')
  return done
}
