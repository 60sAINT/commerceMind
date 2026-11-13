import { clsx } from 'clsx'
import {
  Check,
  LoaderCircle,
  Moon,
  Palette,
  PlugZap,
  RefreshCw,
  Server,
  Sun,
  Waypoints,
} from 'lucide-react'
import { useEffect, useState } from 'react'

import styles from './app.module.less'

type Theme = 'light' | 'dark'

type Probe = {
  kind: 'loading' | 'ok' | 'error'
  message: string
}

type HealthPayload = {
  status?: string
  model?: string
  model_configured?: boolean
  detail?: string | null
}

const stack = [
  'Vite 8',
  'React 19',
  'TypeScript 5.9',
  'Tailwind 4',
  'Less',
  'Zustand 5',
]

const tokens = [
  { name: 'canvas', value: '--cm-canvas' },
  { name: 'sidebar', value: '--cm-sidebar' },
  { name: 'soft', value: '--cm-soft' },
  { name: 'ink', value: '--cm-ink' },
  { name: 'muted', value: '--cm-muted' },
  { name: 'line', value: '--cm-line' },
  { name: 'success', value: '--cm-success' },
  { name: 'danger', value: '--cm-danger' },
]

const milestones = [
  { label: 'P0 工程并入与设计基线', done: true },
  { label: 'P1 类型与接口层', done: false },
  { label: 'P2 会话与状态', done: false },
  { label: 'P3 SSE 流式链路', done: false },
]

function readTheme(): Theme {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light'
}

async function probeHealth(): Promise<Probe> {
  const started = performance.now()

  try {
    const response = await fetch('/api/health', { headers: { Accept: 'application/json' } })
    const payload = (await response.json().catch(() => null)) as HealthPayload | null

    if (!response.ok) {
      throw new Error(payload?.detail ?? `HTTP ${response.status}`)
    }

    const status = payload?.status ?? 'ok'
    const model = payload?.model_configured === false ? '模型未配置' : payload?.model
    const elapsed = Math.round(performance.now() - started)

    return {
      kind: 'ok',
      message: [status, model, `${elapsed} ms`].filter(Boolean).join(' · '),
    }
  } catch (error) {
    return {
      kind: 'error',
      message: error instanceof Error ? error.message : '无法连接后端',
    }
  }
}

