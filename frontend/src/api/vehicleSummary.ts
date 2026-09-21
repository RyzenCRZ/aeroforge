import { ApiError, isRecord, postJson, readErrorBody } from './client'
import { fetchJob, openJobStream, type JobRecord, type JobStream } from './geometry'
import type { Vehicle } from './params'

/**
 * 整箭数据与报告导出域 API（FR-10 / FR-11，M5 第三片）。
 *
 * - `POST /api/vehicle/summary` —— 整箭数据（质量 / 几何七字段 + 轨道运力，FR-10/FR-11）；
 * - `POST /api/export` —— 报告导出**异步作业**（复用既有作业通道：WS 优先、降级轮询），
 *   完成后经 `/api/artifacts/{key}/{file}` 取回 blob（§9.3 / §11.12 规则 4）。
 *
 * ⚠ 类型是**手写 + 运行时守卫**：后端并行开发中，OpenAPI 生成物就绪后切生成物
 * （与 `perf.ts` 的 MC 结果同族处理——§18.2 禁止的是"后端已有类型却手写重复"，
 * 此处 OpenAPI 尚未覆盖）。本模块只做传输与边界校验，不含任何物理计算（ADR-011）。
 */

/** 一条轨道的运力点值（`payload_by_orbit` 的表项；其余字段后端可增，前端不假定）。 */
export interface OrbitPayloadPoint {
  payload_kg: number
  [key: string]: unknown
}

/** 整箭数据响应（FR-10 七字段 + FR-11 轨道运力；数值一律后端下发，前端只格式化）。 */
export interface VehicleSummaryResponse {
  /** 起飞总质量（kg，SI） */
  glow_kg: number
  /** 起飞总质量（t）——后端在 API 边界换算好的双显示值（§6.4），前端不再换算 */
  glow_t: number
  propellant_total_kg: number
  dry_mass_kg: number
  total_length_m: number
  max_diameter_m: number
  /** 无整流罩构型为 null */
  fairing_diameter_m: number | null
  orbit: {
    /** 当前 Mission 的目标轨道名（如 LEO） */
    target: string
    /** 当前目标轨道的运力点值（kg） */
    payload_kg: number
    /** 四轨道小表：键为轨道名（LEO / SSO / GTO / GEO） */
    payload_by_orbit: Record<string, OrbitPayloadPoint>
  }
  warnings: string[]
  /** 来源标注（CON-04）：导出报告必须附 provenance（§11.12 规则 2） */
  provenance: Record<string, string>
}

function isOrbitPayloadPoint(value: unknown): value is OrbitPayloadPoint {
  return isRecord(value) && typeof value.payload_kg === 'number'
}

function isVehicleSummary(value: unknown): value is VehicleSummaryResponse {
  if (!isRecord(value)) return false
  const orbit = value.orbit
  if (!isRecord(orbit)) return false
  return (
    typeof value.glow_kg === 'number' &&
    typeof value.glow_t === 'number' &&
    typeof value.propellant_total_kg === 'number' &&
    typeof value.dry_mass_kg === 'number' &&
    typeof value.total_length_m === 'number' &&
    typeof value.max_diameter_m === 'number' &&
    (value.fairing_diameter_m === null || typeof value.fairing_diameter_m === 'number') &&
    typeof orbit.target === 'string' &&
    typeof orbit.payload_kg === 'number' &&
    isRecord(orbit.payload_by_orbit) &&
    Object.values(orbit.payload_by_orbit).every(isOrbitPayloadPoint) &&
    Array.isArray(value.warnings) &&
    value.warnings.every((item) => typeof item === 'string') &&
    isRecord(value.provenance) &&
    Object.values(value.provenance).every((item) => typeof item === 'string')
  )
}

/**
 * 请求整箭数据（面板挂载 / vehicle 变更后经 300 ms 防抖调用，OI-04 冻结时由调用方停发）。
 */
export function fetchVehicleSummary(
  vehicle: Vehicle,
  signal?: AbortSignal,
): Promise<VehicleSummaryResponse> {
  return postJson('/api/vehicle/summary', { vehicle }, isVehicleSummary, signal)
}

/** 报告导出格式（FR-11「导出报告」：至少这三种产物）。 */
export const REPORT_EXPORT_FORMATS = ['params_json', 'mass_csv', 'perf_json'] as const

/** `POST /api/export` 的响应：异步作业挂点 + 格式→产物文件名映射（后端单一下发）。 */
export interface ExportStartResponse {
  job_id: string
  formats: string[]
  /** 格式 → 产物文件名（/api/artifacts/{key}/{file} 的 file 段；命名事实源在后端）。 */
  files: Record<string, string>
}

function isExportStartResponse(value: unknown): value is ExportStartResponse {
  if (!isRecord(value)) return false
  const files = value.files
  return (
    typeof value.job_id === 'string' &&
    Array.isArray(value.formats) &&
    value.formats.every((item) => typeof item === 'string') &&
    isRecord(files) &&
    Object.entries(files).every(
      ([key, val]) => typeof key === 'string' && typeof val === 'string',
    )
  )
}

/**
 * 投递报告导出作业（§11.12 规则 4：几何与报告导出走异步作业，带进度提示）。
 *
 * 契约：响应 `files` 下发「格式 → 产物文件名」映射（命名单一事实源在后端
 * cache.store 的 ARTIFACT_EXPORT_*），前端按映射下载，**禁止自行拼接文件名**
 * （ADR-011 同族纪律——前端零命名知识）。
 */
