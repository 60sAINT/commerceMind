import { AlertTriangle, Check, Sparkles } from 'lucide-react'

import type { SkillSummary } from '@/types/api'

interface SkillMenuProps {
  skills: SkillSummary[]
  loading: boolean
  error: string | null
  enabled: boolean
  activeIndex: number
  selectedIds: Set<string>
  maxReached: boolean
  onPick: (skill: SkillSummary) => void
  onHover: (index: number) => void
}

/**
 * 输入 `/` 后弹出的技能候选列表。
 * 只负责展示与鼠标交互；键盘上下选择由 Composer 统一处理。
 */
export function SkillMenu({
  skills,
  loading,
  error,
  enabled,
  activeIndex,
  selectedIds,
  maxReached,
  onPick,
  onHover,
}: SkillMenuProps) {
  const empty = !loading && !error && enabled && skills.length === 0

  return (
    <div
      className="absolute bottom-full left-0 right-0 z-30 mb-2 overflow-hidden rounded-xl border border-line bg-elevated shadow-float cm-fade-up"
      role="listbox"
      aria-label="技能候选"
    >
      <div className="flex items-center justify-between gap-3 border-b border-line px-3 py-2 text-[10px] text-faint">
        <span className="inline-flex items-center gap-1.5">
          <Sparkles className="size-3" />
          选择技能
        </span>
        <span>{maxReached ? '已达到 3 个上限' : '↑↓ 选择 · Enter 确认 · Esc 关闭'}</span>
      </div>

      {loading && skills.length === 0 && (
        <p className="px-3 py-3 text-[11px] text-muted" role="status">
          正在加载技能…
        </p>
      )}

      {error && (
        <div className="flex items-start gap-2 px-3 py-3 text-[11px] leading-5 text-danger">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span className="min-w-0">
            {error}
            <span className="mt-0.5 block text-faint">可在「技能」面板中重新加载。</span>
          </span>
        </div>
      )}

      {!loading && !error && !enabled && (
        <p className="px-3 py-3 text-[11px] leading-5 text-muted">
          当前环境未启用技能。启用后输入 <code className="font-mono">/</code> 即可选择。
        </p>
      )}

      {empty && (
        <p className="px-3 py-3 text-[11px] text-muted">没有匹配的技能，换个关键词试试。</p>
      )}

      {skills.length > 0 && (
        <ul className="max-h-72 overflow-y-auto py-1">
          {skills.map((skill, index) => {
            const selected = selectedIds.has(skill.id)
            const active = index === activeIndex
            const disabled = !skill.enabled || (maxReached && !selected)
            return (
              <li key={skill.id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={selected}
                  disabled={disabled}
                  onMouseEnter={() => onHover(index)}
                  onMouseDown={(event) => {
                    // 阻止 textarea 先失焦，避免菜单在点击前关闭。
                    event.preventDefault()
                  }}
                  onClick={() => onPick(skill)}
                  className={`flex w-full items-start gap-2.5 px-3 py-2 text-left transition-colors ${
                    active ? 'bg-hover' : ''
                  } ${disabled ? 'cursor-not-allowed opacity-45' : 'hover:bg-hover'}`}
                >
                  <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-md border border-line bg-canvas text-[10px] font-semibold text-muted">
                    {skill.name.slice(0, 1)}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2">
                      <span className="truncate text-xs font-medium">{skill.name}</span>
                      <span className="shrink-0 font-mono text-[10px] text-faint">/{skill.id}</span>
                      {!skill.enabled && (
                        <span className="shrink-0 rounded-full bg-soft px-1.5 py-0.5 text-[9px] text-faint">
                          停用
                        </span>
                      )}
                    </span>
                    {skill.description && (
                      <span className="mt-0.5 line-clamp-2 block text-[11px] leading-5 text-muted">
                        {skill.description}
                      </span>
                    )}
                  </span>
                  {selected && <Check className="mt-0.5 size-3.5 shrink-0 text-success" />}
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