export default function App() {
  const [theme, setTheme] = useState<Theme>(readTheme)
  const [probe, setProbe] = useState<Probe>({
    kind: 'loading',
    message: '正在通过 /api 检查后端…',
  })

  useEffect(() => {
    let cancelled = false

    void probeHealth().then((result) => {
      if (!cancelled) {
        setProbe(result)
      }
    })

    return () => {
      cancelled = true
    }
  }, [])

  function toggleTheme() {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    setTheme(next)
    document.documentElement.classList.toggle('dark', next === 'dark')
    document.documentElement.style.colorScheme = next
    localStorage.setItem('commercemind.theme', next)
  }

  async function rerunProbe() {
    setProbe({ kind: 'loading', message: '正在检查…' })
    setProbe(await probeHealth())
  }

  const statusDot = clsx(
    'size-1.5 rounded-full',
    probe.kind === 'ok' && 'bg-success',
    probe.kind === 'error' && 'bg-danger',
    probe.kind === 'loading' && 'cm-breathe bg-faint',
  )

  return (
    <div className="relative min-h-dvh bg-canvas text-ink">
      <div className={styles.backdrop} aria-hidden="true" />

      <main className="mx-auto flex min-h-dvh w-full max-w-5xl flex-col px-6 py-12 sm:px-8 sm:py-16">
        <header className="cm-fade-up flex flex-wrap items-start justify-between gap-6">
          <div className="flex items-center gap-4">
            <span className={styles.brandMark} aria-hidden="true">
              C
            </span>
            <div>
              <p className="text-[10px] font-medium uppercase tracking-[0.2em] text-faint">
                CommerceMind
              </p>
              <h1 className="mt-1 text-2xl font-semibold tracking-tight sm:text-[28px]">
                前端工程基线
              </h1>
            </div>
          </div>

          <div className="flex items-center gap-3">
            <span className="inline-flex items-center gap-2 rounded-full border border-line bg-elevated px-3 py-1.5 text-[11px] text-muted shadow-soft">
              <span className="size-1.5 rounded-full bg-success" aria-hidden="true" />
              P0 · 已就绪
            </span>
            <button
              type="button"
              onClick={toggleTheme}
              className="inline-flex items-center gap-2 rounded-full border border-line bg-elevated px-3.5 py-2 text-xs font-medium text-ink shadow-soft transition-colors hover:bg-hover"
              aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
            >
              {theme === 'dark' ? (
                <Sun className="size-4" aria-hidden="true" />
              ) : (
                <Moon className="size-4" aria-hidden="true" />
              )}
              {theme === 'dark' ? '浅色' : '深色'}
            </button>
          </div>
        </header>

        <section className="cm-fade-up mt-10 grid gap-4 lg:grid-cols-3">
          <article className="rounded-panel border border-line bg-elevated p-6 shadow-soft lg:col-span-2">
            <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-faint">
              <Waypoints className="size-3.5" aria-hidden="true" />
              工程链路
            </div>

            <h2 className="mt-4 text-lg font-semibold tracking-tight">
              前后端同仓，开发期统一走代理
            </h2>
            <p className="mt-2 max-w-xl text-sm leading-7 text-muted">
              Vite 负责开发与构建，Tailwind 负责布局与状态，Less 只承载设计令牌、动画和复杂选择器。
              浏览器请求统一带上 <code className="font-mono text-[12px] text-ink">/api</code>{' '}
              前缀，由开发服务器转发到 FastAPI，生产环境保持同源。
            </p>

            <div className={clsx(styles.stackRow, 'mt-5')}>
              {stack.map((item) => (
                <span
                  key={item}
                  className="rounded-md border border-line bg-soft px-2.5 py-1 font-mono text-[11px] text-muted"
                >
                  {item}
                </span>
              ))}
            </div>
          </article>

          <article className="rounded-panel border border-line bg-elevated p-6 shadow-soft">
            <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-faint">
              <PlugZap className="size-3.5" aria-hidden="true" />
              接口代理
            </div>

            <div className="mt-4 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="font-mono text-[12px] text-ink">/api → 127.0.0.1:8000</p>
                <p className="mt-2 flex items-center gap-2 text-[11px] leading-5 text-muted">
                  <span className={statusDot} aria-hidden="true" />
                  <span aria-live="polite" className="break-words">
                    {probe.message}
                  </span>
                </p>
              </div>

              <button
                type="button"
                onClick={() => void rerunProbe()}
                disabled={probe.kind === 'loading'}
                className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg border border-line bg-soft text-muted transition-colors hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
                aria-label="重新检查后端连接"
              >
                {probe.kind === 'loading' ? (
                  <LoaderCircle className="cm-spin size-3.5" aria-hidden="true" />
                ) : (
                  <RefreshCw className="size-3.5" aria-hidden="true" />
                )}
              </button>
            </div>

            <div className="mt-4 rounded-card border border-line bg-code px-3 py-2.5 font-mono text-[11px] leading-6 text-muted">
              <span className="text-success">GET</span> /api/health
              <br />
              <span className="text-faint">→ proxy rewrite →</span> GET /health
            </div>
          </article>

          <article className="rounded-panel border border-line bg-elevated p-6 shadow-soft lg:col-span-2">
            <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-faint">
              <Palette className="size-3.5" aria-hidden="true" />
              设计令牌
            </div>

            <h2 className="mt-4 text-lg font-semibold tracking-tight">一套变量，两套主题</h2>
            <p className="mt-2 text-sm leading-7 text-muted">
              颜色、圆角、阴影与字体统一由 CSS 变量驱动，Tailwind 使用{' '}
              <code className="font-mono text-[12px] text-ink">@theme inline</code>{' '}
              映射为工具类，Less 模块直接消费同一组变量。切换主题不会触发页面刷新。
            </p>

            <div className={clsx(styles.tokenGrid, 'mt-5')}>
              {tokens.map((token) => (
                <div key={token.name} className="flex items-center gap-2.5">
                  <span
                    className="size-7 shrink-0 rounded-md border border-line"
                    style={{ background: `var(${token.value})` }}
                    aria-hidden="true"
                  />
                  <span className="text-[11px] text-muted">{token.name}</span>
                </div>
              ))}
            </div>
          </article>

          <article className="rounded-panel border border-line bg-elevated p-6 shadow-soft">
            <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-faint">
              <Server className="size-3.5" aria-hidden="true" />
              实施进度
            </div>

            <ul className="mt-4 space-y-3">
              {milestones.map((item) => (
                <li key={item.label} className="flex items-center gap-2.5 text-[12px]">
                  <span
                    className={clsx(
                      'inline-flex size-4 shrink-0 items-center justify-center rounded-full border',
                      item.done
                        ? 'border-success/30 bg-success/10 text-success'
                        : 'border-line bg-soft text-faint',
                    )}
                    aria-hidden="true"
                  >
                    {item.done ? <Check className="size-2.5" /> : null}
                  </span>
                  <span className={item.done ? 'text-ink' : 'text-muted'}>{item.label}</span>
                </li>
              ))}
            </ul>

            <p className="mt-5 border-t border-line pt-4 text-[11px] leading-6 text-faint">
              下一步：对齐后端 Pydantic 模型，封装 /chat、/skills 与错误码。
            </p>
          </article>
        </section>

        <footer className="mt-auto flex flex-wrap items-center justify-between gap-3 pt-12 text-[10px] text-faint">
          <span className="font-mono">frontend/ · p0 · 2026-11-13</span>
          <span>Vite dev proxy · FastAPI same-origin in production</span>
        </footer>
      </main>
    </div>
  )
}
