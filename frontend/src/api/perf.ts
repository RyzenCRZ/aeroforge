import { ApiError, isRecord, postJson } from './client'
import { fetchJob, openJobStream, type JobRecord, type JobStream } from './geometry'
import type { Mission, Vehicle } from './params'
import type { components } from './schema'

/**
 * 性能评估域 API（规格 §8.6–§8.8 / §10.1 / OI-23 / OI-25 两阶段契约）。
 *
 * - `POST /api/perf/evaluate` —— 阶段①：四轨道点值运力表 + ΔV 瀑布，毫秒级同步返回；
 *   `mc=true`（默认）时响应带 `interval_pending=true` 与 `mc_job_id`（自动投递的阶段②作业挂点）。
 * - 阶段②结果**经既有作业通道**取回（`GET /api/jobs/{id}` / `WS /ws/jobs/{id}`，
 *   `JobRecord.metrics` 即 MC 结果载荷）——见 `fetchMcResult`。
 *
 * 类型一律取自 OpenAPI 生成物（§18.2：后端类型禁止手写重复定义）；本模块只做传输与
 * **边界**校验，不含任何物理计算（ADR-011）。
 */

export type PerfEvaluateRequest = components['schemas']['PerfEvaluateRequest']
export type PerfEvaluateResponse = components['schemas']['PerfEvaluateResponse']
export type PointEvaluation = components['schemas']['PointEvaluation']
export type OrbitPayload = components['schemas']['OrbitPayload']
export type DeltaVBudget = components['schemas']['DeltaVBudget']

// ---------------------------------------------------------------------------
// MC 结果（阶段②载荷）：手写类型 + 运行时守卫
// ---------------------------------------------------------------------------

/**
 * 一个量的 P5 / P50 / P95 区间（§8.7 输出契约，kg）。
 *
 * ⚠ 这组类型**不在 OpenAPI 生成物里**：MC 结果经 `JobRecord.metrics`（索引签名
 * `Record<string, unknown>`）下发，OpenAPI 描述不了它的内部结构——§18.2 禁止的是
 * 「后端已有类型却手写重复」，此处是 OpenAPI 覆盖不到的部分，故按契约（§8.7 /
 * OI-25 定死的形态）手写，并配运行时守卫补回边界校验。
 */
export interface McIntervalRow {
  p5: number
  p50: number
  p95: number
}

/** 一个量的分布形态指标（§8.7 末 / OI-01：n 与 estimator 必须同时声明）。 */
export interface McMomentsRow {
  n: number
  estimator: string
  skewness: number | null
  skewness_null_reason: string | null
  /** |G₁|>0.5 时非空：均值 ≠ P50，不得把 P50 当期望值使用（§8.7）。 */
  mean_vs_p50_note: string | null
}

/** 一个量的直方图（bin 数固定 50：边界 51 个 + 计数 50 个，由后端保证）。 */
export interface McHistogramRow {
  bin_edges: number[]
  counts: number[]
}

/** 一个量的 P50 收敛诊断（前 1k/2k/5k/10k 检查点，§8.7）。 */
export interface McConvergenceRow {
  checkpoints: number[]
  p50: number[]
  drift_vs_final: number[]
}

/** 一个参数的一阶差分敏感度（Top-8 成员，非 Sobol）。 */
export interface McSensitivityItem {
  param: string
  impact: number
}

/** MC 作业结果（阶段②到达后的整体覆盖载荷，`metrics` 形态）。 */
export interface McResult {
  interval: Record<string, McIntervalRow>
  moments: Record<string, McMomentsRow>
  histogram: Record<string, McHistogramRow>
  convergence: Record<string, McConvergenceRow>
  sensitivity: McSensitivityItem[]
  provenance: Record<string, string>
  /** OI-25：阶段②到达后必须为 false（「区间是占位态还是终态」的唯一判据）。 */
  interval_pending: false
  mc_job_id: string
}

function isNumberArray(value: unknown): value is number[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'number')
}

function isMcIntervalRow(value: unknown): value is McIntervalRow {
  if (!isRecord(value)) return false
  return (
    typeof value.p5 === 'number' && typeof value.p50 === 'number' && typeof value.p95 === 'number'
  )
}

function isMcMomentsRow(value: unknown): value is McMomentsRow {
  if (!isRecord(value)) return false
  return (
    typeof value.n === 'number' &&
    typeof value.estimator === 'string' &&
    (value.skewness === null || typeof value.skewness === 'number') &&
    (value.skewness_null_reason === null ||
      value.skewness_null_reason === undefined ||
      typeof value.skewness_null_reason === 'string') &&
    (value.mean_vs_p50_note === null ||
      value.mean_vs_p50_note === undefined ||
      typeof value.mean_vs_p50_note === 'string')
  )
}

