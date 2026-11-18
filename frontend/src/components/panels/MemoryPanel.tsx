import { useEffect, useState } from 'react'
import { AlertTriangle, Database, RefreshCw } from 'lucide-react'

import { isApiError } from '@/api'
import { getMemoryDebug } from '@/services'
import type { MemoryDebugResponse } from '@/types/api'
import { Sheet } from '@/components/ui/Sheet'

interface MemoryPanelProps {
  open: boolean
  onClose: () => void
  sessionId: string
  userId: string
}

function errorText(error: unknown): string {
  if (isApiError(error)) {
    return error.status === 404
      ? '当前环境未开启记忆调试接口'
      : error.message
  }
  return error instanceof Error ? error.message : '读取记忆失败'
}

function formatDuration(seconds: number): string {
  if (!seconds) return '0s'
  if (seconds >= 86_400) return `${(seconds / 86_400).toFixed(1)} 天`
  if (seconds >= 3_600) return `${(seconds / 3_600).toFixed(1)} 小时`
  if (seconds >= 60) return `${Math.round(seconds / 60)} 分钟`
  return `${seconds}s`
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

/** 只读记忆抽屉：排查多轮上下文、摘要与 TTL，不触发压缩或写入。 */
export function MemoryPanel({ open, onClose, sessionId, userId }: MemoryPanelProps) {
  const [data, setData] = useState<MemoryDebugResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    if (!open || !sessionId) return
    const controller = new AbortController()
    getMemoryDebug(sessionId, userId, { signal: controller.signal })
      .then((next) => {
        if (controller.signal.aborted) return
        setData(next)
        setError(null)
      })
      .catch((caught) => {
        if (controller.signal.aborted) return
        setData(null)
        setError(errorText(caught))
      })
    return () => controller.abort()
  }, [open, reloadToken, sessionId, userId])

  const fetching = Boolean(sessionId) && !data && !error
  const refresh = () => {
    setData(null)
    setError(null)
    setReloadToken((value) => value + 1)
  }

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="记忆调试"
      description="只读查看当前会话的 Redis working memory、摘要与 TTL。"
      width="lg"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-xs text-muted">
          <Database className="size-4 shrink-0" />
          <span className="truncate font-mono text-[11px]">{sessionId || '当前没有会话'}</span>
        </div>
        <button
          type="button"
          onClick={refresh}
          disabled={fetching || !sessionId}
          className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-line px-2.5 py-1.5 text-[11px] text-muted transition-colors hover:bg-hover hover:text-ink disabled:opacity-50"
        >
          <RefreshCw className={`size-3 ${fetching ? 'cm-spin' : ''}`} />
          刷新
        </button>
      </div>

      {!sessionId && (
        <p className="mt-4 text-xs text-muted">发送一条消息或从侧栏选择会话后再查看。</p>
      )}

      {error && (
        <div className="mt-3 flex items-start gap-2 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] leading-5 text-danger">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {fetching && (
        <div className="mt-4 space-y-2" role="status" aria-label="正在读取记忆">
          {[0, 1, 2].map((item) => (
            <div key={item} className="h-16 animate-pulse rounded-lg border border-line bg-soft" />
          ))}
        </div>
      )}

      {data && (
        <div className="mt-4 space-y-4">
          <dl className="grid grid-cols-2 gap-2 rounded-lg border border-line bg-soft px-3 py-2 text-[10px] text-muted">
            <div>
              <dt className="text-faint">用户</dt>
              <dd className="mt-0.5 truncate text-[11px] text-ink">{data.user_id}</dd>
            </div>
            <div>
              <dt className="text-faint">轮次</dt>
              <dd className="mt-0.5 text-[11px] text-ink">{data.turn_count}</dd>
            </div>
            <div>
              <dt className="text-faint">working TTL</dt>
              <dd className="mt-0.5 text-[11px] text-ink">{formatDuration(data.ttl_s.working)}</dd>
            </div>
            <div>
              <dt className="text-faint">summary TTL</dt>
              <dd className="mt-0.5 text-[11px] text-ink">{formatDuration(data.ttl_s.summary)}</dd>
            </div>
          </dl>

          <section>
            <h3 className="text-[11px] font-medium uppercase tracking-wider text-faint">摘要</h3>
            <p className="mt-1.5 whitespace-pre-wrap rounded-lg border border-line bg-elevated px-3 py-2 text-[11px] leading-5 text-muted">
              {data.summary || '暂无摘要'}
            </p>
          </section>

          <section>
            <h3 className="text-[11px] font-medium uppercase tracking-wider text-faint">状态</h3>
            <pre className="mt-1.5 max-h-48 overflow-auto rounded-lg border border-line bg-elevated px-3 py-2 font-mono text-[10px] leading-5 text-muted">
              {JSON.stringify(data.state, null, 2)}
            </pre>
          </section>

          <section>
            <h3 className="text-[11px] font-medium uppercase tracking-wider text-faint">
              最近轮次
            </h3>
            {data.turns.length === 0 ? (
              <p className="mt-1.5 text-[11px] text-muted">暂无轮次。</p>
            ) : (
              <ul className="mt-1.5 space-y-2">
                {data.turns.map((turn) => (
                  <li key={turn.turn_id} className="rounded-lg border border-line bg-elevated px-3 py-2">
                    <p className="text-[10px] text-faint">{formatTime(turn.created_at)}</p>
                    <p className="mt-1 whitespace-pre-wrap text-[11px] leading-5">
                      <span className="text-faint">用户：</span>
                      {turn.user_message}
                    </p>
                    <p className="mt-0.5 whitespace-pre-wrap text-[11px] leading-5 text-muted">
                      <span className="text-faint">助手：</span>
                      {turn.assistant_message}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>
      )}
    </Sheet>
  )
}
