import { useCallback, useEffect, useRef, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ArrowUp, Square } from 'lucide-react'

export const MAX_MESSAGE_LENGTH = 12_000

interface ComposerProps {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  onStop: () => void
  sending: boolean
  disabled: boolean
  editing: boolean
}

export function Composer({
  value,
  onChange,
  onSubmit,
  onStop,
  sending,
  disabled,
  editing,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // 自动增高：先归零再按 scrollHeight 计算，避免删除内容后高度不回落。
  useEffect(() => {
    const element = textareaRef.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`
  }, [value])

  const submit = useCallback(() => {
    if (sending || disabled || !value.trim()) return
    textareaRef.current?.form?.requestSubmit()
  }, [disabled, sending, value])

  const handleKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key !== 'Enter') return
      // 中文输入法组合期间的回车只用于选词，不能触发发送。
      if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return

      if (event.metaKey || event.ctrlKey) {
        event.preventDefault()
        submit()
        return
      }
      if (!event.shiftKey) {
        event.preventDefault()
        submit()
      }
    },
    [submit],
  )

  const nearLimit = value.length >= MAX_MESSAGE_LENGTH * 0.9
  const sendDisabled = !value.trim() || disabled || sending

  return (
    <div className="mx-auto w-full max-w-3xl xl:max-w-4xl">
      <form
        onSubmit={(event) => {
          event.preventDefault()
          onSubmit()
        }}
        className="flex items-end gap-2 rounded-xl border border-line bg-elevated p-2 shadow-soft transition-colors focus-within:border-line-strong"
      >
        <textarea
          ref={textareaRef}
          id="composer-input"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
          maxLength={MAX_MESSAGE_LENGTH}
          placeholder="输入消息…（Enter 发送，Shift+Enter 换行）"
          aria-label="消息输入框"
          className="max-h-[200px] min-h-11 flex-1 resize-none overflow-y-auto bg-transparent px-3 py-2.5 text-sm leading-6 outline-none placeholder:text-faint"
        />
        {sending ? (
          <button
            type="button"
            title="中断生成"
            aria-label="中断生成"
            onClick={onStop}
            className="grid size-10 shrink-0 place-items-center rounded-lg bg-danger text-white transition-opacity hover:opacity-90"
          >
            <Square className="size-3.5" />
          </button>
        ) : (
          <button
            type="submit"
            title={editing ? '编辑并重新发送' : '发送消息'}
            aria-label={editing ? '编辑并重新发送' : '发送消息'}
            disabled={sendDisabled}
            className="grid size-10 shrink-0 place-items-center rounded-lg bg-ink text-canvas transition-opacity hover:opacity-90 disabled:opacity-35"
          >
            <ArrowUp className="size-4" />
          </button>
        )}
      </form>

      <div className="mt-2 flex items-center justify-between gap-3 px-1 text-[10px] text-faint">
        <span className="truncate">
          {editing ? '正在编辑上一条消息 · Esc 取消' : '回复内容由 AI 生成，请核实重要信息'}
        </span>
        <span className="flex shrink-0 items-center gap-3">
          <span className="hidden md:inline">Enter 发送 · Shift+Enter 换行</span>
          {nearLimit && (
            <span className={value.length >= MAX_MESSAGE_LENGTH ? 'text-danger' : 'text-warning'}>
              {value.length} / {MAX_MESSAGE_LENGTH}
            </span>
          )}
        </span>
      </div>
    </div>
  )
}
