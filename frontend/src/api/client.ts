import axios, { type AxiosInstance, type AxiosResponse } from 'axios'

import { useSessionStore } from '@/stores/session'
import { ApiError, resolveErrorMessage } from './errors'

/**
 * 默认走 /api，由 Vite 开发代理或生产同源反向代理转发到 FastAPI。
 * 后端目前没有配置 CORS，因此不要在浏览器里直连 127.0.0.1:8000。
 */
export const DEFAULT_API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/+$/, '')

/** 兼容旧引用；实际请求请用 getApiBaseUrl()，它会读取设置里的动态地址。 */
export const API_BASE_URL = DEFAULT_API_BASE_URL

/** 当前生效的 API 前缀；设置面板修改后立即用于后续 JSON 与 SSE 请求。 */
export function getApiBaseUrl(): string {
  const configured = useSessionStore.getState().baseUrl.trim()
  return (configured || DEFAULT_API_BASE_URL).replace(/\/+$/, '')
}

/** 后端 /chat 的硬超时是 50s，这里留出网络与排队余量。 */
export const DEFAULT_TIMEOUT_MS = 60_000

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE'
  /** JSON 请求体，axios 会自动序列化并设置 Content-Type。 */
  body?: unknown
  /** URL 查询参数，值为 undefined / null 时由 axios 跳过。 */
  params?: Record<string, unknown>
  signal?: AbortSignal
  timeoutMs?: number
  headers?: Record<string, string>
}

/** 普通 JSON 接口共用这一个实例；SSE 不走 axios，见 P3 的 fetch 实现。 */
export const http: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: DEFAULT_TIMEOUT_MS,
  headers: { Accept: 'application/json' },
})

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function formatDetail(detail: unknown): string | null {
  if (typeof detail === 'string') {
    return detail.trim() || null
  }

  // FastAPI 422 的 detail 是 [{ loc, msg, type }]，这里只保留可读的 msg。
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item) => (isRecord(item) && typeof item.msg === 'string' ? item.msg : null))
      .filter((item): item is string => Boolean(item))

    return messages.length > 0 ? messages.join('；') : null
  }

  return null
}

function readHeader(response: AxiosResponse, name: string): string | null {
  const value = response.headers?.[name]
  return typeof value === 'string' && value ? value : null
}

function buildHttpError(response: AxiosResponse): ApiError {
  const payload: unknown = response.data
  const record = isRecord(payload) ? payload : null
  const code = typeof record?.error_code === 'string' ? record.error_code : `http_${response.status}`
  const detail = formatDetail(record?.detail)
  const requestId =
    (typeof record?.request_id === 'string' ? record.request_id : null) ??
    readHeader(response, 'x-request-id')

  return new ApiError({
    status: response.status,
    code,
    message: resolveErrorMessage(
      code,
      response.status,
      detail ?? response.statusText ?? `HTTP ${response.status}`,
    ),
    requestId,
    body: payload,
  })
}

/**
 * 把 axios 抛出的异常收敛成 ApiError，保证上层只处理一种错误类型。
 * 关键映射：ERR_CANCELED -> aborted / timeout，ECONNABORTED -> timeout，其余无响应 -> network_error。
 */
function toApiError(error: unknown, signal?: AbortSignal): ApiError {
  if (error instanceof ApiError) return error

  if (axios.isAxiosError(error)) {
    if (error.response) return buildHttpError(error.response)

    if (error.code === 'ERR_CANCELED') {
      return new ApiError({
        status: 0,
        // 外部 signal 主动取消与内部超时都会触发 cancel，用 signal 状态区分。
        code: signal?.aborted ? 'aborted' : 'timeout',
        body: error,
      })
    }

    if (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT') {
      return new ApiError({ status: 0, code: 'timeout', body: error })
    }

    return new ApiError({ status: 0, code: 'network_error', body: error })
  }

  return new ApiError({ status: 0, code: 'network_error', body: error })
}

/** 统一 JSON 请求入口；path 为后端相对路径（如 /chat），绝对 URL 会覆盖 baseURL。 */
export async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, params, signal, timeoutMs = DEFAULT_TIMEOUT_MS, headers } = options

  try {
    const response = await http.request<T>({
      baseURL: getApiBaseUrl(),
      url: path,
      method,
      data: body,
      params,
      signal,
      timeout: timeoutMs,
      headers,
    })

    return response.data
  } catch (error) {
    throw toApiError(error, signal)
  }
}
