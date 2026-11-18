import { useEffect, useRef, useState } from 'react'
import { Upload, X } from 'lucide-react'
import { previewSkillUpload, uploadSkill } from '@/services/skills'
import { useSkillsStore } from '@/stores/skills'
import type { SkillUploadPreview } from '@/types/api'

/** 上传只展示名称、描述与确认按钮；文件结构与内容由后端校验。 */
export function SkillUpload({ onBusy }: { onBusy: (busy: boolean) => void }) {
  const input = useRef<HTMLInputElement>(null)
  const generation = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<SkillUploadPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [message, setMessage] = useState<string | null>(null)
  const data = useSkillsStore((s) => s.data)
  const load = useSkillsStore((s) => s.load)
  const maxBytes = data?.limits?.max_total_bytes ?? 1048576

  useEffect(
    () => () => {
      generation.current++
      controller.current?.abort()
    },
    [],
  )

  async function choose(next: File | undefined) {
    if (!next || busy) return
    setMessage(null)
    setError(null)
    setPreview(null)
    setFile(null)
    if (!(next.name.toLowerCase() === 'skill.md' || next.name.toLowerCase().endsWith('.zip'))) {
      setError('请选择 SKILL.md 或 ZIP 文件')
      return
    }
    if (next.size > maxBytes) {
      setError('文件超过当前上传大小限制')
      return
    }
    setBusy(true)
    onBusy(true)
    const version = ++generation.current
    const abort = new AbortController()
    controller.current = abort
    try {
      const result = await previewSkillUpload(next, abort.signal)
      if (version !== generation.current) return
      setFile(next)
      setPreview(result)
    } catch (e) {
      if (version === generation.current) setError(e instanceof Error ? e.message : '校验失败')
    } finally {
      if (version === generation.current) {
        setBusy(false)
        onBusy(false)
      }
    }
  }

  async function confirm() {
    if (!file || !preview || busy) return
    setBusy(true)
    onBusy(true)
    setError(null)
    const version = ++generation.current
    try {
      const result = await uploadSkill(file)
      if (version !== generation.current) return
      setFile(null)
      setPreview(null)
      setMessage(`${result.skill.name} 已上传并启用，可在输入框用 / 选择。`)
      await load(true)
      if (useSkillsStore.getState().error) setMessage((m) => `${m} 列表刷新失败，请重新加载。`)
    } catch (e) {
      if (version === generation.current)
        setError(
          `${e instanceof Error ? e.message : '上传失败'}。若为断网或超时，请先刷新列表确认结果。`,
        )
    } finally {
      if (version === generation.current) {
        setBusy(false)
        onBusy(false)
      }
    }
  }

  return (
    <div className="mt-4 space-y-3">
      <input
        ref={input}
        type="file"
        accept=".md,.zip"
        aria-label="选择技能文件"
        className="hidden"
        disabled={busy}
        onChange={(e) => {
          void choose(e.target.files?.[0])
          e.target.value = ''
        }}
      />
      {!preview && (
        <button
          type="button"
          disabled={busy}
          onClick={() => input.current?.click()}
          className="flex w-full flex-col items-center gap-2 rounded-xl border border-dashed border-line bg-soft px-4 py-6 text-sm hover:bg-hover disabled:opacity-50"
        >
          <Upload className="size-5 text-muted" />
          <span>{busy ? '正在处理…' : '上传技能'}</span>
          <span className="text-xs text-muted">
            选择 SKILL.md 或 ZIP · 最多 {maxBytes / 1048576} MiB
          </span>
        </button>
      )}
      {!preview && (
        <p className="text-[11px] leading-5 text-muted">
          ZIP 结构示例：my-skill/SKILL.md；文件夹名与入口的 name
          一致，仅用小写字母、数字、连字符。当前只支持文本资源。
        </p>
      )}
      {preview && (
        <div className="rounded-xl border border-line bg-soft p-4">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="break-all text-sm font-medium">{preview.skill.name}</p>
              <p className="mt-1 break-words text-xs leading-5 text-muted">
                {preview.skill.description}
              </p>
            </div>
            <button
              type="button"
              aria-label="取消上传"
              disabled={busy}
              onClick={() => {
                setFile(null)
                setPreview(null)
                setError(null)
              }}
              className="shrink-0 rounded p-1 hover:bg-hover disabled:opacity-50"
            >
              <X className="size-4" />
            </button>
          </div>
          <p className="mt-3 text-[11px] text-muted">
            格式校验通过 · {preview.file_count} 个文件 · {(preview.total_bytes / 1024).toFixed(1)}{' '}
            KiB
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={() => void confirm()}
            className="mt-3 rounded-lg bg-ink px-4 py-2 text-xs text-canvas disabled:opacity-50"
          >
            {busy ? '正在上传…' : '上传并启用'}
          </button>
        </div>
      )}
      {error && (
        <p role="alert" className="break-words text-xs leading-5 text-danger">
          {error}
        </p>
      )}
      {message && (
        <p role="status" className="break-words text-xs leading-5 text-success">
          {message}
        </p>
      )}
    </div>
  )
}
