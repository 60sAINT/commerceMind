import { memo, useCallback, useDeferredValue, useEffect, useRef, useState } from 'react'
import { Check, Copy, Pencil, RotateCcw } from 'lucide-react'

import type { ChatMessage } from '@/stores/chat'
import { Markdown } from './Markdown'

interface MessageItemProps {
  message: ChatMessage
  onRetry: (messageId: string) => void
  onEdit: (message: ChatMessage) => void
  onStop: () => void
}

function CopyButton({ text, label = '复制' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<number | null>(null)

  useEffect(
    () => () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    },
    [],
  )

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = window.setTimeout(() => setCopied(false), 1600)
    } catch {
      // 无剪贴板权限时保持静默。
    }
  }, [text])

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={copied ? '已复制' : label}
      className="inline-flex items-center gap-1 rounded px-1 py-0.5 transition-colors hover:bg-hover hover:text-ink"
    >
      {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
      {copied ? '已复制' : label}
    </button>
  )
}

function MessageItemInner({ message, onRetry, onEdit, onStop }: MessageItemProps) {
  const isUser = message.role === 'user'
  const pending = message.status === 'pending'
  const failed = message.status === 'error'
  // 流式阶段把 Markdown 解析放到低优先级，避免长回答拖慢输入。
  const deferredContent = useDeferredValue(message.content)
  const content = pending ? deferredContent : message.content

  return (
    <article
      className={`flex gap-2.5 cm-fade-up md:gap-3 ${isUser ? 'justify-end' : 'justify-start'}`}
    >
      {!isUser && (
        <div className="grid size-7 shrink-0 place-items-center rounded-md bg-ink text-[11px] font-semibold text-canvas md:size-8 md:text-xs">
          C
        </div>
      )}

      <div className={`min-w-0 max-w-[88%] md:max-w-[80%] ${isUser ? 'order-first' : ''}`}>
        <div
          className={
            isUser
              ? 'whitespace-pre-wrap break-words rounded-lg bg-ink px-3.5 py-2.5 text-sm leading-7 text-canvas md:px-4 md:py-3'
              : 'break-words rounded-lg border border-line bg-elevated px-3.5 py-2.5 text-sm leading-7 md:px-4 md:py-3'
          }
        >
          {isUser ? (
            content
          ) : content ? (
            <>
              <Markdown content={content} />
              {pending && <span className="cm-caret" aria-hidden="true" />}
            </>
          ) : (
            <span className="inline-flex items-center gap-1 text-muted" aria-label="正在思考">
              <span className="cm-dot" />
              <span className="cm-dot" />
              <span className="cm-dot" />
            </span>
          )}
        </div>

        {!isUser && (
          <div className="mt-1 flex min-h-6 flex-wrap items-center gap-3 text-[11px] text-faint">
            {message.interrupted && <span>已中断</span>}
            {message.content.trim() && <CopyButton text={message.content} />}
            {pending && (
              <span className="inline-flex items-center gap-1.5">
                <span className="cm-breathe">正在生成</span>
                <button type="button" onClick={onStop} className="underline hover:text-ink">
                  停止
                </button>
              </span>
            )}
          </div>
        )}

        {isUser && pending && (
          <div className="mt-1 flex items-center justify-end gap-3 text-[11px] text-faint">
            <span className="cm-breathe">已发送</span>
            <button
              type="button"
              onClick={() => {
                onStop()
                onEdit(message)
              }}
              className="inline-flex items-center gap-1 underline hover:text-ink"
            >
              <Pencil className="size-3" />
              编辑
            </button>
          </div>
        )}

        {isUser && failed && (
          <div className="mt-1 flex flex-wrap items-center justify-end gap-3 text-[11px] text-danger">
            <span>{message.error}</span>
            {message.retryable && (
              <button
                type="button"
                onClick={() => onRetry(message.id)}
                className="inline-flex items-center gap-1 underline hover:opacity-80"
              >
                <RotateCcw className="size-3" />
                重试
              </button>
            )}
            <button
              type="button"
              onClick={() => onEdit(message)}
              className="inline-flex items-center gap-1 underline hover:opacity-80"
            >
              <Pencil className="size-3" />
              编辑
            </button>
          </div>
        )}
      </div>

      {isUser && (
        <div className="grid size-7 shrink-0 place-items-center rounded-md border border-line bg-elevated text-[11px] font-semibold md:size-8 md:text-xs">
          我
        </div>
      )}
    </article>
  )
}

/** 消息对象在 store 中保持引用稳定，memo 能让流式更新只重绘当前这一条。 */
export const MessageItem = memo(MessageItemInner)
