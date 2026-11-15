import { requestJson } from '@/api'
import type { ChatRequest, ChatResponse } from '@/types/api'

interface ChatOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

/**
 * POST /chat
 * - session_id 为空时由后端生成，并在响应中返回。
 * - client_request_id 用于幂等回放：同 ID + 同消息会返回上次结果。
 * - skill_ids 传 [] 可关闭本轮自动技能选择。
 */
export function sendChat(payload: ChatRequest, options: ChatOptions = {}): Promise<ChatResponse> {
  return requestJson<ChatResponse>('/chat', {
    method: 'POST',
    body: payload,
    signal: options.signal,
    timeoutMs: options.timeoutMs,
  })
}
