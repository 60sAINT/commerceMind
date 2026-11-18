import { useEffect, type ReactNode } from 'react'
import { X } from 'lucide-react'

interface SheetProps {
  open: boolean
  onClose: () => void
  title: string
  description?: string
  side?: 'left' | 'right'
  width?: 'md' | 'lg'
  children: ReactNode
}

/**
 * 通用抽屉：桌面端从右侧滑出，移动端铺满可视宽度。
 * 只在 open 时挂载，关闭后不保留 DOM。
 */
export function Sheet({
  open,
  onClose,
  title,
  description,
  side = 'right',
  width = 'md',
  children,
}: SheetProps) {
  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    window.addEventListener('keydown', onKeyDown)
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex" role="presentation">
      <button
        type="button"
        aria-label="关闭面板"
        onClick={onClose}
        className="absolute inset-0 bg-black/35 backdrop-blur-[1px]"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`relative z-10 flex h-full w-full flex-col border-line bg-canvas shadow-float ${
          side === 'right'
            ? 'ml-auto border-l cm-slide-in-right'
            : 'mr-auto border-r cm-slide-in-left'
        } ${width === 'lg' ? 'sm:max-w-lg' : 'sm:max-w-md'}`}
      >
        <header className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold">{title}</h2>
            {description && <p className="mt-1 text-xs text-muted">{description}</p>}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="grid size-8 shrink-0 place-items-center rounded-md text-muted hover:bg-hover"
          >
            <X className="size-4" />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  )
}
