import { create } from 'zustand'

import {
  createSession,
  deleteSession,
  listSessionMessages,
  listSessions,
} from '@/services/sessions'
import type { SessionSummary } from '@/types/api'
import { useChatStore, type ChatMessage } from './chat'
import { useSessionStore } from './session'

interface HistoryState {
  owner: string | null
  sessions: SessionSummary[]
  ready: boolean
  loading: boolean
  loadingMessages: boolean
  busy: boolean
  error: string | null
  messageCursor: number | null
  loadSessions: (userId: string) => Promise<void>
  loadMessages: (userId: string, sessionId: string, older?: boolean) => Promise<void>
  create: (userId: string) => Promise<void>
  remove: (userId: string, sessionId: string) => Promise<void>
}

let listController: AbortController | null = null
let messageController: AbortController | null = null
const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : '加载历史失败，请重试'

export const useHistoryStore = create<HistoryState>((set, get) => ({
  owner: null,
  sessions: [],
  ready: false,
  loading: false,
  loadingMessages: false,
  busy: false,
  error: null,
  messageCursor: null,
  loadSessions: async (userId) => {
    listController?.abort()
    const controller = new AbortController()
    listController = controller
    const changedUser = get().owner !== userId
    if (changedUser) {
      messageController?.abort()
      useChatStore.getState().cancelForSessionSwitch()
      // Session IDs in the local cache are not proof of user ownership.
      set({ owner: userId, sessions: [], ready: false, loadingMessages: false })
    }
    set({ loading: true, error: null })
    try {
      const sessions: SessionSummary[] = []
      let before: string | undefined
      do {
        const page = await listSessions(userId, 100, before, controller.signal)
        sessions.push(...page.items)
        const next = page.next_cursor ?? undefined
        if (!next || next === before) break
        before = next
      } while (!controller.signal.aborted)
      if (
        controller.signal.aborted ||
        useSessionStore.getState().userId !== userId ||
        useChatStore.getState().sendingSessionId !== null
      )
        return
      set({ sessions, ready: true })
      const selected = useSessionStore.getState().sessionId
      if (!sessions.some((session) => session.session_id === selected)) {
        useSessionStore.getState().setSessionId(sessions[0]?.session_id ?? '')
      }
    } catch (error) {
      if (!controller.signal.aborted) set({ error: errorMessage(error) })
    } finally {
      if (listController === controller) set({ loading: false })
    }
  },
  loadMessages: async (userId, sessionId, older = false) => {
    messageController?.abort()
    const controller = new AbortController()
    messageController = controller
    if (!sessionId || useChatStore.getState().sendingSessionId !== null) {
      set({ loadingMessages: false })
      return
    }
    const snapshot = useChatStore.getState().messagesBySession[sessionId]
    set({ loadingMessages: true, error: null, ...(older ? {} : { messageCursor: null }) })
    try {
      const page = await listSessionMessages(
        sessionId,
        userId,
        50,
        older ? (get().messageCursor ?? undefined) : undefined,
        controller.signal,
      )
      if (
        controller.signal.aborted ||
        useSessionStore.getState().userId !== userId ||
        useSessionStore.getState().sessionId !== sessionId ||
        useChatStore.getState().sendingSessionId !== null ||
        useChatStore.getState().messagesBySession[sessionId] !== snapshot
      )
        return
      const saved: ChatMessage[] = page.items.map((item) => ({
        id: `saved-${item.message_id}`,
        role: item.role,
        content: item.content,
        createdAt: item.created_at,
        requestId: item.client_request_id ?? undefined,
      }))
      const committedIds = new Set(page.items.map((item) => item.client_request_id).filter(Boolean))
      // Keep unsent/interrupted attempts, but remove ones already committed by the server.
      const local = (snapshot ?? [])
        .filter(
          (message) =>
            (message.status || message.interrupted) && !committedIds.has(message.requestId ?? null),
        )
        .map((message): ChatMessage =>
          message.status === 'pending'
            ? { ...message, status: 'error', error: '上次连接已结束，可重试', retryable: true }
            : message,
        )
      const messages = older ? [...saved, ...(snapshot ?? [])] : [...saved, ...local]
      useChatStore.setState((state) => ({
        messagesBySession: { ...state.messagesBySession, [sessionId]: messages },
      }))
      set({ messageCursor: page.next_cursor })
    } catch (error) {
      if (!controller.signal.aborted) set({ error: errorMessage(error) })
    } finally {
      if (messageController === controller) set({ loadingMessages: false })
    }
  },
  create: async (userId) => {
    if (
      get().busy ||
      get().loading ||
      !get().ready ||
      get().owner !== userId ||
      useSessionStore.getState().userId !== userId
    )
      return
    set({ busy: true, error: null })
    try {
      const selectedId = useSessionStore.getState().sessionId
      const chat = useChatStore.getState()
      const isEmpty = (session: SessionSummary) =>
        session.user_id === userId &&
        session.message_count === 0 &&
        (chat.messagesBySession[session.session_id]?.length ?? 0) === 0 &&
        chat.sendingSessionId !== session.session_id
      const emptySession =
        get().sessions.find((session) => session.session_id === selectedId && isEmpty(session)) ??
        get().sessions.find(isEmpty)
      if (emptySession) {
        if (emptySession.session_id !== selectedId) {
          chat.cancelForSessionSwitch()
          useSessionStore.getState().setSessionId(emptySession.session_id)
        }
        return
      }
      useChatStore.getState().cancelForSessionSwitch()
      const session = await createSession(userId)
      if (useSessionStore.getState().userId !== userId) return
      set((state) => ({ sessions: [session, ...state.sessions] }))
      useSessionStore.getState().setSessionId(session.session_id)
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: false })
    }
  },
  remove: async (userId, sessionId) => {
    if (get().busy) return
    listController?.abort()
    set({ busy: true, loading: false, error: null })
    try {
      useChatStore.getState().cancelForSessionSwitch()
      await deleteSession(sessionId, userId)
      if (useSessionStore.getState().userId !== userId) return
      useChatStore.getState().remove(sessionId)
      const sessions = get().sessions.filter((item) => item.session_id !== sessionId)
      set({ sessions })
      if (useSessionStore.getState().sessionId === sessionId) {
        useSessionStore.getState().setSessionId(sessions[0]?.session_id ?? '')
      }
    } catch (error) {
      set({ error: errorMessage(error) })
    } finally {
      set({ busy: false })
    }
  },
}))
