import { useCallback, useEffect } from 'react'
import { AlertTriangle, Clock3, RefreshCw, Sparkles } from 'lucide-react'

import { useSkillsStore } from '@/stores/skills'
import { Sheet } from '@/components/ui/Sheet'

interface SkillsPanelProps {
  open: boolean
  onClose: () => void
}

function formatLoadedAt(value: string): string {
  if (!value) return '未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function SkillsPanel({ open, onClose }: SkillsPanelProps) {
  const data = useSkillsStore((state) => state.data)
  const loading = useSkillsStore((state) => state.loading)
  const error = useSkillsStore((state) => state.error)
  const reloading = useSkillsStore((state) => state.reloading)
  const reloadMessage = useSkillsStore((state) => state.reloadMessage)
  const load = useSkillsStore((state) => state.load)
  const reload = useSkillsStore((state) => state.reload)
  const clearReloadMessage = useSkillsStore((state) => state.clearReloadMessage)

  useEffect(() => {
    if (!open) return
    clearReloadMessage()
    void load(true)
  }, [clearReloadMessage, load, open])

  const handleReload = useCallback(() => {
    void reload()
  }, [reload])

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
          onClick={handleReload}
          disabled={reloading || loading}
          className="inline-flex items-center gap-1.5 rounded-md border border-line px-2.5 py-1.5 text-[11px] text-muted transition-colors hover:bg-hover hover:text-ink disabled:opacity-50"
        >
          <RefreshCw className={`size-3 ${reloading ? 'cm-spin' : ''}`} />
          重新加载
        </button>
      </div>

      {data && (
        <dl className="mt-3 grid grid-cols-2 gap-2 rounded-lg border border-line bg-soft px-3 py-2 text-[10px] text-muted">
          <div className="min-w-0">
            <dt className="text-faint">目录版本</dt>
            <dd className="mt-0.5 font-mono text-[11px] text-ink">
              {data.version > 0 ? `v${data.version}` : '—'}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="inline-flex items-center gap-1 text-faint">
              <Clock3 className="size-3" />
              加载时间
            </dt>
            <dd className="mt-0.5 truncate text-[11px] text-ink" title={data.loaded_at}>
              {formatLoadedAt(data.loaded_at)}
            </dd>
          </div>
        </dl>
      )}

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
                  <p className="mt-0.5 font-mono text-[10px] text-faint">/{skill.id}</p>
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
