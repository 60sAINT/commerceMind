import { requestJson } from '@/api'
import type { HealthResponse } from '@/types/api'

interface HealthOptions {
  signal?: AbortSignal
  timeoutMs?: number
}

/** GET /health：不依赖模型 Key，可用于启动页与连接状态。 */
export function getHealth(options: HealthOptions = {}): Promise<HealthResponse> {
  return requestJson<HealthResponse>('/health', {
    signal: options.signal,
    timeoutMs: options.timeoutMs ?? 10_000,
  })
}
