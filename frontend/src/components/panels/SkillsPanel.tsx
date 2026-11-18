import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Trash2 } from 'lucide-react'
import { useSkillsStore } from '@/stores/skills'
import { deleteSkillPackage, getSkillPackage } from '@/services/skills'
import { Sheet } from '@/components/ui/Sheet'
import { SkillUpload } from './SkillUpload'
import type { SkillSummary } from '@/types/api'

interface SkillsPanelProps {
  open: boolean
  onClose: () => void
}

export function SkillsPanel({ open, onClose }: SkillsPanelProps) {
  const { data, loading, error, reloading, reloadMessage, load, reload, clearReloadMessage } =
    useSkillsStore()
  const [busy, setBusy] = useState(false)
  const [deleting, setDeleting] = useState<SkillSummary | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const locked = busy || reloading || loading
  const canManage = Boolean(data?.enabled && data.management_enabled)
  const close = useCallback(() => {
    if (!busy && !reloading) onClose()
  }, [busy, reloading, onClose])

  useEffect(() => {
    if (!open) return
    clearReloadMessage()
    void load(true)
  }, [clearReloadMessage, load, open])

  useEffect(() => {
    if (!busy) return
    const prevent = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', prevent)
    return () => window.removeEventListener('beforeunload', prevent)
  }, [busy])

  async function remove() {
    if (!deleting || locked) return
    setBusy(true)
    setActionError(null)
    setMessage(null)
    try {
      const pkg = await getSkillPackage(deleting.id)
      await deleteSkillPackage(deleting.id, pkg.revision)
      setDeleting(null)
      setMessage('技能已删除。')
      await load(true)
      if (useSkillsStore.getState().error) setMessage('技能已删除，列表刷新失败，请重新加载。')
    } catch (e) {
      setActionError(
        `${e instanceof Error ? e.message : '删除失败'}。若为网络异常，请刷新列表确认结果。`,
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <Sheet
      open={open}
      onClose={close}
      title="技能"
      description="上传技能后，可在聊天输入框用 / 选择。"
      width="lg"
    >
      <div className="flex items-center justify-between gap-3">
        <p className="text-xs text-muted">
          {data
            ? data.enabled
              ? `${data.count} 个技能 · 已启用`
              : '技能未启用'
            : loading
              ? '正在加载…'
              : '暂无数据'}
        </p>
        <button
          type="button"
          disabled={locked}
          onClick={() => {
            setActionError(null)
            void reload()
          }}
          className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-xs text-muted hover:bg-hover disabled:opacity-50"
        >
          <RefreshCw className={`size-3.5 ${reloading ? 'cm-spin' : ''}`} />
          重新加载
        </button>
      </div>
      {canManage && (
        <fieldset disabled={locked || Boolean(deleting)} className="min-w-0">
          <SkillUpload onBusy={setBusy} />
        </fieldset>
      )}
      {data?.enabled && !data.management_enabled && (
        <p className="mt-4 text-xs leading-5 text-muted">
          当前为只读模式。上传和删除需要后端启用 skills.enabled 与 skills.debug。
        </p>
      )}
      {data && !data.enabled && (
        <p className="mt-4 rounded-lg bg-soft p-4 text-xs leading-5 text-muted">
          后端尚未启用技能。请配置 COMMERCEMIND_SKILLS_ENABLED=true 并挂载技能目录。
        </p>
      )}
      {(error || actionError) && (
        <p role="alert" className="mt-3 text-xs leading-5 text-danger">
          {actionError || error}
        </p>
      )}
      {(message || reloadMessage) && (
        <p role="status" className="mt-3 text-xs leading-5 text-muted">
          {message || reloadMessage}
        </p>
      )}
      {deleting && (
        <div className="mt-4 rounded-lg border border-danger/30 bg-danger/5 p-3 text-xs">
          <p className="break-words leading-5">
            确认删除 {deleting.name}？技能文件将移除，之后的聊天不再使用它；已有聊天记录保留。
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              disabled={locked}
              onClick={() => void remove()}
              className="rounded bg-danger px-3 py-1.5 text-white disabled:opacity-50"
            >
              {busy ? '正在删除…' : '确认删除'}
            </button>
            <button
              type="button"
              disabled={locked}
              onClick={() => {
                setDeleting(null)
                setActionError(null)
              }}
              className="rounded border border-line px-3 py-1.5 hover:bg-hover disabled:opacity-50"
            >
              取消
            </button>
          </div>
        </div>
      )}
      {data && data.skills.length > 0 && (
        <ul className="mt-5 divide-y divide-line">
          {data.skills.map((skill) => (
            <li key={skill.id} className="flex items-start gap-3 py-4">
              <div className="min-w-0 flex-1">
                <p className="break-words text-sm font-medium">{skill.name}</p>
                <p className="mt-1 break-all font-mono text-[10px] text-faint">/{skill.id}</p>
                <p className="mt-1.5 break-words text-xs leading-5 text-muted">
                  {skill.description}
                </p>
              </div>
              {canManage && (
                <button
                  type="button"
                  aria-label={`删除 ${skill.name}`}
                  disabled={locked || Boolean(deleting)}
                  onClick={() => {
                    setDeleting(skill)
                    setActionError(null)
                    setMessage(null)
                  }}
                  className="shrink-0 rounded p-2 text-faint hover:bg-hover hover:text-danger disabled:opacity-50"
                >
                  <Trash2 className="size-4" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {data?.enabled && !data.skills.length && !loading && (
        <p className="mt-6 text-center text-xs text-muted">还没有技能，上传第一个试试。</p>
      )}
      {data && data.errors.length > 0 && (
        <details className="mt-4 text-xs text-muted">
          <summary className="cursor-pointer text-warning">
            {data.errors.length} 个文件加载异常
          </summary>
          <ul className="mt-2 space-y-1">
            {data.errors.map((item) => (
              <li key={item.path + item.message} className="break-all leading-5">
                {item.path} · {item.message}
              </li>
            ))}
          </ul>
        </details>
      )}
    </Sheet>
  )
}
