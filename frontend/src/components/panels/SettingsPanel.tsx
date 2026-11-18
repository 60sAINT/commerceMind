import { useState } from 'react'
import { Check, Database, Moon, RefreshCw, Sun } from 'lucide-react'

import { DEFAULT_API_BASE_URL } from '@/api'
import { healthProblems, modelLabel, useHealthStore } from '@/stores/health'
import { useSessionStore } from '@/stores/session'
import { useSkillsStore } from '@/stores/skills'
import { Sheet } from '@/components/ui/Sheet'

interface SettingsPanelProps {
  open: boolean
  onClose: () => void
  userId: string
  sessionId: string
  onChangeUserId: (userId: string) => void
  onChangeSessionId: (sessionId: string) => void
  onOpenMemory: () => void
}

function formatTime(timestamp: number | null): string {
  if (!timestamp) return '尚未检测'
  return new Date(timestamp).toLocaleTimeString('zh-CN', { hour12: false })
}

export function SettingsPanel({
  open,
  onClose,
  userId,
  sessionId,
  onChangeUserId,
  onChangeSessionId,
  onOpenMemory,
}: SettingsPanelProps) {
  const theme = useSessionStore((state) => state.theme)
  const setTheme = useSessionStore((state) => state.setTheme)
  const baseUrl = useSessionStore((state) => state.baseUrl)
  const setBaseUrl = useSessionStore((state) => state.setBaseUrl)
  const health = useHealthStore()
  const [draftUserId, setDraftUserId] = useState(userId)
  const [draftSessionId, setDraftSessionId] = useState(sessionId)
  const [draftBaseUrl, setDraftBaseUrl] = useState(baseUrl)
  const [baseUrlNotice, setBaseUrlNotice] = useState<string | null>(null)

  const problems = healthProblems(health)

  const applyBaseUrl = () => {
    const next = draftBaseUrl.trim() || DEFAULT_API_BASE_URL
    setBaseUrl(next)
    setDraftBaseUrl(next)
    setBaseUrlNotice('已应用，后续 JSON 与 SSE 请求都会使用新地址。')
    void health.check()
    void useSkillsStore.getState().load(true)
  }

  const applySessionId = () => {
    const next = draftSessionId.trim()
    setDraftSessionId(next)
    onChangeSessionId(next)
  }

  return (
    <Sheet open={open} onClose={onClose} title="设置" description="本地偏好与后端连接信息。">
      <section>
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-faint">外观</h3>
        <div className="mt-2 grid grid-cols-2 gap-2">
          {(
            [
              { value: 'light', label: '浅色', icon: Sun },
              { value: 'dark', label: '深色', icon: Moon },
            ] as const
          ).map((option) => (
            <button
              key={option.value}
              type="button"
              onClick={() => setTheme(option.value)}
              className={`flex items-center justify-between gap-2 rounded-lg border px-3 py-2.5 text-xs transition-colors ${
                theme === option.value
                  ? 'border-line-strong bg-soft text-ink'
                  : 'border-line text-muted hover:bg-hover'
              }`}
            >
              <span className="inline-flex items-center gap-2">
                <option.icon className="size-3.5" />
                {option.label}
              </span>
              {theme === option.value && <Check className="size-3.5" />}
            </button>
          ))}
        </div>
      </section>

      <section className="mt-6">
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-faint">身份</h3>
        <label htmlFor="settings-user-id" className="mt-2 block text-[11px] text-muted">
          用户 ID
        </label>
        <div className="mt-1.5 flex gap-2">
          <input
            id="settings-user-id"
            value={draftUserId}
            onChange={(event) => setDraftUserId(event.target.value)}
            className="h-9 min-w-0 flex-1 rounded-md border border-line bg-canvas px-2.5 text-xs outline-none focus:border-line-strong"
          />
          <button
            type="button"
            disabled={!draftUserId.trim() || draftUserId.trim() === userId}
            onClick={() => onChangeUserId(draftUserId.trim())}
            className="h-9 shrink-0 rounded-md bg-ink px-3 text-xs text-canvas disabled:opacity-40"
          >
            切换
          </button>
        </div>
        <p className="mt-1.5 text-[10px] leading-5 text-faint">
          切换用户会结束当前会话并加载该用户的历史会话。
        </p>
      </section>

      <section className="mt-6">
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-faint">会话</h3>
        <label htmlFor="settings-session-id" className="mt-2 block text-[11px] text-muted">
          会话 ID
        </label>
        <div className="mt-1.5 flex gap-2">
          <input
            id="settings-session-id"
            value={draftSessionId}
            onChange={(event) => setDraftSessionId(event.target.value)}
            placeholder="留空则自动创建"
            className="h-9 min-w-0 flex-1 rounded-md border border-line bg-canvas px-2.5 font-mono text-[11px] outline-none focus:border-line-strong"
          />
          <button
            type="button"
            disabled={draftSessionId.trim() === sessionId}
            onClick={applySessionId}
            className="h-9 shrink-0 rounded-md bg-ink px-3 text-xs text-canvas disabled:opacity-40"
          >
            切换
          </button>
        </div>
        <p className="mt-1.5 text-[10px] leading-5 text-faint">
          指定已有会话可续接记忆；留空时下一条消息会自动创建新会话。
        </p>
      </section>

      <section className="mt-6">
        <h3 className="text-[11px] font-medium uppercase tracking-wider text-faint">连接</h3>
        <label htmlFor="settings-base-url" className="mt-2 block text-[11px] text-muted">
          API 地址
        </label>
        <div className="mt-1.5 flex gap-2">
          <input
            id="settings-base-url"
            value={draftBaseUrl}
            onChange={(event) => setDraftBaseUrl(event.target.value)}
            placeholder={DEFAULT_API_BASE_URL}
            spellCheck={false}
            className="h-9 min-w-0 flex-1 rounded-md border border-line bg-canvas px-2.5 font-mono text-[11px] outline-none focus:border-line-strong"
          />
          <button
            type="button"
            disabled={!draftBaseUrl.trim() || draftBaseUrl.trim() === baseUrl}
            onClick={applyBaseUrl}
            className="h-9 shrink-0 rounded-md bg-ink px-3 text-xs text-canvas disabled:opacity-40"
          >
            应用
          </button>
        </div>
        <p className="mt-1.5 text-[10px] leading-5 text-faint">
          默认 <code className="font-mono">{DEFAULT_API_BASE_URL}</code> 由开发代理或同源反向代理转发。
          后端未开启 CORS，浏览器直连绝对地址可能被拦截。
        </p>
        {baseUrlNotice && <p className="mt-1 text-[10px] text-success">{baseUrlNotice}</p>}

        <dl className="mt-3 divide-y divide-line rounded-lg border border-line text-xs">
          <div className="flex items-center justify-between gap-3 px-3 py-2.5">
            <dt className="text-muted">当前地址</dt>
            <dd className="truncate font-mono text-[11px]">{baseUrl}</dd>
          </div>
          <div className="flex items-center justify-between gap-3 px-3 py-2.5">
            <dt className="text-muted">状态</dt>
            <dd
              className={
                health.connection === 'ok'
                  ? 'text-success'
                  : health.connection === 'offline'
                    ? 'text-danger'
                    : 'text-warning'
              }
            >
              {health.connection === 'loading' ? '检测中' : health.connection === 'ok' ? '正常' : health.connection === 'degraded' ? '降级' : '离线'}
            </dd>
          </div>
          <div className="flex items-center justify-between gap-3 px-3 py-2.5">
            <dt className="text-muted">模型</dt>
            <dd className="truncate">{modelLabel(health)}</dd>
          </div>
          <div className="flex items-center justify-between gap-3 px-3 py-2.5">
            <dt className="text-muted">记忆</dt>
            <dd className="text-[11px]">
              {health.data?.memory.enabled
                ? `Redis ${health.data.memory.redis} · Chroma ${health.data.memory.chroma}`
                : '未启用'}
            </dd>
          </div>
          <div className="flex items-center justify-between gap-3 px-3 py-2.5">
            <dt className="text-muted">最近检测</dt>
            <dd className="text-[11px] text-muted">{formatTime(health.checkedAt)}</dd>
          </div>
        </dl>

        {problems.length > 0 && (
          <ul className="mt-2 space-y-1 rounded-md border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] leading-5 text-warning">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        )}

        <div className="mt-2 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => void health.check()}
            disabled={health.checking}
            className="inline-flex items-center gap-1.5 rounded-md border border-line px-2.5 py-1.5 text-[11px] text-muted transition-colors hover:bg-hover hover:text-ink disabled:opacity-50"
          >
            <RefreshCw className={`size-3 ${health.checking ? 'cm-spin' : ''}`} />
            手动健康检查
          </button>
          <button
            type="button"
            onClick={onOpenMemory}
            disabled={!sessionId}
            className="inline-flex items-center gap-1.5 rounded-md border border-line px-2.5 py-1.5 text-[11px] text-muted transition-colors hover:bg-hover hover:text-ink disabled:opacity-40"
          >
            <Database className="size-3" />
            记忆调试
          </button>
        </div>
        <p className="mt-1.5 text-[10px] leading-5 text-faint">
          记忆调试仅在后端开启 <code className="font-mono">COMMERCEMIND_MEMORY_DEBUG=true</code> 时可用。
        </p>
      </section>

      <section className="mt-6 border-t border-line pt-4 text-[10px] leading-5 text-faint">
        CommerceMind 前端 0.1.0 · SSE 流式对话
      </section>
    </Sheet>
  )
}
