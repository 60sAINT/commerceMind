/**
 * 与后端 Pydantic 模型一一对应的请求 / 响应类型。
 * 后端定义见 api/main.py 与 core/skill_loader.py，改动时需同步这里。
 */

export type HealthStatus = 'ok' | 'degraded'

export interface MemoryHealth {
  enabled: boolean
  redis: string
  chroma: string
  detail: string | null
}

export interface HealthResponse {
  status: HealthStatus
  model_configured: boolean
  model: string
  memory: MemoryHealth
  detail: string | null
}

export interface ChatRequest {
  /** 1 ~ 12000 字符 */
  message: string
  user_id?: string
  session_id?: string | null
  client_request_id?: string | null
  skill_ids?: string[] | null
}

export interface ChatResponse {
  request_id: string
  session_id: string
  response: string
  latency_ms: number
  input_tokens: number | null
  output_tokens: number | null
}

export interface ApiErrorBody {
  detail: string
  request_id: string
  error_code: string
}

export interface SkillError {
  path: string
  message: string
}

export interface SkillSummary {
  id: string
  name: string
  description: string
  keywords: string[]
  version: string
  enabled: boolean
  path: string
}

export interface SkillsResponse {
  enabled: boolean
  count: number
  skills: SkillSummary[]
  errors: SkillError[]
  version: number
  loaded_at: string
}

export interface SkillReloadReport {
  added: string[]
  updated: string[]
  removed: string[]
  unchanged: string[]
  errors: SkillError[]
  version: number
  loaded_at: string
  duration_ms: number
  count: number
}

export interface MemoryTurn {
  turn_id: string
  request_id: string
  user_message: string
  assistant_message: string
  created_at: string
  metadata: Record<string, unknown>
}

export interface MemoryTtl {
  working: number
  state: number
  summary: number
}

export interface MemoryDebugResponse {
  session_id: string
  user_id: string
  turn_count: number
  summary: string
  state: Record<string, unknown>
  turns: MemoryTurn[]
  ttl_s: MemoryTtl
}
