import type { components } from './schema'

/**
 * API 传输层公共设施（规格 §10.3 / §18.2）。
 *
 * 为什么单开一个模块：几何域与参数域（`geometry.ts` / `params.ts`）都要「发请求 →
 * 校验响应形状 → 把 `{error:{...}}` 还原成异常」。这条链路有两份实现时，
 * 两边的错误文案与降级行为会各自演化——而 §10.3 要求 `suggestion` **处处必填且可见**。
 *
 * 本模块只做传输与**边界**校验，不含任何计算（ADR-011）。
 */

/** §10.3 的错误体（由 OpenAPI 生成，禁止手写重复类型）。 */
export type ErrorBody = components['schemas']['ErrorBody']

/**
 * §10.3 错误体的 JS 异常形态。
 *
 * `suggestion` 是契约必填项，UI 必须把它展示给用户（不得只显示 message）。
 */
export class ApiError extends Error {
  readonly code: string
  readonly stage: string
  readonly suggestion: string
  readonly details: Record<string, unknown> | undefined

  constructor(body: ErrorBody) {
    super(body.message)
    this.name = 'ApiError'
    this.code = body.code
    this.stage = body.stage
    this.suggestion = body.suggestion
    this.details = body.details
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

/** 收窄 §10.3 错误体；字段不全时返回 false（调用方退回通用文案）。 */
export function isErrorBody(value: unknown): value is ErrorBody {
  if (!isRecord(value)) return false
  return (
    typeof value.code === 'string' &&
    typeof value.stage === 'string' &&
    typeof value.message === 'string' &&
    typeof value.suggestion === 'string'
  )
}

/** 契约不符（响应形状与 OpenAPI 不一致）时抛出，避免下游拿着坏数据继续算。 */
export function contractError(path: string): ApiError {
  return new ApiError({
    code: 'CONTRACT_MISMATCH',
    stage: 'api',
    message: `响应不符合接口契约：${path}`,
    suggestion: '确认前端 schema.d.ts 与后端一致（重跑 npm run gen:api），并检查请求路径是否正确',
  })
}

/**
 * 把 `{error: {...}}` 还原成错误体；无法解析时退回可操作的通用文案。
 *
 * 前端一律以同源相对路径访问（开发期由 Vite 代理转发，生产期由桌面壳同源托管）。
 */
export async function readErrorBody(response: Response): Promise<ErrorBody> {
  try {
    const body: unknown = await response.json()
    if (isRecord(body) && isErrorBody(body.error)) return body.error
  } catch {
    // 错误体不是 JSON：退回通用文案，不吞掉 HTTP 状态码。
  }
  return {
    code: `HTTP_${response.status}`,
    stage: 'api',
    message: `请求失败：HTTP ${response.status}`,
    suggestion: '确认后端已启动（GET /api/health），必要时查看后端日志',
  }
}

async function sendJson<T>(
  method: 'POST' | 'PUT',
  path: string,
  body: unknown,
  isBody: (value: unknown) => value is T,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok) throw new ApiError(await readErrorBody(response))

  const parsed: unknown = await response.json()
  if (!isBody(parsed)) throw contractError(path)
  return parsed
}

export function postJson<T>(
  path: string,
  body: unknown,
  isBody: (value: unknown) => value is T,
  signal?: AbortSignal,
): Promise<T> {
  return sendJson('POST', path, body, isBody, signal)
}

export function putJson<T>(
  path: string,
  body: unknown,
  isBody: (value: unknown) => value is T,
): Promise<T> {
  return sendJson('PUT', path, body, isBody)
}

export async function getJson<T>(path: string, isBody: (value: unknown) => value is T): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw new ApiError(await readErrorBody(response))

  const parsed: unknown = await response.json()
  if (!isBody(parsed)) throw contractError(path)
  return parsed
}
