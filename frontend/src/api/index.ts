/**
 * 传输层出口：只负责 HTTP 细节，不包含业务接口。
 * 业务接口在 src/services 下按领域拆分。
 */
export {
  API_BASE_URL,
  DEFAULT_API_BASE_URL,
  DEFAULT_TIMEOUT_MS,
  getApiBaseUrl,
  http,
  requestJson,
} from './client'
export type { RequestOptions } from './client'
export { ApiError, isApiError, isRetryable, resolveErrorMessage } from './errors'
export type { ApiErrorInit } from './errors'
