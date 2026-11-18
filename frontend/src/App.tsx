import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, ArrowDown, RefreshCw } from 'lucide-react'

import { Composer } from '@/components/chat/Composer'
import { EmptyState } from '@/components/chat/EmptyState'
import { MessageItem } from '@/components/chat/MessageItem'
import { Sidebar } from '@/components/layout/Sidebar'
import { TopBar } from '@/components/layout/TopBar'
import { SettingsPanel } from '@/components/panels/SettingsPanel'
import { SkillsPanel } from '@/components/panels/SkillsPanel'
import { useSmartScroll } from '@/hooks/useSmartScroll'
import { healthProblems, useHealthStore } from '@/stores/health'
import { useChatStore, type ChatMessage } from '@/stores/chat'
import { useHistoryStore } from '@/stores/history'
import { useSessionStore } from '@/stores/session'

/** 稳定引用，避免 messagesBySession 缺省时每次渲染都生成新数组。 */
const EMPTY_MESSAGES: ChatMessage[] = []

type Panel = 'skills' | 'settings' | null

function ThinkingRow({ onStop }: { onStop: () => void }) {
  return (
    <article className="flex gap-2.5 cm-fade-up md:gap-3" aria-live="polite">
      <div className="grid size-7 shrink-0 place-items-center rounded-md bg-ink text-[11px] font-semibold text-canvas md:size-8 md:text-xs">
        C
      </div>
      <div className="rounded-lg border border-line bg-elevated px-3.5 py-2.5 md:px-4 md:py-3">
        <div className="flex items-center gap-2.5 text-xs text-muted">
          <span className="flex items-center gap-1 text-ink" aria-hidden="true">
            <span className="cm-dot" />
            <span className="cm-dot" />
            <span className="cm-dot" />
          </span>
          <span>正在思考</span>
          <button type="button" onClick={onStop} className="underline hover:text-ink">
            停止
          </button>
        </div>
      </div>
    </article>
  )
}

