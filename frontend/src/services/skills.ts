import { requestJson } from '@/api'
import type { SkillReloadReport, SkillsResponse } from '@/types/api'

interface SkillsOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

/** GET /skills：只返回元数据，不包含正文与资源内容。 */
export function getSkills(options: SkillsOptions = {}): Promise<SkillsResponse> {
  return requestJson<SkillsResponse>('/skills', {
    signal: options.signal,
    timeoutMs: options.timeoutMs ?? 15_000,
  })
}

/**
 * POST /skills/reload
 * 仅在 Skills enabled 且 debug 开启时注册；否则返回 404 http_404。
 */
export function reloadSkills(options: SkillsOptions = {}): Promise<SkillReloadReport> {
  return requestJson<SkillReloadReport>('/skills/reload', {
    method: 'POST',
    signal: options.signal,
    timeoutMs: options.timeoutMs ?? 30_000,
  })
}
