/**
 * 错误码到用户可读文案的映射。
 * 自定义 error_code 来自 api/main.py 的 _error_response 与 core/llm.py。
 */

const ERROR_MESSAGES: Record<string, string> = {
  // 网络 / 客户端
  network_error: '无法连接后端服务，请确认服务已启动',
  timeout: '请求超时，请重试',
  aborted: '请求已取消',
  invalid_response: '后端返回了无法解析的数据',

  // /chat 业务错误
  model_not_configured: '后端未配置模型 API Key，暂时无法对话',
  request_timeout: '模型响应超时，请重试',
  client_request_id_conflict: '请求标识冲突，请重新发送',
  conversation_busy: '当前会话正在处理上一条消息，请稍候',
  conversation_lease_lost: '会话处理已中断，请重试',
  skill_not_found: '所选技能不存在，请刷新技能列表',
  skill_unavailable: '技能内容不可用，请在技能面板重新加载',
  too_many_skills: '最多选择 3 个技能，请先移除多余的技能',
  skill_message_required: '选择技能后还需要输入问题，请在 /技能名 后补充具体问题',
  internal_error: '服务内部错误，请稍后重试',

  // LLM 上游
  llm_timeout: '模型响应超时，请稍后重试',
  llm_auth_failed: '模型鉴权失败，请检查后端配置',
  llm_not_found: '模型不存在，请检查后端配置',
  llm_bad_request: '模型请求被拒绝，请检查后端配置',
  llm_rate_limited: '模型调用频率受限，请稍后重试',
  llm_connection_failed: '无法连接模型服务，请稍后重试',
  llm_upstream_error: '模型服务返回异常，请稍后重试',
  llm_api_error: '模型服务调用失败，请稍后重试',
  llm_empty_response: '模型返回了空响应，请重试',
  summary_truncated: '记忆摘要被截断',

  // HTTP 兜底（FastAPI 默认 detail 或代理错误）
  http_400: '请求参数不合法',
  http_401: '请求未授权',
  http_403: '没有访问权限',
  http_404: '接口不存在，请确认后端版本或调试开关',
  http_409: '请求冲突，请稍后重试',
  http_422: '请求参数不合法',
  http_429: '请求过于频繁，请稍后重试',
  http_500: '后端服务不可用，请确认服务已启动',
  http_502: '网关异常，请稍后重试',
  http_503: '服务暂不可用，请稍后重试',
  http_504: '网关超时，请稍后重试',
}

const NON_RETRYABLE_CODES = new Set([
  'aborted',
  'invalid_response',
  'model_not_configured',
  'skill_not_found',
  'skill_unavailable',
  'too_many_skills',
  'skill_message_required',
  'llm_auth_failed',
  'llm_not_found',
  'llm_bad_request',
])

const RETRYABLE_CODES = new Set([
  'network_error',
  'timeout',
  'request_timeout',
  'client_request_id_conflict',
  'conversation_busy',
  'conversation_lease_lost',
  'internal_error',
  'llm_timeout',
  'llm_rate_limited',
  'llm_connection_failed',
  'llm_upstream_error',
  'llm_api_error',
  'llm_empty_response',
])

export interface ApiErrorInit {
  status: number
  code: string
  message?: string
  requestId?: string | null
  retryable?: boolean
  body?: unknown
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null
  readonly retryable: boolean
  readonly body: unknown

  constructor(init: ApiErrorInit) {
    super(init.message ?? resolveErrorMessage(init.code, init.status))
    this.name = 'ApiError'
    this.status = init.status
    this.code = init.code
    this.requestId = init.requestId ?? null
    this.retryable = init.retryable ?? isRetryable(init.status, init.code)
    this.body = init.body
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError
}

export function isRetryable(status: number, code: string): boolean {
  if (NON_RETRYABLE_CODES.has(code)) return false
  if (RETRYABLE_CODES.has(code)) return true
  return status === 408 || status === 429 || status >= 500
}

export function resolveErrorMessage(code: string, status: number, fallback?: string): string {
  // FastAPI 参数校验错误（422）没有业务错误码，优先展示后端 detail。
  if (code === 'http_422' && fallback) return fallback

  return (
    ERROR_MESSAGES[code] ??
    fallback ??
    (status > 0 ? `请求失败（HTTP ${status}）` : '请求失败，请稍后重试')
  )
}
