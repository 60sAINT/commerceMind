import { useEffect, useRef, useState } from 'react'
import { create } from 'zustand'
import { MessageSquarePlus, Pencil, Moon, Send, Square, Sun, Trash2, Wifi } from 'lucide-react'

import { getHealth } from '@/services'
import { useChatStore, type ChatMessage } from '@/stores/chat'
import { useSessionStore } from '@/stores/session'
import { useHistoryStore } from '@/stores/history'

const useConnectionStore = create<{ label: string; check: () => Promise<void> }>((set) => ({
  label: '连接中',
  check: async () => {
    try {
      const health = await getHealth({ timeoutMs: 8000 })
      set({ label: health.status === 'ok' ? '在线' : '服务降级' })
    } catch {
      set({ label: '离线' })
    }
  },
}))

/** 稳定引用，避免 messagesBySession 缺省时每次渲染都生成新数组。 */
const EMPTY_MESSAGES: ChatMessage[] = []

export default function App() {
  const userId = useSessionStore((state) => state.userId)
  const sessionId = useSessionStore((state) => state.sessionId)
  const theme = useSessionStore((state) => state.theme)
  const setSessionId = useSessionStore((state) => state.setSessionId)
  const setUserId = useSessionStore((state) => state.setUserId)
  const setTheme = useSessionStore((state) => state.setTheme)
  const messagesBySession = useChatStore((state) => state.messagesBySession)
  const history = useHistoryStore()
  const historyReady = history.owner === userId && history.ready
  const messages = historyReady ? (messagesBySession[sessionId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES
  const sendingSessionId = useChatStore((state) => state.sendingSessionId)
  const sending = useChatStore((state) => state.sendingSessionId === sessionId)
  const send = useChatStore((state) => state.send)
  const stop = useChatStore((state) => state.stop)
  const retry = useChatStore((state) => state.retry)
  const editAndResend = useChatStore((state) => state.editAndResend)
  const cancelForSessionSwitch = useChatStore((state) => state.cancelForSessionSwitch)
  const connection = useConnectionStore((state) => state.label)
  const checkConnection = useConnectionStore((state) => state.check)
  const [draft, setDraft] = useState('')
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    document.documentElement.style.colorScheme = theme
    localStorage.setItem('commercemind.theme', theme)
  }, [theme])

  useEffect(() => {
    void checkConnection()
  }, [checkConnection])

  useEffect(() => {
    if (sendingSessionId === null) void useHistoryStore.getState().loadSessions(userId)
  }, [userId, sendingSessionId])

  useEffect(() => {
    if (historyReady) void useHistoryStore.getState().loadMessages(userId, sessionId)
  }, [historyReady, userId, sessionId, sendingSessionId])

  useEffect(() => {
    const abortActiveRequest = () => useChatStore.getState().stop()
    window.addEventListener('pagehide', abortActiveRequest)
    return () => window.removeEventListener('pagehide', abortActiveRequest)
  }, [])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
  }, [messages])

  function switchSession(id: string) {
    if (id === sessionId) return
    cancelForSessionSwitch()
    setEditingMessageId(null)
    setDraft('')
    setSessionId(id)
  }

  function startNewSession() {
    setEditingMessageId(null)
    setDraft('')
    void history.create(userId)
  }

  function deleteSession(id: string) {
    setEditingMessageId(null)
    setDraft('')
    void history.remove(userId, id)
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (
      !draft.trim() ||
      sending ||
      history.busy ||
      history.loading ||
      history.loadingMessages ||
      !historyReady
    )
      return
    let targetSessionId = sessionId
    if (!targetSessionId) {
      await history.create(userId)
      targetSessionId = useSessionStore.getState().sessionId
      if (!targetSessionId || useSessionStore.getState().userId !== userId) return
    }
    const text = draft
    setDraft('')
    if (editingMessageId) {
      void editAndResend(editingMessageId, text, targetSessionId, userId)
      setEditingMessageId(null)
      return
    }
    void send(text, targetSessionId, userId)
  }

  return (
    <div className="flex h-dvh bg-canvas text-ink">
      <aside className="flex w-64 shrink-0 flex-col border-r border-line bg-sidebar max-sm:hidden">
        <div className="flex h-16 items-center gap-3 border-b border-line px-5">
          <div className="grid size-8 place-items-center rounded-md bg-ink text-sm font-bold text-canvas">
            C
          </div>
          <span className="font-semibold">CommerceMind</span>
        </div>
        <div className="p-3">
          <button
            onClick={startNewSession}
            disabled={!historyReady || history.busy || history.loading}
            className="flex h-10 w-full items-center gap-2 rounded-md border border-line px-3 text-sm hover:bg-hover"
          >
            <MessageSquarePlus className="size-4" /> 新建会话
          </button>
        </div>
        <div className="px-5 pb-2 pt-3 text-[11px] font-medium text-faint">最近会话</div>
        <nav className="min-h-0 flex-1 overflow-y-auto px-2">
          {(historyReady ? history.sessions : []).map((session) => (
            <button
              key={session.session_id}
              disabled={history.busy}
              onClick={() => switchSession(session.session_id)}
              className={`mb-1 flex w-full items-center gap-2 rounded-md px-3 py-2.5 text-left text-xs ${session.session_id === sessionId ? 'bg-hover text-ink' : 'text-muted hover:bg-hover'}`}
            >
              <span className="size-1.5 shrink-0 rounded-full bg-success" />
              <span className="truncate">{session.title || '新会话'}</span>
            </button>
          ))}
          {history.loading && <p className="p-3 text-xs text-muted">正在同步会话…</p>}
        </nav>
        <div className="border-t border-line p-4">
          <label className="mb-1.5 block text-[11px] text-faint" htmlFor="user-id">
            用户 ID
          </label>
          <input
            id="user-id"
            value={userId}
            onChange={(event) => {
              cancelForSessionSwitch()
              setEditingMessageId(null)
              setDraft('')
              setSessionId('')
              setUserId(event.target.value)
            }}
            className="h-8 w-full rounded-md border border-line bg-canvas px-2 text-xs outline-none focus:border-line-strong"
          />
          <div className="mt-3 flex items-center gap-2 text-[11px] text-muted">
            <Wifi className="size-3.5" />
            后端{connection}
          </div>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-16 shrink-0 items-center justify-between border-b border-line px-5 sm:px-8">
          <div className="flex min-w-0 items-center gap-2">
            <button
              onClick={startNewSession}
              disabled={!historyReady || history.busy || history.loading}
              title="新建会话"
              aria-label="新建会话"
              className="grid size-9 shrink-0 place-items-center rounded-md text-muted hover:bg-hover sm:hidden"
            >
              <MessageSquarePlus className="size-4" />
            </button>
            <div className="min-w-0">
              <h1 className="truncate text-sm font-semibold">
                {messages.find((item) => item.role === 'user')?.content ?? '新会话'}
              </h1>
              <p className="mt-0.5 text-[11px] text-faint">
                {sessionId ? `会话 ${sessionId.slice(0, 18)}` : '尚未发送消息'}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              title="删除当前会话"
              aria-label="删除当前会话"
              onClick={() => deleteSession(sessionId)}
              disabled={!sessionId || !historyReady || history.busy}
              className="grid size-9 place-items-center rounded-md text-muted hover:bg-hover disabled:opacity-40"
            >
              <Trash2 className="size-4" />
            </button>
            <button
              title={theme === 'dark' ? '浅色主题' : '深色主题'}
              aria-label={theme === 'dark' ? '切换浅色主题' : '切换深色主题'}
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              className="grid size-9 place-items-center rounded-md text-muted hover:bg-hover"
            >
              {theme === 'dark' ? <Sun className="size-4" /> : <Moon className="size-4" />}
            </button>
          </div>
        </header>

        <section className="flex-1 overflow-y-auto px-4 py-8 sm:px-8">
          <div className="mx-auto flex min-h-full max-w-3xl flex-col gap-6">
            {history.error && (
              <div role="alert" className="text-sm text-danger">
                {history.error}
                <button
                  className="ml-3 underline"
                  onClick={() => {
                    void history.loadSessions(userId)
                    if (historyReady) void history.loadMessages(userId, sessionId)
                  }}
                >
                  重新加载
                </button>
              </div>
            )}
            {history.loadingMessages && (
              <p role="status" className="text-xs text-muted">
                正在加载消息…
              </p>
            )}
            {historyReady && history.messageCursor !== null && !history.loadingMessages && (
              <button
                className="text-xs text-muted underline"
                onClick={() => void history.loadMessages(userId, sessionId, true)}
              >
                加载更早消息
              </button>
            )}
            {messages.length === 0 ? (
              <div className="m-auto max-w-lg py-16 text-center">
                <div className="mx-auto grid size-12 place-items-center rounded-lg border border-line bg-elevated">
                  <MessageSquarePlus className="size-5 text-muted" />
                </div>
                <h2 className="mt-5 text-xl font-semibold">有什么可以帮你？</h2>
                <p className="mt-2 text-sm text-muted">
                  描述订单、商品或售后问题，CommerceMind 会为你提供协助。
                </p>
              </div>
            ) : (
              messages.map((message) => (
                <article
                  key={message.id}
                  className={`flex gap-3 ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
                >
                  {message.role === 'assistant' && (
                    <div className="grid size-8 shrink-0 place-items-center rounded-md bg-ink text-xs font-semibold text-canvas">
                      C
                    </div>
                  )}
                  <div
                    className={`max-w-[85%] min-w-0 ${message.role === 'user' ? 'order-first' : ''}`}
                  >
                    <div
                      className={`whitespace-pre-wrap break-words rounded-lg px-4 py-3 text-sm leading-7 ${message.role === 'user' ? 'bg-ink text-canvas' : 'border border-line bg-elevated'}`}
                    >
                      {message.content}
                    </div>
                    {message.role === 'assistant' && message.interrupted && (
                      <div className="mt-1 text-[11px] text-faint">已中断</div>
                    )}
                    {message.status === 'pending' && (
                      <div className="mt-1 flex items-center gap-3 text-[11px] text-faint">
                        <span>正在回复…</span>
                        <button
                          onClick={() => {
                            setEditingMessageId(message.id)
                            stop()
                            setDraft(message.content)
                          }}
                          className="inline-flex items-center gap-1 underline"
                        >
                          <Pencil className="size-3" />
                          编辑
                        </button>
                      </div>
                    )}
                    {message.status === 'error' && (
                      <div className="mt-1 flex flex-wrap items-center gap-3 text-[11px] text-danger">
                        <span>{message.error}</span>
                        {message.retryable && (
                          <button
                            onClick={() => void retry(message.id, sessionId, userId)}
                            className="underline"
                          >
                            重试
                          </button>
                        )}
                        <button
                          onClick={() => {
                            setEditingMessageId(message.id)
                            setDraft(message.content)
                            requestAnimationFrame(() =>
                              document.querySelector<HTMLTextAreaElement>('textarea')?.focus(),
                            )
                          }}
                          className="inline-flex items-center gap-1 underline"
                        >
                          <Pencil className="size-3" />
                          编辑
                        </button>
                      </div>
                    )}
                  </div>
                  {message.role === 'user' && (
                    <div className="grid size-8 shrink-0 place-items-center rounded-md border border-line bg-elevated text-xs font-semibold">
                      我
                    </div>
                  )}
                </article>
              ))
            )}
            <div ref={endRef} />
          </div>
        </section>

        <div className="shrink-0 px-4 pb-5 sm:px-8 sm:pb-7">
          <form
            onSubmit={submit}
            className="mx-auto flex max-w-3xl items-end gap-2 rounded-lg border border-line bg-elevated p-2 shadow-soft focus-within:border-line-strong"
          >
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault()
                  event.currentTarget.form?.requestSubmit()
                }
              }}
              rows={2}
              placeholder="输入消息…（Enter 发送，Shift+Enter 换行）"
              className="max-h-40 min-h-12 flex-1 resize-y bg-transparent px-3 py-2 text-sm leading-6 outline-none placeholder:text-faint"
            />
            {sending ? (
              <button
                type="button"
                title="中断生成"
                aria-label="中断生成"
                onClick={stop}
                className="grid size-10 shrink-0 place-items-center rounded-md bg-danger text-white"
              >
                <Square className="size-3.5" />
              </button>
            ) : (
              <button
                type="submit"
                title={editingMessageId ? '编辑并重新发送' : '发送消息'}
                aria-label={editingMessageId ? '编辑并重新发送' : '发送消息'}
                disabled={
                  !draft.trim() ||
                  !historyReady ||
                  history.busy ||
                  history.loading ||
                  history.loadingMessages
                }
                className="grid size-10 shrink-0 place-items-center rounded-md bg-ink text-canvas disabled:opacity-35"
              >
                <Send className="size-4" />
              </button>
            )}
          </form>
          <p className="mx-auto mt-2 max-w-3xl text-center text-[10px] text-faint">
            回复内容由 AI 生成，请核实重要信息
          </p>
        </div>
      </main>
    </div>
  )
}
