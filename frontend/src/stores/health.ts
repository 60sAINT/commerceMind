import { create } from 'zustand'

import { getHealth } from '@/services'
import type { HealthResponse } from '@/types/api'

export type ConnectionState = 'loading' | 'ok' | 'degraded' | 'offline'

interface HealthState {
  connection: ConnectionState
  data: HealthResponse | null
  error: string | null
  checkedAt: number | null
  checking: boolean
  check: () => Promise<void>
}

/** 并发调用（挂载、轮询、窗口聚焦）合并成一次真实请求。 */
let inFlight: Promise<void> | null = null

export const useHealthStore = create<HealthState>((set) => ({
  connection: 'loading',
  data: null,
  error: null,
  checkedAt: null,
  checking: false,
  check: async () => {
    if (inFlight) return inFlight
    set({ checking: true })
    inFlight = (async () => {
      try {
        const data = await getHealth({ timeoutMs: 8000 })
        set({
          data,
          connection: data.status === 'ok' ? 'ok' : 'degraded',
          error: null,
          checkedAt: Date.now(),
          checking: false,
        })
      } catch (error) {
        set({
          connection: 'offline',
          error: error instanceof Error ? error.message : '无法连接后端',
          checkedAt: Date.now(),
          checking: false,
        })
      } finally {
        inFlight = null
      }
    })()
    return inFlight
  },
}))

/** 把 /health 的字段翻译成用户能看懂的告警文案。 */
export function healthProblems(state: HealthState): string[] {
  if (state.connection === 'offline') {
    return [state.error ? `无法连接后端：${state.error}` : '无法连接后端，请确认服务已启动']
  }
  const data = state.data
  if (!data) return []

  const problems: string[] = []
  if (!data.model_configured) problems.push('未配置模型 Key，对话请求会返回 503')
  if (data.memory.enabled && (data.memory.redis !== 'ok' || data.memory.chroma !== 'ok')) {
    const detail = data.memory.detail ?? `Redis ${data.memory.redis} · Chroma ${data.memory.chroma}`
    problems.push(`记忆依赖异常：${detail}`)
  }
  if (problems.length === 0 && data.detail) problems.push(data.detail)
  return problems
}

export function modelLabel(state: HealthState): string {
  if (state.connection === 'offline') return '离线'
  if (!state.data) return state.checking ? '检测中' : '未知模型'
  if (!state.data.model_configured) return '未配置模型'
  return state.data.model || '未知模型'
}
