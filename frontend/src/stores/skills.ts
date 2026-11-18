import { create } from 'zustand'

import { isApiError } from '@/api'
import { getSkills, reloadSkills } from '@/services'
import type { SkillsResponse } from '@/types/api'

interface SkillsState {
  data: SkillsResponse | null
  loading: boolean
  error: string | null
  reloading: boolean
  reloadMessage: string | null
  loadedAt: number | null
  load: (force?: boolean) => Promise<void>
  reload: () => Promise<void>
  clearReloadMessage: () => void
}

export function skillsErrorText(error: unknown): string {
  if (isApiError(error)) return error.message
  return error instanceof Error ? error.message : '加载技能失败'
}

/** 并发加载（输入框挂载、面板打开、重载后刷新）合并成一次真实请求。 */
let inFlight: Promise<void> | null = null

export const useSkillsStore = create<SkillsState>((set, get) => ({
  data: null,
  loading: false,
  error: null,
  reloading: false,
  reloadMessage: null,
  loadedAt: null,
  load: async (force = false) => {
    if (inFlight) {
      await inFlight
      if (!force) return
    }
    if (get().data && !force) return
    set({ loading: true, error: null })
    inFlight = (async () => {
      try {
        const data = await getSkills({ timeoutMs: 15_000 })
        set({ data, loadedAt: Date.now(), error: null })
      } catch (error) {
        set({ error: skillsErrorText(error) })
      } finally {
        set({ loading: false })
        inFlight = null
      }
    })()
    return inFlight
  },
  reload: async () => {
    if (get().reloading) return
    set({ reloading: true, reloadMessage: null })
    try {
      const report = await reloadSkills()
      set({
        reloadMessage: `已重新加载：新增 ${report.added.length} · 更新 ${report.updated.length} · 移除 ${report.removed.length}`,
      })
      await get().load(true)
    } catch (error) {
      set({
        reloadMessage:
          isApiError(error) && error.status === 404
            ? '当前环境未开启技能热重载'
            : skillsErrorText(error),
      })
    } finally {
      set({ reloading: false })
    }
  },
  clearReloadMessage: () => set({ reloadMessage: null }),
}))