export function startReportExport(
  vehicle: Vehicle,
  formats: readonly string[],
): Promise<ExportStartResponse> {
  return postJson('/api/export', { vehicle, formats: [...formats] }, isExportStartResponse)
}

// ---------------------------------------------------------------------------
// 作业通道（复用既有形态：WS 优先 → 异常降级轮询 → 总超时兜底，§10.2 / §9.3）
// ---------------------------------------------------------------------------

/** WS 不可用时轮询 `GET /api/jobs/{id}` 的间隔（与 store/model 的 JOB_POLL_INTERVAL_MS 同值同源）。 */
export const EXPORT_POLL_INTERVAL_MS = 500

/** 取回导出作业终态的总超时（两分钟仍未终态即显式兜底，不得静默挂起）。 */
export const EXPORT_RESULT_TIMEOUT_MS = 120_000

function isTerminal(record: JobRecord): boolean {
  return (
    record.status === 'succeeded' || record.status === 'failed' || record.status === 'cancelled'
  )
}

function jobFailureError(record: JobRecord): ApiError {
  if (record.error != null) {
    return new ApiError({
      code: record.error.code,
      stage: record.error.stage,
      message: record.error.message,
      suggestion: record.error.suggestion,
    })
  }
  return new ApiError({
    code: record.status === 'cancelled' ? 'JOB_CANCELLED' : 'JOB_FAILED',
    stage: record.stage,
    message:
      record.status === 'cancelled' ? '报告导出作业已取消' : '报告导出作业失败（后端未附详细原因）',
    suggestion: '重试导出；若反复失败请查看后端日志中的导出作业阶段耗时',
  })
}

/**
 * 等待导出作业终态并返回**产物键**（`result_key`）。
 *
 * 形态与 `store/model` 的作业订阅 / `perf.ts` 的 `fetchMcResult` 同款：WS 优先（握手
 * 悬置由其内置超时兜底）→ 传输异常 / 非终态关闭时降级轮询 → 总超时兜底。
 */
export function waitForExportJob(jobId: string): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    let settled = false
    let stream: JobStream | null = null
    let pollTimer: number | null = null
    let timeoutTimer: number | null = null

    const stopPolling = (): void => {
      if (pollTimer !== null) {
        window.clearInterval(pollTimer)
        pollTimer = null
      }
    }

    const cleanup = (): void => {
      stopPolling()
      if (timeoutTimer !== null) {
        window.clearTimeout(timeoutTimer)
        timeoutTimer = null
      }
      if (stream !== null) {
        stream.close()
        stream = null
      }
    }

    const settle = (settleFn: () => void): void => {
      if (settled) return
      settled = true
      cleanup()
      settleFn()
    }

    const fail = (error: ApiError | Error): void => {
      settle(() => reject(error))
    }

    const applyRecord = (record: JobRecord): void => {
      if (!isTerminal(record)) return
      if (record.status !== 'succeeded') {
        fail(jobFailureError(record))
        return
      }
      const resultKey = record.result_key ?? null
      if (typeof resultKey !== 'string' || resultKey === '') {
        fail(
          new ApiError({
            code: 'CONTRACT_MISMATCH',
            stage: 'api',
            message: `导出作业 ${jobId} 成功终态但未返回 result_key`,
            suggestion: '重试导出；若持续出现请确认后端导出作业的产物键组装',
          }),
        )
        return
      }
      settle(() => resolve(resultKey))
    }

    const startPolling = (): void => {
      if (pollTimer !== null) return
      const poll = (): void => {
        fetchJob(jobId)
          .then(applyRecord)
          .catch((error: unknown) => {
            fail(error instanceof Error ? error : new Error(String(error)))
          })
      }
      poll() // 降级立即查一次，再按间隔轮询（§10.2）
      pollTimer = window.setInterval(poll, EXPORT_POLL_INTERVAL_MS)
    }

    timeoutTimer = window.setTimeout(() => {
      fail(
        new ApiError({
          code: 'EXPORT_RESULT_TIMEOUT',
          stage: 'api',
          message: `报告导出作业 ${jobId} 在 ${EXPORT_RESULT_TIMEOUT_MS / 1000} s 内未完成`,
          suggestion: '重试导出；若持续超时请查看后端导出侧（产物落盘 / 打包）日志',
        }),
      )
    }, EXPORT_RESULT_TIMEOUT_MS)

    try {
      stream = openJobStream(jobId, {
        onRecord: applyRecord,
        onError: () => {
          stream?.close()
          stream = null
          startPolling()
        },
        onClose: () => {
          // 终态由 applyRecord 收线；非终态关闭（后端重启等）必须降级，否则静默卡"导出中"
          if (!settled) startPolling()
        },
      })
    } catch {
      // WS 构造同步抛出（jsdom 无 WebSocket / 环境不支持）：降级轮询
      stream = null
      startPolling()
    }
  })
}

/** 导出产物地址（与几何产物同一目录约定：`/api/artifacts/{key}/{file}`）。 */
export function exportArtifactUrl(key: string, file: string): string {
  return `/api/artifacts/${encodeURIComponent(key)}/${encodeURIComponent(file)}`
}

/** 取回导出产物内容（blob 下载用；非 2xx 按 §10.3 还原错误体）。 */
export async function fetchArtifactBlob(url: string): Promise<Blob> {
  const response = await fetch(url)
  if (!response.ok) throw new ApiError(await readErrorBody(response))
  return response.blob()
}
