import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, RefreshCw, Sparkles } from 'lucide-react'

import { isApiError } from '@/api'
import { getSkills, reloadSkills } from '@/services'
import type { SkillsResponse } from '@/types/api'
import { Sheet } from '@/components/ui/Sheet'

interface SkillsPanelProps {
  open: boolean
  onClose: () => void
}

function errorText(error: unknown): string {
  if (isApiError(error)) return error.message
  return error instanceof Error ? error.message : '加载技能失败'
}

export function SkillsPanel({ open, onClose }: SkillsPanelProps) {
  const [data, setData] = useState<SkillsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloading, setReloading] = useState(false)
  const [reloadMessage, setReloadMessage] = useState<string | null>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await getSkills({ signal })
      if (signal?.aborted) return
      setData(next)
      setError(null)
    } catch (caught) {
      if (signal?.aborted) return
      setError(errorText(caught))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    getSkills({ signal: controller.signal })
      .then((next) => {
        if (controller.signal.aborted) return
        setData(next)
        setError(null)
      })
      .catch((caught) => {
        if (!controller.signal.aborted) setError(errorText(caught))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [open])

  const reload = useCallback(async () => {
    setReloading(true)
    setReloadMessage(null)
    try {
      const report = await reloadSkills()
      setReloadMessage(`已重新加载：新增 ${report.added.length} · 更新 ${report.updated.length} · 移除 ${report.removed.length}`)
      await load()
    } catch (caught) {
      setReloadMessage(
        isApiError(caught) && caught.status === 404
          ? '当前后端未开启技能热重载（需要 skills.enabled 与 skills.debug）'
          : errorText(caught),
      )
    } finally {
      setReloading(false)
    }
  }, [load])

  return (
    <Sheet
      open={open}
      onClose={onClose}
      title="技能"
      description="后端从 SKILL.md 加载的能力目录，正文不会在这里展示。"
      width="lg"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-xs text-muted">
          <Sparkles className="size-4" />
          {data ? (
            <span>
              {data.enabled ? `已启用 · ${data.count} 个` : '未启用'}
              {data.version > 0 && ` · v${data.version}`}
            </span>
          ) : (
            <span>{loading ? '正在加载…' : '暂无数据'}</span>
          )}
        </div>
        <button
          type="button"
          onClick={() => void reload()}
          disabled={reloading || loading}
          className="inline-flex items-center gap-1.5 rounded-md border border-line px-2.5 py-1.5 text-[11px] text-muted transition-colors hover:bg-hover hover:text-ink disabled:opacity-50"
        >
          <RefreshCw className={`size-3 ${reloading ? 'cm-spin' : ''}`} />
          重新加载
        </button>
      </div>

      {reloadMessage && (
        <p className="mt-3 rounded-md border border-line bg-soft px-3 py-2 text-[11px] leading-5 text-muted">
          {reloadMessage}
        </p>
      )}

      {error && (
        <div className="mt-3 flex items-start gap-2 rounded-md border border-danger/30 bg-danger/5 px-3 py-2 text-[11px] leading-5 text-danger">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {loading && !data && (
        <div className="mt-4 space-y-2" role="status" aria-label="正在加载技能">
          {[0, 1, 2].map((item) => (
            <div key={item} className="h-16 animate-pulse rounded-lg border border-line bg-soft" />
          ))}
        </div>
      )}

      {data && !data.enabled && (
        <div className="mt-4 rounded-lg border border-line bg-soft px-4 py-6 text-center">
          <p className="text-xs font-medium">技能功能未启用</p>
          <p className="mt-1.5 text-[11px] leading-5 text-muted">
            在后端配置 <code className="font-mono">COMMERCEMIND_SKILLS_ENABLED=true</code>{' '}
            并挂载技能目录后，这里会显示可用的 SKILL.md 目录。
          </p>
        </div>
      )}

      {data && data.skills.length > 0 && (
        <ul className="mt-4 space-y-2">
          {data.skills.map((skill) => (
            <li key={skill.id} className="rounded-lg border border-line bg-elevated px-3.5 py-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-xs font-medium">{skill.name}</p>
                  <p className="mt-0.5 font-mono text-[10px] text-faint">{skill.id}</p>
                </div>
                <span
                  className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] ${
                    skill.enabled ? 'bg-success/10 text-success' : 'bg-soft text-faint'
                  }`}
                >
                  {skill.enabled ? '启用' : '停用'}
                </span>
              </div>
              {skill.description && (
                <p className="mt-1.5 text-[11px] leading-5 text-muted">{skill.description}</p>
              )}
              {skill.keywords.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {skill.keywords.map((keyword) => (
                    <span
                      key={keyword}
                      className="rounded border border-line px-1.5 py-0.5 text-[10px] text-faint"
                    >
                      {keyword}
                    </span>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {data && data.enabled && data.skills.length === 0 && !loading && (
        <p className="mt-4 text-xs text-muted">技能目录为空。</p>
      )}

      {data && data.errors.length > 0 && (
        <div className="mt-4">
          <p className="text-[11px] font-medium text-warning">加载异常</p>
          <ul className="mt-1.5 space-y-1">
            {data.errors.map((item) => (
              <li key={`${item.path}-${item.message}`} className="text-[11px] leading-5 text-muted">
                <span className="font-mono text-faint">{item.path}</span> · {item.message}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Sheet>
  )
}
