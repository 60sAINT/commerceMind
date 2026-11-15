import { requestJson } from '@/api'
import type { MemoryDebugResponse } from '@/types/api'

interface MemoryOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

/**
 * GET /memory/{session_id}?user_id=...
 * 仅在 COMMERCEMIND_MEMORY_DEBUG=true 时注册，否则返回 404。
 */
export function getMemoryDebug(
  sessionId: string,
  userId: string,
  options: MemoryOptions = {},
): Promise<MemoryDebugResponse> {
  return requestJson<MemoryDebugResponse>(`/memory/${encodeURIComponent(sessionId)}`, {
    params: { user_id: userId },
    signal: options.signal,
    timeoutMs: options.timeoutMs ?? 10_000,
  })
}
