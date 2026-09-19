import type { components } from './schema'

/**
 * 几何 API 的契约类型。
 *
 * 一律从 OpenAPI 生成物 `schema.d.ts` 取（规格 §18.2：后端类型禁止手写重复定义）。
 */
export type MeridianProfile = components['schemas']['MeridianProfile']
/** 母线段（line / arc / ellipse 三选一），见 §16.3 的 M1 曲线族裁剪。 */
export type MeridianSegment = MeridianProfile['segments'][number]
export type SegmentType = MeridianSegment['type']
export type ValidationReport = components['schemas']['ValidationReport']
export type CheckResult = components['schemas']['CheckResult']
export type JointCheck = components['schemas']['JointCheck']
export type BuildResponse = components['schemas']['BuildResponse']
export type JobRecord = components['schemas']['JobRecord']
export type JobStage = JobRecord['stage']
export type JobStatus = JobRecord['status']
export type ContourResponse = components['schemas']['ContourResponse']
export type ErrorBody = components['schemas']['ErrorBody']

/** `GET /api/artifacts/{key}/{file}` 的白名单文件名（§16.3 产物目录）。 */
export type ArtifactFile =
  | 'model.step'
  | 'model_lod1.glb'
  | 'model_lod2.glb'
  | 'metrics.json'
  | 'provenance.json'

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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isNumberPair(value: unknown): value is [number, number] {
  return Array.isArray(value) && value.length === 2 && value.every((item) => typeof item === 'number')
}

