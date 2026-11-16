import { requestJson } from '@/api'
import type { SessionMessage, SessionSummary } from '@/types/api'

export function createSession(userId: string): Promise<SessionSummary> {
  return requestJson<SessionSummary>('/sessions', { method: 'POST', params: { user_id: userId } })
}

export function listSessions(userId: string, limit = 30, before?: string, signal?: AbortSignal) {
  return requestJson<{ items: SessionSummary[]; next_cursor: string | null }>('/sessions', {
    params: { user_id: userId, limit, before },
    signal,
  })
}

export function listSessionMessages(
  sessionId: string,
  userId: string,
  limit = 50,
  before?: number,
  signal?: AbortSignal,
) {
  return requestJson<{ items: SessionMessage[]; next_cursor: number | null }>(
    `/sessions/${encodeURIComponent(sessionId)}/messages`,
    { params: { user_id: userId, limit, before }, signal },
  )
}

export function deleteSession(sessionId: string, userId: string): Promise<{ deleted: boolean }> {
  return requestJson(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    params: { user_id: userId },
  })
}
