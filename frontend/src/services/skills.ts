import { requestJson } from '@/api'
import type {
  SkillReloadReport,
  SkillsResponse,
  SkillPackage,
  SkillMutationResult,
  SkillUploadPreview,
} from '@/types/api'

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

export function getSkillPackage(id: string, signal?: AbortSignal) {
  return requestJson<SkillPackage>(`/skills/${encodeURIComponent(id)}/files`, { signal })
}
export function previewSkillUpload(file: File, signal?: AbortSignal) {
  return requestJson<SkillUploadPreview>('/skills/upload/preview', {
    method: 'POST',
    body: file,
    params: { filename: file.name },
    headers: { 'Content-Type': 'application/octet-stream' },
    signal,
    timeoutMs: 30_000,
  })
}
export function uploadSkill(file: File) {
  return requestJson<SkillMutationResult>('/skills/upload', {
    method: 'POST',
    body: file,
    params: { filename: file.name },
    headers: { 'Content-Type': 'application/octet-stream' },
    timeoutMs: 30_000,
  })
}
export function deleteSkillPackage(id: string, revision: string) {
  return requestJson<{ deleted: boolean; report: SkillReloadReport }>(
    `/skills/${encodeURIComponent(id)}`,
    {
      method: 'DELETE',
      params: { revision },
      timeoutMs: 30_000,
    },
  )
}