function isMcHistogramRow(value: unknown): value is McHistogramRow {
  if (!isRecord(value)) return false
  return isNumberArray(value.bin_edges) && isNumberArray(value.counts)
}

function isMcConvergenceRow(value: unknown): value is McConvergenceRow {
  if (!isRecord(value)) return false
  return (
    isNumberArray(value.checkpoints) &&
    isNumberArray(value.p50) &&
    isNumberArray(value.drift_vs_final)
  )
}

function isMcSensitivityItem(value: unknown): value is McSensitivityItem {
  if (!isRecord(value)) return false
  return typeof value.param === 'string' && typeof value.impact === 'number'
}

function isMcResult(value: unknown): value is McResult {
  if (!isRecord(value)) return false
  return (
    isRecord(value.interval) &&
    Object.values(value.interval).every(isMcIntervalRow) &&
    isRecord(value.moments) &&
    Object.values(value.moments).every(isMcMomentsRow) &&
    isRecord(value.histogram) &&
    Object.values(value.histogram).every(isMcHistogramRow) &&
    isRecord(value.convergence) &&
    Object.values(value.convergence).every(isMcConvergenceRow) &&
    Array.isArray(value.sensitivity) &&
    value.sensitivity.every(isMcSensitivityItem) &&
    isRecord(value.provenance) &&
    Object.values(value.provenance).every((item) => typeof item === 'string') &&
    value.interval_pending === false &&
    typeof value.mc_job_id === 'string'
  )
}

// ---------------------------------------------------------------------------
// 阶段①：POST /api/perf/evaluate
// ---------------------------------------------------------------------------

function isOrbitPayload(value: unknown): value is OrbitPayload {
  if (!isRecord(value)) return false
  return (
    typeof value.payload_kg === 'number' &&
    typeof value.dv_used_km_s === 'number' &&
    typeof value.dv_source === 'string' &&
    (value.attainable === undefined || typeof value.attainable === 'boolean')
  )
}

function isDeltaVBudget(value: unknown): value is DeltaVBudget {
  if (!isRecord(value)) return false
  return (
    typeof value.target_orbit === 'string' &&
    typeof value.ideal_dv_km_s === 'number' &&
    typeof value.gravity_loss_km_s === 'number' &&
    typeof value.aero_loss_km_s === 'number' &&
    typeof value.steering_loss_km_s === 'number' &&
    typeof value.back_pressure_loss_km_s === 'number' &&
    typeof value.rotation_assist_km_s === 'number' &&
    typeof value.total_dv_km_s === 'number' &&
    Array.isArray(value.assumptions) &&
    value.assumptions.every((item) => typeof item === 'string')
  )
}

function isPerfEvaluateResponse(value: unknown): value is PerfEvaluateResponse {
  if (!isRecord(value)) return false
  const point = value.point
  if (!isRecord(point)) return false
  return (
    isRecord(point.payload_by_orbit) &&
    Object.values(point.payload_by_orbit).every(isOrbitPayload) &&
    typeof point.payload_mass_kg === 'number' &&
    typeof point.glow_kg === 'number' &&
    (point.c3_km2_s2 === null ||
      point.c3_km2_s2 === undefined ||
      typeof point.c3_km2_s2 === 'number') &&
    isDeltaVBudget(value.delta_v_budget) &&
    Array.isArray(value.warnings) &&
    value.warnings.every((item) => typeof item === 'string') &&
    isRecord(value.provenance) &&
    Object.values(value.provenance).every((item) => typeof item === 'string') &&
    typeof value.cache_hit === 'boolean' &&
    typeof value.interval_pending === 'boolean' &&
    (value.mc_job_id === null || value.mc_job_id === undefined || typeof value.mc_job_id === 'string')
  )
}

/**
 * 阶段①点值评估（§8.7 OI-25：同步纯数值，不进 MC）。
 *
 * @param vehicle 飞行器参数（Mission 内嵌：目标轨道 / 倾角 / 发射场是 ΔV 的唯一输入）
 * @param mission 任务覆写（给出时替换 `vehicle.mission` 再发送——后端请求体
 *   `extra="forbid"` 且只有 `vehicle` / `mc` 两个键，覆写必须在前端边界完成合并）
 * @param mc 是否自动投递阶段②的 MC 区间作业（默认 true）
 */
