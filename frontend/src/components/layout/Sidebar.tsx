import { MessageSquarePlus, Sparkles, Wifi, X } from 'lucide-react'

import { modelLabel, useHealthStore, type ConnectionState } from '@/stores/health'
import type { SessionSummary } from '@/types/api'

interface SidebarProps {
  userId: string
  sessionId: string
  sessions: SessionSummary[]
  historyReady: boolean
  historyBusy: boolean
  historyLoading: boolean
  onNewSession: () => void
  onSelectSession: (sessionId: string) => void
  onOpenSkills: () => void
  onOpenSettings: () => void
  variant?: 'desktop' | 'drawer'
  onClose?: () => void
}

const CONNECTION_LABEL: Record<ConnectionState, string> = {
  loading: '检测中',
  ok: '在线',
  degraded: '服务降级',
  offline: '离线',
}

const CONNECTION_DOT: Record<ConnectionState, string> = {
  loading: 'bg-faint cm-breathe',
  ok: 'bg-success',
  degraded: 'bg-warning',
  offline: 'bg-danger',
}

function SidebarContent({
  userId,
  sessionId,
  sessions,
  historyReady,
  historyBusy,
  historyLoading,
  onNewSession,
  onSelectSession,
  onOpenSkills,
  onOpenSettings,
  onClose,
}: Omit<SidebarProps, 'variant'>) {
  const connection = useHealthStore((state) => state.connection)
  const health = useHealthStore()

  return (
    <div className="flex h-full w-full flex-col border-r border-line bg-sidebar">
      <div className="flex h-16 items-center gap-3 border-b border-line px-4 xl:px-5">
        <div className="grid size-8 shrink-0 place-items-center rounded-lg bg-ink text-sm font-bold text-canvas">
          C
        </div>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold tracking-tight">CommerceMind</p>
          <p className="truncate text-[10px] text-faint">电商客服智能助手</p>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭侧栏"
            className="grid size-8 place-items-center rounded-md text-muted hover:bg-hover lg:hidden"
          >
            <X className="size-4" />
          </button>
        )}
      </div>

      <div className="p-3">
        <button
          type="button"
          onClick={onNewSession}
          disabled={!historyReady || historyBusy || historyLoading}
          className="flex h-10 w-full items-center gap-2 rounded-lg border border-line bg-elevated px-3 text-xs font-medium transition-colors hover:bg-hover disabled:opacity-50"
        >
          <MessageSquarePlus className="size-4" />
          新建会话
        </button>
      </div>

      <nav className="grid gap-1 px-2 pb-2" aria-label="功能入口">
        <button
          type="button"
          onClick={onOpenSkills}
          className="flex items-center gap-2 rounded-md px-3 py-2 text-xs text-muted transition-colors hover:bg-hover hover:text-ink"
        >
          <Sparkles className="size-4" />
          技能
        </button>
      </nav>

      <div className="px-4 pb-2 pt-1 text-[10px] font-medium uppercase tracking-wider text-faint">
        最近会话
      </div>
      <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-2" aria-label="最近会话">
        {(historyReady ? sessions : []).map((session) => (
          <button
            key={session.session_id}
            type="button"
            disabled={historyBusy}
            onClick={() => onSelectSession(session.session_id)}
            className={`mb-0.5 flex w-full items-center gap-2 rounded-md px-3 py-2.5 text-left text-xs transition-colors ${
              session.session_id === sessionId
                ? 'bg-hover text-ink'
                : 'text-muted hover:bg-hover hover:text-ink'
            }`}
          >
            <span className="size-1.5 shrink-0 rounded-full bg-success" />
            <span className="truncate">{session.title || '新会话'}</span>
          </button>
        ))}
        {historyLoading && <p className="p-3 text-xs text-muted">正在同步会话…</p>}
        {historyReady && sessions.length === 0 && !historyLoading && (
          <p className="px-3 py-2 text-[11px] leading-5 text-faint">还没有会话，发送第一条消息即可开始。</p>
        )}
      </nav>

      <div className="border-t border-line p-3">
        <button
          type="button"
          onClick={onOpenSettings}
          className="flex w-full items-center gap-2.5 rounded-lg px-2 py-2 text-left transition-colors hover:bg-hover"
        >
          <span className="grid size-7 shrink-0 place-items-center rounded-full border border-line bg-elevated text-[10px] font-semibold">
            我
          </span>
          <span className="min-w-0 flex-1">
            <span className="block truncate text-xs font-medium">{userId}</span>
            <span className="block truncate text-[10px] text-faint">点击打开设置</span>
          </span>
        </button>
        <div
          className="mt-1 flex items-center gap-2 px-2 py-1 text-[10px] text-muted"
          title={health.error ?? undefined}
        >
          <Wifi className="size-3" />
          <span className={`size-1.5 rounded-full ${CONNECTION_DOT[connection]}`} />
          <span>后端{CONNECTION_LABEL[connection]}</span>
          <span className="ml-auto max-w-24 truncate text-faint">{modelLabel(health)}</span>
        </div>
      </div>
    </div>
  )
}

export function Sidebar(props: SidebarProps) {
  if (props.variant === 'drawer') {
    return (
      <div className="fixed inset-0 z-50 flex lg:hidden" role="presentation">
        <button
          type="button"
          aria-label="关闭侧栏"
          onClick={props.onClose}
          className="absolute inset-0 bg-black/35 backdrop-blur-[1px]"
        />
        <div className="relative z-10 h-full w-72 max-w-[85vw] cm-slide-in-left">
          <SidebarContent {...props} />
        </div>
      </div>
    )
  }

  return (
    <aside className="hidden w-64 shrink-0 lg:flex xl:w-72">
      <SidebarContent {...props} />
    </aside>
  )
}
