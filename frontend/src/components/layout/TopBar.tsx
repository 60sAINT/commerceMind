import { Menu, Moon, Sun, Trash2 } from 'lucide-react'

import { modelLabel, useHealthStore, type ConnectionState } from '@/stores/health'

interface TopBarProps {
  title: string
  subtitle: string
  theme: 'light' | 'dark'
  canDelete: boolean
  onToggleTheme: () => void
  onDelete: () => void
  onOpenSidebar: () => void
}

const DOT: Record<ConnectionState, string> = {
  loading: 'bg-faint cm-breathe',
  ok: 'bg-success',
  degraded: 'bg-warning',
  offline: 'bg-danger',
}

const TEXT: Record<ConnectionState, string> = {
  loading: 'text-muted',
  ok: 'text-muted',
  degraded: 'text-warning',
  offline: 'text-danger',
}

export function TopBar({
  title,
  subtitle,
  theme,
  canDelete,
  onToggleTheme,
  onDelete,
  onOpenSidebar,
}: TopBarProps) {
  const connection = useHealthStore((state) => state.connection)
  const health = useHealthStore()

  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line px-3 md:h-16 md:px-6 xl:px-8">
      <div className="flex min-w-0 items-center gap-2">
        <button
          type="button"
          onClick={onOpenSidebar}
          title="打开侧栏"
          aria-label="打开侧栏"
          className="grid size-9 shrink-0 place-items-center rounded-md text-muted hover:bg-hover lg:hidden"
        >
          <Menu className="size-4" />
        </button>
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold tracking-tight">{title}</h1>
          <p className="mt-0.5 hidden truncate text-[10px] text-faint xs:block md:text-[11px]">
            {subtitle}
          </p>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-1 md:gap-2">
        <span
          title={`${modelLabel(health)}${health.data?.detail ? ` · ${health.data.detail}` : ''}`}
          className={`inline-flex max-w-[45vw] items-center gap-2 rounded-full border border-line bg-elevated px-2 py-1.5 text-[10px] md:px-3 md:text-[11px] ${TEXT[connection]}`}
        >
          <span className={`size-1.5 shrink-0 rounded-full ${DOT[connection]}`} />
          <span className="truncate">{modelLabel(health)}</span>
        </span>

        <button
          type="button"
          title={theme === 'dark' ? '浅色主题' : '深色主题'}
          aria-label={theme === 'dark' ? '切换浅色主题' : '切换深色主题'}
          onClick={onToggleTheme}
          className="grid size-9 place-items-center rounded-md text-muted hover:bg-hover"
        >
          {theme === 'dark' ? <Sun className="size-4" /> : <Moon className="size-4" />}
        </button>

        <button
          type="button"
          title="删除当前会话"
          aria-label="删除当前会话"
          onClick={onDelete}
          disabled={!canDelete}
          className="grid size-9 place-items-center rounded-md text-muted hover:bg-hover disabled:opacity-40"
        >
          <Trash2 className="size-4" />
        </button>
      </div>
    </header>
  )
}
