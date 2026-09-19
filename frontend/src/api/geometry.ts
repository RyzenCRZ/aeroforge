import { ApiError, getJson, isRecord, postJson } from './client'
import type { components } from './schema'

// 传输层设施统一在 `./client`（几何域与参数域共用一套错误还原与契约校验）；
// 这里**转出** ApiError，使既有调用方（store / 测试）的 import 路径保持不变。
export { ApiError } from './client'
export type { ErrorBody } from './client'

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

/** `GET /api/artifacts/{key}/{file}` 的白名单文件名（§16.3 产物目录）。 */
export type ArtifactFile =
  | 'model.step'
  | 'model_lod1.glb'
  | 'model_lod2.glb'
  | 'metrics.json'
  | 'provenance.json'

function isNumberPair(value: unknown): value is [number, number] {
  return Array.isArray(value) && value.length === 2 && value.every((item) => typeof item === 'number')
}

function isNumberTriple(value: unknown): value is [number, number, number] {
  return Array.isArray(value) && value.length === 3 && value.every((item) => typeof item === 'number')
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
    Array.isArray(value.segment_outline) &&
    value.segment_outline.every(
      (chunk) => Array.isArray(chunk) && chunk.every(isNumberPair),
    ) &&
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

// 传输（postJson / getJson）与错误还原（readErrorBody / contractError）见 `./client`。

/**
 * 母线层校验（纯 Python，无内核）。
 *
 * 响应同时带 `sample` / `outline` 采样点，前端据此画 2D 剖面与示意网格——
 * `segment_outline` 是**逐段**轮廓（下标与 `profile.segments` 一一对应），
 * 供示意通道与权威通道的 `seg-<i>` 节点**同粒度**显隐（§11.4 / OI-33）。
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
 * WS 握手超时：超过此时长仍未 `open` 即视同失败，触发降级（§10.2）。
 *
 * 取值远大于本机握手的正常耗时（实测直连 < 10 ms），又远小于用户能忍受的"卡住"感知。
 */
export const JOB_HANDSHAKE_TIMEOUT_MS = 3_000

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
 * 构造失败会**同步抛出**，由调用方（store）降级为轮询；连接建立后的传输异常走 `onError`；
 * 握手阶段卡住则走下面的**握手超时**兜底。
 */
export function openJobStream(jobId: string, handlers: JobStreamHandlers): JobStream {
  const socket = new WebSocket(jobStreamUrl(jobId))
  let closed = false
  let handshaken = false
  let handshakeTimer: number | null = null

  const clearHandshakeTimer = (): void => {
    if (handshakeTimer !== null) {
      window.clearTimeout(handshakeTimer)
      handshakeTimer = null
    }
  }

  /** 放弃当前 socket（幂等）：清定时器、摘回调、关闭连接，避免迟到事件回流。 */
  const abandon = (): void => {
    closed = true
    clearHandshakeTimer()
    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
    socket.close()
  }

  // 握手兜底（§10.2）：连接建立阶段的失败**既不派发 error 也不派发 close**——实测当反向
  // 代理未转发 WS 升级请求（例如 Vite 缺 /ws 代理）时，socket 会无限停在 CONNECTING
  // （readyState = 0）。只依赖 onerror/onclose 的话，降级轮询永不启动，界面会静默卡在
  // "排队中（0%）"，而后端作业其实早已跑完。这类"既不成功也不失败"的挂起必须显式兜住。
  handshakeTimer = window.setTimeout(() => {
    if (closed || handshaken) return
    abandon()
    handlers.onError?.(
      new ApiError({
        code: 'JOB_STREAM_HANDSHAKE_TIMEOUT',
        stage: 'api',
        message: `作业 ${jobId} 的进度通道在 ${JOB_HANDSHAKE_TIMEOUT_MS} ms 内未建立`,
        suggestion:
          '已降级为轮询 GET /api/jobs/{job_id}；开发态若持续出现，检查 Vite 代理是否转发了 /ws（server.proxy 需含 ws: true）',
      }),
    )
  }, JOB_HANDSHAKE_TIMEOUT_MS)

  socket.onopen = () => {
    handshaken = true
    clearHandshakeTimer()
  }

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
    clearHandshakeTimer()
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
    clearHandshakeTimer()
    handlers.onClose?.()
  }

  return {
    close: () => {
      if (closed) return
      abandon()
    },
  }
}