function isNumberTriple(value: unknown): value is [number, number, number] {
  return Array.isArray(value) && value.length === 3 && value.every((item) => typeof item === 'number')
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

function isCheckResult(value: unknown): boolean {
  if (!isRecord(value)) return false
  return typeof value.check === 'string' && typeof value.severity === 'string' && typeof value.detail === 'string'
}

function isJointCheck(value: unknown): boolean {
  if (!isRecord(value)) return false
  return (
    typeof value.index === 'number' &&
    typeof value.z === 'number' &&
    typeof value.radius === 'number' &&
    typeof value.angle_deg === 'number' &&
    (value.kind === 'g1' || value.kind === 'pinch') &&
    typeof value.severity === 'string'
  )
}

function isValidationReport(value: unknown): value is ValidationReport {
  if (!isRecord(value)) return false
  return (
    typeof value.ok === 'boolean' &&
    Array.isArray(value.checks) &&
    value.checks.every(isCheckResult) &&
    Array.isArray(value.joints) &&
    value.joints.every(isJointCheck) &&
    Array.isArray(value.sample) &&
    value.sample.every(isNumberPair) &&
    Array.isArray(value.outline) &&
    value.outline.every(isNumberPair) &&
    isNumberTriple(value.envelope) &&
    typeof value.volume === 'number' &&
    typeof value.surface_area === 'number' &&
    typeof value.centroid_z === 'number' &&
    typeof value.max_radius === 'number' &&
    typeof value.total_length === 'number'
  )
}

function isBuildResponse(value: unknown): value is BuildResponse {
  if (!isRecord(value)) return false
  return (
    typeof value.cache_hit === 'boolean' &&
    typeof value.key === 'string' &&
    (value.job_id === undefined || value.job_id === null || typeof value.job_id === 'string') &&
    (value.metrics === undefined || value.metrics === null || isRecord(value.metrics))
  )
}

function isJobRecord(value: unknown): value is JobRecord {
  if (!isRecord(value)) return false
  const status: unknown = value.status
  return (
    typeof value.job_id === 'string' &&
    (status === 'queued' ||
      status === 'running' ||
      status === 'succeeded' ||
      status === 'failed' ||
      status === 'cancelled') &&
    typeof value.stage === 'string' &&
    typeof value.progress === 'number' &&
    typeof value.created_at === 'string'
  )
}

function isContourResponse(value: unknown): value is ContourResponse {
  if (!isRecord(value)) return false
  return typeof value.id === 'string' && typeof value.canonical === 'string' && isRecord(value.profile)
}

/** 契约不符（响应形状与 OpenAPI 不一致）时抛出，避免下游拿着坏数据继续算。 */
function contractError(path: string): ApiError {
  return new ApiError({
    code: 'CONTRACT_MISMATCH',
    stage: 'api',
    message: `响应不符合接口契约：${path}`,
    suggestion: '确认前端 schema.d.ts 与后端一致（重跑 npm run gen:api），并检查请求路径是否正确',
  })
}

/**
 * 把 `{error: {...}}` 还原成异常；无法解析时退回可操作的通用文案。
 *
 * 前端一律以同源相对路径访问（开发期由 Vite 代理转发，生产期由桌面壳同源托管），
 * 本模块只做传输与边界校验，不参与任何计算（ADR-011）。
 */
async function readErrorBody(response: Response): Promise<ErrorBody> {
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

async function postJson<T>(
  path: string,
  body: unknown,
  isBody: (value: unknown) => value is T,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok) throw new ApiError(await readErrorBody(response))

  const parsed: unknown = await response.json()
  if (!isBody(parsed)) throw contractError(path)
  return parsed
}

async function getJson<T>(path: string, isBody: (value: unknown) => value is T): Promise<T> {
  const response = await fetch(path)
  if (!response.ok) throw new ApiError(await readErrorBody(response))

  const parsed: unknown = await response.json()
  if (!isBody(parsed)) throw contractError(path)
  return parsed
}

/**
 * 母线层校验（纯 Python，无内核）。
 *
 * 响应同时带 `sample` / `outline` 采样点，前端据此画 2D 剖面与示意网格——
 * 端点数值全部来自后端，前端不做任何几何计算（ADR-011）。
 */
export function validateProfile(profile: MeridianProfile, signal?: AbortSignal): Promise<ValidationReport> {
  return postJson('/api/geometry/validate', profile, isValidationReport, signal)
}

/** 构建回转几何：命中缓存同步返回，否则返回 job_id 供订阅进度（§9.3）。 */
export function buildGeometry(profile: MeridianProfile): Promise<BuildResponse> {
  return postJson('/api/geometry/build', profile, isBuildResponse)
}

/** 查询作业快照（WS 不可用时的降级路径，§10.2）。 */
export function fetchJob(jobId: string): Promise<JobRecord> {
  return getJson(`/api/jobs/${encodeURIComponent(jobId)}`, isJobRecord)
}

/** 保存母线：返回 canonical JSON，往返一致的判据（§16.3 验收项 2）。 */
export function saveContour(id: string | null, profile: MeridianProfile): Promise<ContourResponse> {
  return postJson('/api/geometry/contour', { id, profile }, isContourResponse)
}

/** 载入母线。 */
export function loadContour(id: string): Promise<ContourResponse> {
  return getJson(`/api/geometry/contour/${encodeURIComponent(id)}`, isContourResponse)
}

/**
 * 产物下载地址（同源相对路径）。
 *
 * 只做字符串拼接：不探测主机、不写端口——交付形态是桌面壳同源托管（ADR-015）。
 */
export function artifactUrl(key: string, file: ArtifactFile): string {
  return `/api/artifacts/${encodeURIComponent(key)}/${file}`
}

export interface JobStreamHandlers {
  /** 收到一条作业快照；可能与上一帧重复（服务端订阅与快照存在重叠），按"最新到达者胜"渲染。 */
  onRecord: (record: JobRecord) => void
  /** 传输异常（含无法建立连接）；调用方据此降级为轮询，不得静默卡住。 */
  onError?: (error: ApiError) => void
  /** 通道关闭（含终态后的正常关闭）。 */
  onClose?: () => void
}

export interface JobStream {
  close: () => void
}

/**
 * WS 地址：由当前页面协议与主机推导（同源），禁止硬编码主机名或端口。
 *
 * 桌面壳（pywebview）与 Vite dev server 的端口都不同，写死任一端口都会使另一形态失效。
 */
function jobStreamUrl(jobId: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/jobs/${encodeURIComponent(jobId)}`
}

/**
 * 订阅作业进度流。
 *
 * 构造失败会**同步抛出**，由调用方（store）降级为轮询；连接建立后的传输异常走 `onError`。
 */
export function openJobStream(jobId: string, handlers: JobStreamHandlers): JobStream {
  const socket = new WebSocket(jobStreamUrl(jobId))
  let closed = false

  socket.onmessage = (event: MessageEvent) => {
    let parsed: unknown
    try {
      parsed = JSON.parse(String(event.data))
    } catch {
      return
    }
    if (isJobRecord(parsed)) handlers.onRecord(parsed)
  }

  socket.onerror = () => {
    handlers.onError?.(
      new ApiError({
        code: 'JOB_STREAM_ERROR',
        stage: 'api',
        message: `作业 ${jobId} 的进度通道异常`,
        suggestion: '已降级为轮询 GET /api/jobs/{job_id}；若持续失败请检查后端作业执行器与日志',
      }),
    )
  }

  socket.onclose = () => {
    handlers.onClose?.()
  }

  return {
    close: () => {
      if (closed) return
      closed = true
      socket.onmessage = null
      socket.onerror = null
      socket.onclose = null
      socket.close()
    },
  }
}
