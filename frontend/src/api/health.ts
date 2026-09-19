import type { components } from './schema'

/**
 * 后端 `GET /api/health` 的响应类型。
 *
 * 由 OpenAPI schema 生成（`npm run gen:api`），禁止手写重复类型（规格 §18.2）。
 */
export type HealthResponse = components['schemas']['HealthResponse']

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Record<string, unknown>
  return (
    typeof candidate.status === 'string' &&
    typeof candidate.name === 'string' &&
    typeof candidate.version === 'string'
  )
}

/**
 * 查询后端存活状态。
 *
 * 前端一律以同源相对路径访问（开发期由 Vite 代理转发到 127.0.0.1:8000，见 vite.config.ts），
 * 本函数只做传输与边界校验，不参与任何计算（ADR-011）。
 */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch('/api/health', { signal })
  if (!response.ok) {
    throw new Error(`健康检查失败：HTTP ${response.status}`)
  }

  const body: unknown = await response.json()
  if (!isHealthResponse(body)) {
    throw new Error('健康检查响应不符合 HealthResponse 契约')
  }
  return body
}