export default function App() {
  const userId = useSessionStore((state) => state.userId)
  const sessionId = useSessionStore((state) => state.sessionId)
  const theme = useSessionStore((state) => state.theme)
  const setSessionId = useSessionStore((state) => state.setSessionId)
  const setUserId = useSessionStore((state) => state.setUserId)
  const setTheme = useSessionStore((state) => state.setTheme)

  const messagesBySession = useChatStore((state) => state.messagesBySession)
  const sendingSessionId = useChatStore((state) => state.sendingSessionId)
  const send = useChatStore((state) => state.send)
  const stop = useChatStore((state) => state.stop)
  const retry = useChatStore((state) => state.retry)
  const editAndResend = useChatStore((state) => state.editAndResend)
  const cancelForSessionSwitch = useChatStore((state) => state.cancelForSessionSwitch)

  const history = useHistoryStore()
  const historyReady = history.owner === userId && history.ready
  const messages = historyReady ? (messagesBySession[sessionId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES
  const sending = sendingSessionId === sessionId

  const health = useHealthStore()
  const checkHealth = useHealthStore((state) => state.check)
  const problems = useMemo(() => healthProblems(health), [health])

  const [draft, setDraft] = useState('')
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [panel, setPanel] = useState<Panel>(null)

  const { scrollRef, atBottom, handleScroll, scrollToBottom } = useSmartScroll(sessionId, messages)

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    document.documentElement.style.colorScheme = theme
    localStorage.setItem('commercemind.theme', theme)
  }, [theme])

  // 首次检测 + 30s 轮询 + 窗口回到前台时补检。
  useEffect(() => {
    void checkHealth()
    const timer = window.setInterval(() => void checkHealth(), 30_000)
    const onFocus = () => void checkHealth()
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onFocus)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onFocus)
    }
  }, [checkHealth])

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

  // 编辑态按 Esc 退出，不发送任何请求。
  useEffect(() => {
    if (!editingMessageId) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setEditingMessageId(null)
      setDraft('')
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [editingMessageId])

  const switchSession = useCallback(
    (id: string) => {
      setSidebarOpen(false)
      if (id === sessionId) return
      cancelForSessionSwitch()
      setEditingMessageId(null)
      setDraft('')
      setSessionId(id)
    },
    [cancelForSessionSwitch, sessionId, setSessionId],
  )

  const startNewSession = useCallback(() => {
    setSidebarOpen(false)
    setEditingMessageId(null)
    setDraft('')
    void history.create(userId)
  }, [history, userId])

  const deleteSession = useCallback(
    (id: string) => {
      setEditingMessageId(null)
      setDraft('')
      void history.remove(userId, id)
    },
    [history, userId],
  )

  const beginEdit = useCallback((message: ChatMessage) => {
    setEditingMessageId(message.id)
    setDraft(message.content)
    requestAnimationFrame(() => document.getElementById('composer-input')?.focus())
  }, [])

  const handleRetry = useCallback(
    (messageId: string) => {
      void retry(messageId, sessionId, userId)
    },
    [retry, sessionId, userId],
  )

  const changeUserId = useCallback(
    (next: string) => {
      if (!next || next === userId) return
      cancelForSessionSwitch()
      setEditingMessageId(null)
      setDraft('')
      setSessionId('')
      setUserId(next)
      setPanel(null)
    },
    [cancelForSessionSwitch, setSessionId, setUserId, userId],
  )

  const blocked =
    !historyReady || history.busy || history.loading || history.loadingMessages

  const sendText = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || sending || blocked) return
      let targetSessionId = sessionId
      if (!targetSessionId) {
        await history.create(userId)
        targetSessionId = useSessionStore.getState().sessionId
        if (!targetSessionId || useSessionStore.getState().userId !== userId) return
      }
      void send(trimmed, targetSessionId, userId)
    },
    [blocked, history, send, sending, sessionId, userId],
  )

  const submit = useCallback(() => {
    const text = draft.trim()
    if (!text || blocked || sending) return
    setDraft('')
    if (editingMessageId) {
      if (!sessionId) return
      void editAndResend(editingMessageId, text, sessionId, userId)
      setEditingMessageId(null)
      return
    }
    void sendText(text)
  }, [blocked, draft, editAndResend, editingMessageId, sendText, sending, sessionId, userId])

  const askQuickQuestion = useCallback(
    (question: string) => {
      setDraft('')
      setEditingMessageId(null)
      void sendText(question)
    },
    [sendText],
  )

  const firstUserMessage = messages.find((message) => message.role === 'user')
  const title = firstUserMessage?.content.slice(0, 60) || '新会话'
  const subtitle = sessionId ? `会话 ${sessionId.slice(0, 18)}` : '尚未发送消息'
  const lastMessage = messages[messages.length - 1]
  const thinking = sending && (!lastMessage || lastMessage.role === 'user')

  return (
    <div className="flex h-dvh bg-canvas text-ink">
      <Sidebar
        userId={userId}
        sessionId={sessionId}
        sessions={historyReady ? history.sessions : []}
        historyReady={historyReady}
        historyBusy={history.busy}
        historyLoading={history.loading}
        onNewSession={startNewSession}
        onSelectSession={switchSession}
        onOpenSkills={() => {
          setSidebarOpen(false)
          setPanel('skills')
        }}
        onOpenSettings={() => {
          setSidebarOpen(false)
          setPanel('settings')
        }}
      />

      {sidebarOpen && (
        <Sidebar
          variant="drawer"
          onClose={() => setSidebarOpen(false)}
          userId={userId}
          sessionId={sessionId}
          sessions={historyReady ? history.sessions : []}
          historyReady={historyReady}
          historyBusy={history.busy}
          historyLoading={history.loading}
          onNewSession={startNewSession}
          onSelectSession={switchSession}
          onOpenSkills={() => {
            setSidebarOpen(false)
            setPanel('skills')
          }}
          onOpenSettings={() => {
            setSidebarOpen(false)
            setPanel('settings')
          }}
        />
      )}

      <main className="flex min-w-0 flex-1 flex-col">
        <TopBar
          title={title}
          subtitle={subtitle}
          theme={theme}
          canDelete={Boolean(sessionId) && historyReady && !history.busy}
          onToggleTheme={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
          onDelete={() => deleteSession(sessionId)}
          onOpenSidebar={() => setSidebarOpen(true)}
        />

        {problems.length > 0 && (
          <div
            role="alert"
            className="flex items-start gap-2 border-b border-warning/25 bg-warning/5 px-3 py-2 text-[11px] leading-5 text-warning md:px-6 xl:px-8"
          >
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span className="min-w-0 flex-1">{problems.join(' · ')}</span>
            <button
              type="button"
              onClick={() => void checkHealth()}
              disabled={health.checking}
              className="inline-flex shrink-0 items-center gap-1 underline disabled:opacity-50"
            >
              <RefreshCw className={`size-3 ${health.checking ? 'cm-spin' : ''}`} />
              重新检测
            </button>
          </div>
        )}

        <div className="relative min-h-0 flex-1">
          <section
            ref={scrollRef}
            onScroll={handleScroll}
            className="h-full overflow-y-auto px-3 py-6 md:px-6 md:py-8 xl:px-10"
            aria-label="消息列表"
          >
            <div className="mx-auto flex min-h-full max-w-3xl flex-col gap-5 xl:max-w-4xl">
              {history.error && (
                <div role="alert" className="text-xs text-danger">
                  {history.error}
                  <button
                    type="button"
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
                <p role="status" className="text-[11px] text-muted">
                  正在加载消息…
                </p>
              )}

              {historyReady && history.messageCursor !== null && !history.loadingMessages && (
                <button
                  type="button"
                  className="self-center rounded-full border border-line px-3 py-1 text-[11px] text-muted hover:bg-hover hover:text-ink"
                  onClick={() => void history.loadMessages(userId, sessionId, true)}
                >
                  加载更早消息
                </button>
              )}

              {!historyReady && !history.error && (
                <p className="m-auto text-xs text-muted" role="status">
                  正在同步会话…
                </p>
              )}

              {historyReady && messages.length === 0 && !thinking && (
                <EmptyState onAsk={askQuickQuestion} disabled={blocked} />
              )}

              {messages.map((message) => (
                <MessageItem
                  key={message.id}
                  message={message}
                  onRetry={handleRetry}
                  onEdit={beginEdit}
                  onStop={stop}
                />
              ))}

              {thinking && <ThinkingRow onStop={stop} />}
            </div>
          </section>

          {!atBottom && messages.length > 0 && (
            <button
              type="button"
              onClick={() => scrollToBottom(true)}
              className="absolute bottom-3 left-1/2 inline-flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-line bg-elevated px-3 py-1.5 text-[11px] text-muted shadow-soft transition-colors hover:text-ink"
            >
              <ArrowDown className="size-3" />
              回到底部
            </button>
          )}
        </div>

        <div className="shrink-0 px-3 pb-3 pt-2 md:px-6 md:pb-5 xl:px-10">
          <Composer
            value={draft}
            onChange={setDraft}
            onSubmit={submit}
            onStop={stop}
            sending={sending}
            disabled={blocked}
            editing={Boolean(editingMessageId)}
          />
        </div>
      </main>

      {panel === 'skills' && (
        <SkillsPanel open onClose={() => setPanel(null)} />
      )}
      {panel === 'settings' && (
        <SettingsPanel
          open
          onClose={() => setPanel(null)}
          userId={userId}
          onChangeUserId={changeUserId}
        />
      )}
    </div>
  )
}
