import { create } from 'zustand'
import { createJSONStorage, persist } from 'zustand/middleware'

import { isApiError } from '@/api'
import { createRequestId } from '@/lib/id'
import { streamChat } from '@/services/chat'
import { useSessionStore } from '@/stores/session'
import type { ChatRequest } from '@/types/api'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  createdAt: string
  requestId?: string
  status?: 'pending' | 'error'
  error?: string
  retryable?: boolean
  /** 中断时保留的助手片段，仅用于展示，不参与后端上下文。 */
  interrupted?: boolean
}

interface ChatState {
  messagesBySession: Record<string, ChatMessage[]>
  sendingSessionId: string | null
  activeRequestId: string | null
  send: (text: string, sessionId: string, userId: string, skillIds?: string[]) => Promise<void>
  editAndResend: (
    messageId: string,
    text: string,
    sessionId: string,
    userId: string,
  ) => Promise<void>
  retry: (messageId: string, sessionId: string, userId: string) => Promise<void>
  stop: () => void
  clear: (sessionId: string) => void
  remove: (sessionId: string) => void
  cancelForSessionSwitch: () => void
}

let activeController: AbortController | null = null

function messagesFor(state: ChatState, sessionId: string) {
  return state.messagesBySession[sessionId] ?? []
}

function replaceMessages(sessionId: string, messages: ChatMessage[]) {
  useChatStore.setState((state) => ({
    messagesBySession: { ...state.messagesBySession, [sessionId]: messages },
  }))
}

const STREAM_PREFIX = 'stream-'
const PARTIAL_PREFIX = 'partial-'

/**
 * 中断时把仍在流式输出的临时消息转成可保留的历史片段。
 * - 已经产出文字的部分保留下来，标记 interrupted，只用于展示；
 * - 还没产出任何文字的空占位直接丢弃；
 * - 用户消息标记为可重试错误。
 */
function settleInterruptedStream(sessionId: string, errorText: string) {
  const messages = messagesFor(useChatStore.getState(), sessionId)
  const next: ChatMessage[] = []

  for (const message of messages) {
    if (message.id.startsWith(STREAM_PREFIX)) {
      if (message.content.trim()) {
        next.push({
          ...message,
          id: `${PARTIAL_PREFIX}${message.id.slice(STREAM_PREFIX.length)}`,
          status: undefined,
          error: undefined,
          retryable: undefined,
          interrupted: true,
        })
      }
      continue
    }

    next.push(
      message.status === 'pending'
        ? { ...message, status: 'error', error: errorText, retryable: true }
        : message,
    )
  }

  replaceMessages(sessionId, next)
}

/** 重试 / 编辑重发前清掉上一轮的临时片段，避免和新生成的回答重复。 */
function dropStreamArtifacts(sessionId: string, requestIds: Array<string | undefined>) {
  const drop = new Set<string>()
  for (const id of requestIds) {
    if (!id) continue
    drop.add(`${STREAM_PREFIX}${id}`)
    drop.add(`${PARTIAL_PREFIX}${id}`)
  }
  if (drop.size === 0) return

  const messages = messagesFor(useChatStore.getState(), sessionId)
  const next = messages.filter((message) => !drop.has(message.id))
  if (next.length === messages.length) return
  replaceMessages(sessionId, next)
}

function moveSessionMessages(fromSessionId: string, toSessionId: string) {
  if (!toSessionId || fromSessionId === toSessionId) return
  useChatStore.setState((state) => {
    const source = state.messagesBySession[fromSessionId] ?? []
    const target = state.messagesBySession[toSessionId] ?? []
    const remaining = { ...state.messagesBySession }
    delete remaining[fromSessionId]
    return {
      messagesBySession: { ...remaining, [toSessionId]: [...target, ...source] },
      sendingSessionId:
        state.sendingSessionId === fromSessionId ? toSessionId : state.sendingSessionId,
    }
  })
  useSessionStore.getState().setSessionId(toSessionId)
}