export function evaluatePerformance(
  vehicle: Vehicle,
  mission?: Mission,
  mc: boolean = true,
): Promise<PerfEvaluateResponse> {
  const body: PerfEvaluateRequest = {
    vehicle: mission === undefined ? vehicle : { ...vehicle, mission },
    mc,
  }
  return postJson('/api/perf/evaluate', body, isPerfEvaluateResponse)
}

// ---------------------------------------------------------------------------
// 阶段②：经既有作业通道取回 MC 结果
// ---------------------------------------------------------------------------

/**
 * 作业轮询间隔（§10.2 降级路径）：与 `store/model` 的 `JOB_POLL_INTERVAL_MS` 同值同源
 * （同一份降级口径，只是该常量住在 store 层——api 层不反向依赖 store，故在此重申）。
 */
export const MC_POLL_INTERVAL_MS = 500

/**
 * MC 结果取回的总超时：后端 10 000 样本是秒级（§8.7），两分钟仍未终态即视为异常
 * （作业挂死 / 通道双断），必须显式兜底——「既不成功也不失败」的悬置是 M2 握手
 * 教训的同族形态（只依赖 onerror/onclose 的话，降级轮询也可能永远等不到终态）。
 */
export const MC_RESULT_TIMEOUT_MS = 120_000

function isTerminal(record: JobRecord): boolean {
  return (
    record.status === 'succeeded' || record.status === 'failed' || record.status === 'cancelled'
  )
}

/** 作业终态但非成功（取消 / 失败）时组装 §10.3 错误体；后端未附原因时给出可操作文案。 */
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
    message: record.status === 'cancelled' ? 'MC 区间作业已取消' : 'MC 区间作业失败（后端未附详细原因）',
    suggestion: '点击重试重新取回区间；若反复失败请查看后端日志中的 MC 作业阶段耗时',
  })
}

/**
 * 取回 MC 区间作业结果（阶段②，经既有作业通道）。
 *
 * 形态与 `store/model` 的作业订阅同款：**WS 优先**（`openJobStream`，握手悬置由其内置
 * 的 3 s 超时兜底）→ 传输异常 / 非终态关闭时**降级轮询** `GET /api/jobs/{id}`；另有
 * 总超时兜底。终态后 `metrics` 经 `isMcResult` 守卫还原为 `McResult`。
 *
 * 消费方（store）以「代数 token」防迟到覆盖：新评估发起后，旧作业的迟到结果被丢弃。
 */
export function fetchMcResult(jobId: string): Promise<McResult> {
  return new Promise<McResult>((resolve, reject) => {
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
      if (record.status === 'succeeded') {
        const metrics = record.metrics ?? null
        if (isMcResult(metrics)) {
          settle(() => resolve(metrics))
          return
        }
        fail(
          new ApiError({
            code: 'CONTRACT_MISMATCH',
            stage: 'api',
            message: `作业 ${jobId} 的 metrics 不符合 MC 结果契约（OI-25 阶段②）`,
            suggestion:
              '确认前端与后端契约一致（重跑 npm run gen:api），并检查后端 MC 作业的 metrics 组装',
          }),
        )
        return
      }
      fail(jobFailureError(record))
    }

    const startPolling = (): void => {
      if (pollTimer !== null) return // 已在轮询，不叠第二份定时器
      const poll = (): void => {
        fetchJob(jobId)
          .then(applyRecord)
          .catch((error: unknown) => {
            fail(error instanceof Error ? error : new Error(String(error)))
          })
      }
      poll() // 降级立即查一次，再按间隔轮询（§10.2）
      pollTimer = window.setInterval(poll, MC_POLL_INTERVAL_MS)
    }

    timeoutTimer = window.setTimeout(() => {
      fail(
        new ApiError({
          code: 'MC_RESULT_TIMEOUT',
          stage: 'api',
          message: `MC 区间作业 ${jobId} 在 ${MC_RESULT_TIMEOUT_MS / 1000} s 内未完成`,
          suggestion: '点击重试重新取回区间；若持续超时请查看后端计算侧（进程池）日志',
        }),
      )
    }, MC_RESULT_TIMEOUT_MS)

    try {
      stream = openJobStream(jobId, {
        onRecord: applyRecord,
        onError: () => {
          stream?.close()
          stream = null
          startPolling()
        },
        onClose: () => {
          // 终态由 applyRecord 收线；非终态关闭（后端重启等）必须降级，否则区间会静默卡占位态
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
