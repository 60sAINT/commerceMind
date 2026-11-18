import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import { ArrowUp, Sparkles, Square, X } from 'lucide-react'

import { isStandaloneSkillCommand } from '@/lib/skills'
import type { SkillSummary } from '@/types/api'
import { SkillMenu } from './SkillMenu'

export const MAX_MESSAGE_LENGTH = 12_000
export const MAX_SKILLS = 3

interface ComposerProps {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  onStop: () => void
  sending: boolean
  disabled: boolean
  editing: boolean
  skills: SkillSummary[]
  skillsLoading: boolean
  skillsError: string | null
  skillsEnabled: boolean
  selectedSkills: SkillSummary[]
  onSelectSkill: (skill: SkillSummary) => void
  onRemoveSkill: (skillId: string) => void
  maxSkills?: number
}

/** 输入框以 `/` 开头且还没输入空格时，返回过滤词；否则返回 null。 */
function slashQuery(value: string): string | null {
  const match = /^\s*\/([^\s/]*)$/.exec(value)
  return match ? match[1] : null
}

export function Composer({
  value,
  onChange,
  onSubmit,
  onStop,
  sending,
  disabled,
  editing,
  skills,
  skillsLoading,
  skillsError,
  skillsEnabled,
  selectedSkills,
  onSelectSkill,
  onRemoveSkill,
  maxSkills = MAX_SKILLS,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [menuDismissed, setMenuDismissed] = useState(false)
  const [active, setActive] = useState({ query: '', index: 0 })

  const query = slashQuery(value)
  const menuOpen = query !== null && !menuDismissed

  const candidates = useMemo(() => {
    if (!menuOpen) return []
    const needle = (query ?? '').trim().toLowerCase()
    return skills
      .filter((skill) => {
        if (!needle) return true
        return (
          skill.id.toLowerCase().includes(needle) ||
          skill.name.toLowerCase().includes(needle) ||
          skill.description.toLowerCase().includes(needle) ||
          skill.keywords.some((keyword) => keyword.toLowerCase().includes(needle))
        )
      })
      .slice(0, 24)
  }, [menuOpen, query, skills])

  const selectedIds = useMemo(() => new Set(selectedSkills.map((skill) => skill.id)), [selectedSkills])
  const maxReached = selectedSkills.length >= maxSkills
  const activeIndex =
    active.query === (query ?? '')
      ? Math.min(active.index, Math.max(candidates.length - 1, 0))
      : 0

  // 自动增高：先归零再按 scrollHeight 计算，避免删除内容后高度不回落。
  useEffect(() => {
    const element = textareaRef.current
    if (!element) return
    element.style.height = 'auto'
    element.style.height = `${Math.min(element.scrollHeight, 200)}px`
  }, [value])

  const focusInput = useCallback(() => {
    requestAnimationFrame(() => textareaRef.current?.focus())
  }, [])

  const pickSkill = useCallback(
    (skill: SkillSummary) => {
      if (!skill.enabled) return
      if (selectedIds.has(skill.id)) {
        setMenuDismissed(true)
        onChange('')
        focusInput()
        return
      }
      if (selectedSkills.length >= maxSkills) return
      onSelectSkill(skill)
      setMenuDismissed(true)
      onChange('')
      focusInput()
    },
    [focusInput, maxSkills, onChange, onSelectSkill, selectedIds, selectedSkills.length],
  )

  const changeValue = useCallback(
    (next: string) => {
      // 用户继续输入时重新打开菜单；Esc 关闭后不会因旧值自动弹回。
      setMenuDismissed(false)
      onChange(next)
    },
    [onChange],
  )

  const submit = useCallback(() => {
    if (sending || disabled || !value.trim()) return
    textareaRef.current?.form?.requestSubmit()
  }, [disabled, sending, value])

  const handleKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
      if (event.key === 'Escape' && menuOpen) {
        event.preventDefault()
        setMenuDismissed(true)
        return
      }

      const composing = event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229
      if (menuOpen && candidates.length > 0) {
        if (event.key === 'ArrowDown') {
          event.preventDefault()
          setActive({ query: query ?? '', index: (activeIndex + 1) % candidates.length })
          return
        }
        if (event.key === 'ArrowUp') {
          event.preventDefault()
          setActive({
            query: query ?? '',
            index: (activeIndex - 1 + candidates.length) % candidates.length,
          })
          return
        }
        // Cmd/Ctrl+Enter 始终按“发送”处理；普通 Enter 用于确认候选。
        if (
          event.key === 'Enter' &&
          !composing &&
          !event.metaKey &&
          !event.ctrlKey &&
          !event.shiftKey
        ) {
          event.preventDefault()
          pickSkill(candidates[activeIndex])
          return
        }
      }

      if (event.key !== 'Enter') return
      // 中文输入法组合期间的回车只用于选词，不能触发发送。
      if (composing) return

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
    [activeIndex, candidates, menuOpen, pickSkill, query, submit],
  )

  const nearLimit = value.length >= MAX_MESSAGE_LENGTH * 0.9
  const sendDisabled = !value.trim() || disabled || sending
  const standaloneCommand = isStandaloneSkillCommand(value)

  return (
    <div className="relative mx-auto w-full max-w-3xl xl:max-w-4xl">
      {menuOpen && (
        <SkillMenu
          skills={candidates}
          loading={skillsLoading}
          error={skillsError}
          enabled={skillsEnabled}
          activeIndex={activeIndex}
          selectedIds={selectedIds}
          maxReached={maxReached}
          onPick={pickSkill}
          onHover={(index) => setActive({ query: query ?? '', index })}
        />
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault()
          onSubmit()
        }}
        className="flex flex-col gap-2 rounded-xl border border-line bg-elevated p-2 shadow-soft transition-colors focus-within:border-line-strong"
      >
        {selectedSkills.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5 px-1">
            {selectedSkills.map((skill) => (
              <span
                key={skill.id}
                className="inline-flex max-w-full items-center gap-1 rounded-full border border-line bg-soft px-2 py-0.5 text-[11px] text-ink"
              >
                <Sparkles className="size-3 shrink-0 text-muted" />
                <span className="truncate">{skill.name}</span>
                <span className="shrink-0 font-mono text-[10px] text-faint">/{skill.id}</span>
                <button
                  type="button"
                  onClick={() => onRemoveSkill(skill.id)}
                  aria-label={`移除技能 ${skill.name}`}
                  className="ml-0.5 grid size-4 shrink-0 place-items-center rounded-full text-faint transition-colors hover:bg-hover hover:text-ink"
                >
                  <X className="size-3" />
                </button>
              </span>
            ))}
            <span className="ml-auto shrink-0 text-[10px] text-faint">
              {selectedSkills.length}/{maxSkills}
            </span>
          </div>
        )}

        <div className="flex items-end gap-2">
          <textarea
            ref={textareaRef}
            id="composer-input"
            value={value}
            onChange={(event) => changeValue(event.target.value)}
            onKeyDown={handleKeyDown}
            rows={1}
            maxLength={MAX_MESSAGE_LENGTH}
            placeholder="输入消息…（/ 选择技能，Enter 发送）"
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
        </div>
      </form>

      <div className="mt-2 flex items-center justify-between gap-3 px-1 text-[10px] text-faint">
        <span className="truncate">
          {editing ? '正在编辑上一条消息 · Esc 取消' : '回复内容由 AI 生成，请核实重要信息'}
        </span>
        <span className="flex shrink-0 items-center gap-3">
          <span className="hidden md:inline">输入 / 选择技能 · 最多 {maxSkills} 个</span>
          {nearLimit && (
            <span className={value.length >= MAX_MESSAGE_LENGTH ? 'text-danger' : 'text-warning'}>
              {value.length} / {MAX_MESSAGE_LENGTH}
            </span>
          )}
        </span>
      </div>

      {standaloneCommand && !sending && (
        <p className="mt-1 px-1 text-[10px] text-warning" role="status">
          单独发送 /技能名 不会生效，请在命令后补充具体问题。
        </p>
      )}
    </div>
  )
}