async function runRequest(
  messageId: string,
  sessionId: string,
  userId: string,
  requestId: string,
  text: string,
  skillIds?: string[],
) {
  if (useChatStore.getState().sendingSessionId) return
  const controller = new AbortController()
  let activeSessionId = sessionId
  activeController = controller
  useChatStore.setState({ sendingSessionId: sessionId, activeRequestId: requestId })

  try {
    const payload: ChatRequest = {
      message: text,
      user_id: userId,
      session_id: sessionId || null,
      client_request_id: requestId,
      ...(skillIds ? { skill_ids: skillIds } : {}),
    }
    const response = await streamChat(
      payload,
      {
        onMeta: (meta) => {
          if (!sessionId && meta.session_id) {
            moveSessionMessages(activeSessionId, meta.session_id)
            activeSessionId = meta.session_id
            useChatStore.setState({ sendingSessionId: activeSessionId })
          }
        },
        onDelta: (delta) => {
          const state = useChatStore.getState()
          if (state.activeRequestId !== requestId) return
          const current = messagesFor(state, activeSessionId)
          const assistantIndex = current.findIndex((item) => item.id === `stream-${requestId}`)
          const partial: ChatMessage =
            assistantIndex >= 0
              ? { ...current[assistantIndex], content: current[assistantIndex].content + delta }
              : {
                  id: `stream-${requestId}`,
                  role: 'assistant',
                  content: delta,
                  createdAt: new Date().toISOString(),
                  status: 'pending',
                }
          const next = [...current]
          if (assistantIndex >= 0) next[assistantIndex] = partial
          else next.push(partial)
          replaceMessages(activeSessionId, next)
        },
        onDone: (completed) => {
          const state = useChatStore.getState()
          if (state.activeRequestId !== requestId) return
          replaceMessages(
            activeSessionId,
            messagesFor(state, activeSessionId).map((message) => {
              if (message.id === messageId)
                return { ...message, status: undefined, error: undefined }
              if (message.id === `stream-${requestId}`) {
                return {
                  ...message,
                  id: completed.request_id,
                  requestId: completed.request_id,
                  content: completed.response,
                  status: undefined,
                }
              }
              return message
            }),
          )
          if (!activeSessionId && completed.session_id)
            moveSessionMessages(activeSessionId, completed.session_id)
        },
      },
      { signal: controller.signal },
    )
    const state = useChatStore.getState()
    if (state.activeRequestId !== requestId || state.sendingSessionId !== activeSessionId) return

    const current = messagesFor(state, activeSessionId)
      .filter((message) => message.id !== `stream-${requestId}`)
      .map((message) =>
        message.id === messageId ? { ...message, status: undefined, error: undefined } : message,
      )
    if (!current.some((message) => message.id === response.request_id)) {
      current.push({
        id: response.request_id,
        role: 'assistant',
        content: response.response,
        createdAt: new Date().toISOString(),
        requestId: response.request_id,
      })
    }
    replaceMessages(activeSessionId, current)
    if (!activeSessionId && response.session_id)
      moveSessionMessages(activeSessionId, response.session_id)
  } catch (error) {
    const state = useChatStore.getState()
    if (state.activeRequestId !== requestId || state.sendingSessionId !== activeSessionId) return
    if (controller.signal.aborted) return
    const apiError = isApiError(error) ? error : null
    replaceMessages(
      activeSessionId,
      messagesFor(state, activeSessionId).map((message) =>
        message.id === messageId
          ? {
              ...message,
              status: 'error',
              error: apiError?.message ?? '请求失败，请重试',
              retryable: apiError?.retryable ?? true,
            }
          : message,
      ),
    )
  } finally {
    const state = useChatStore.getState()
    if (state.activeRequestId === requestId) {
      activeController = null
      useChatStore.setState({ sendingSessionId: null, activeRequestId: null })
    }
  }
}

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
      messagesBySession: {},
      sendingSessionId: null,
      activeRequestId: null,
      send: async (rawText, sessionId, userId, skillIds) => {
        if (get().sendingSessionId || !rawText.trim()) return
        const text = rawText.trim()
        const requestId = createRequestId()
        const message: ChatMessage = {
          id: requestId,
          role: 'user',
          content: text,
          createdAt: new Date().toISOString(),
          requestId,
          status: 'pending',
        }
        replaceMessages(sessionId, [...messagesFor(get(), sessionId), message])
        await runRequest(requestId, sessionId, userId, requestId, text, skillIds)
      },
      retry: async (messageId, sessionId, userId) => {
        if (get().sendingSessionId) return
        const message = messagesFor(get(), sessionId).find((item) => item.id === messageId)
        if (!message || message.role !== 'user' || message.status !== 'error') return
        const requestId = message.requestId ?? message.id
        dropStreamArtifacts(sessionId, [requestId])
        replaceMessages(
          sessionId,
          messagesFor(get(), sessionId).map((item) =>
            item.id === messageId ? { ...item, status: 'pending', error: undefined } : item,
          ),
        )
        await runRequest(messageId, sessionId, userId, requestId, message.content)
      },
      editAndResend: async (messageId, rawText, sessionId, userId) => {
        if (get().sendingSessionId || !rawText.trim()) return
        const original = messagesFor(get(), sessionId).find((item) => item.id === messageId)
        if (!original || original.role !== 'user' || original.status !== 'error') return

        const text = rawText.trim()
        const previousRequestId = original.requestId ?? original.id
        const requestId = text === original.content ? previousRequestId : createRequestId()
        dropStreamArtifacts(sessionId, [previousRequestId, requestId])

        const messages = messagesFor(get(), sessionId)
        const index = messages.findIndex((item) => item.id === messageId)
        const edited: ChatMessage = {
          ...original,
          id: requestId,
          requestId,
          content: text,
          status: 'pending',
          error: undefined,
          retryable: undefined,
          interrupted: undefined,
          createdAt: new Date().toISOString(),
        }
        replaceMessages(sessionId, [...messages.slice(0, index), edited])
        await runRequest(requestId, sessionId, userId, requestId, text)
      },
      stop: () => {
        const { sendingSessionId } = get()
        activeController?.abort()
        activeController = null
        if (sendingSessionId) settleInterruptedStream(sendingSessionId, '已中断')
        set({ sendingSessionId: null, activeRequestId: null })
      },
      clear: (sessionId) => {
        if (get().sendingSessionId === sessionId) {
          activeController?.abort()
          activeController = null
          set({ sendingSessionId: null, activeRequestId: null })
        }
        replaceMessages(sessionId, [])
      },
      remove: (sessionId) => {
        if (get().sendingSessionId === sessionId) {
          activeController?.abort()
          activeController = null
          set({ sendingSessionId: null, activeRequestId: null })
        }
        // 整条会话从历史中移除，而不是留下一个空数组被渲染成“新会话”。
        set((state) => {
          const remaining = { ...state.messagesBySession }
          delete remaining[sessionId]
          return { messagesBySession: remaining }
        })
      },
      cancelForSessionSwitch: () => {
        const { sendingSessionId } = get()
        activeController?.abort()
        activeController = null
        if (sendingSessionId) settleInterruptedStream(sendingSessionId, '请求已取消，可重试')
        set({ sendingSessionId: null, activeRequestId: null })
      },
    }),
    {
      name: 'commercemind.chat-history',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ messagesBySession: state.messagesBySession }) as ChatState,
    },
  ),
)
