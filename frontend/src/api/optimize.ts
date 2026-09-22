import { ApiError, isRecord, postJson } from './client'
import { fetchJob, openJobStream, type JobRecord, type JobStream } from './geometry'
import type { Vehicle } from './params'
import type { components } from './schema'

/**
 * 优化与权衡研究域 API（规格 §14，M6 缺项补做的前端片）。
 *
 * - `POST /api/optimize/nsga2`       —— 多目标优化（NSGA-II，异步作业）
 * - `POST /api/optimize/trade-study` —— 权衡研究（≤ 20 方案，异步作业）
 * - `POST /api/optimize/sweep`       —— 批量扫描（≤ 3 轴 / ≤ 10⁴ 组合，异步作业）
 * - `POST /api/optimize/inverse`     —— 逆向设计（给定运力求最小构型，异步作业）
 *
 * 四个端点**全部是异步作业**，复用既有作业通道（`GET /api/jobs/{id}` /
 * `WS /ws/jobs/{id}`，§10.2）：WS 优先 → 降级轮询 → 总超时兜底，结果经
 * `JobRecord.metrics` 下发（与 `perf.ts` 的 MC 结果同族形态）。
 *
 * 类型分两层（§18.2：后端已有类型禁止手写重复）：
 *
 * 1. **请求体 / 回执已切 OpenAPI 生成物**（`Nsga2Request` / `TradeStudyRequest` /
 *    `SweepRequest` / `InverseRequest` / `OptimizeVariableSpec` / `SweepAxisSpec` /
 *    `OptimizeJobResponse`）——后端契约重导完成，键名与原手写形态零偏离；
 *    注意四个请求体都带 `dv_supply`（生成物 default "anchored"），前端显式下发。
 * 2. **结果载荷仍手写 + 运行时守卫**：结果经 `JobRecord.metrics`（索引签名
 *    `Record<string, unknown>`）下发，OpenAPI 描述不了它的内部结构（生成物
 *    `OptimizeJobResponse` 注释即此口径：metrics 键随端点 pareto_front /
 *    variants / rows / config）——与 `perf.ts` 的 MC 结果同族处理。
 *
 * 取消语义：`JobRecord` 有 `cancelled` **终态**，但前端既有 API 面没有取消端点
 * （`/api/jobs/{id}` 只读）——故不提供取消按钮；后端若取消作业，经失败降级通路
 * 呈现（code = `JOB_CANCELLED`）。本模块只做传输与边界校验，不含任何优化计算
 * （ADR-011：数值一律后端下发）。
 */

/** 请求体与受理回执一律取自 OpenAPI 生成物（§18.2）。 */
export type Nsga2Request = components['schemas']['Nsga2Request']
export type TradeStudyRequest = components['schemas']['TradeStudyRequest']
export type SweepRequest = components['schemas']['SweepRequest']
export type InverseRequest = components['schemas']['InverseRequest']
export type OptimizeVariableSpec = components['schemas']['OptimizeVariableSpec']
export type SweepAxisSpec = components['schemas']['SweepAxisSpec']
export type OptimizeJobResponse = components['schemas']['OptimizeJobResponse']

/** 优化变量类别（生成物枚举转出：长度按比例连续缩放 / 数量类整数变量）。 */
export type OptimizeVariableKind = OptimizeVariableSpec['kind']

/**
 * 多目标优化的目标键（生成物 `Nsga2Request['objectives']` 成员的子集；前端片只
 * 暴露规格 §14 的前两个——min_glow / max_payload_leo，min_dry_mass 暂不进面板）。
 */
export type OptimizeObjectiveKey = 'min_glow' | 'max_payload_leo'

/** 逆向设计的目标轨道（生成物 `InverseRequest['target_orbit']`；SSO 不在契约内）。 */
export type InverseTargetOrbit = InverseRequest['target_orbit']

/** ΔV 供给模式（§8.6）：前端片固定 anchored（毫秒级；l2 会逐候选积分放大作业时长）。 */
const DV_SUPPLY: Nsga2Request['dv_supply'] = 'anchored'

function isJobStartResponse(value: unknown): value is OptimizeJobResponse {
  return isRecord(value) && typeof value.job_id === 'string' && value.job_id !== ''
}

// ---------------------------------------------------------------------------
// 结果载荷（经 JobRecord.metrics 下发；不在生成物覆盖范围，手写 + 守卫——同族 MC 口径）
// ---------------------------------------------------------------------------

/**
 * 一个候选方案行（Pareto 前沿 / 权衡对比 / 扫描聚合 / 逆向最小构型共用）。
 *
 * 审计纪律（§14 约束）：每个候选方案都要带 provenance，否则"最优解"无法审计——
 * 行级 `provenance` 可选下发，缺失时 UI 不得伪造。
 */
export interface OptimizeSolutionRow {
  /** 方案名（权衡研究的对比行有；Pareto 行可缺省）。 */
  label?: string
  /** 方案参数摘要：field_path → 数值（载入构型时按既有参数通路逐项写回）。 */
  params: Record<string, number>
  glow_kg: number
  payload_kg: number
  provenance?: Record<string, string>
}

/** NSGA-II 结果（经 metrics 下发）：Pareto 前沿。 */
export interface Nsga2Result {
  pareto_front: OptimizeSolutionRow[]
  warnings?: string[]
}

/** 权衡研究结果（经 metrics 下发）：≤ 20 个方案的对比行。 */
export interface TradeStudyResult {
  variants: OptimizeSolutionRow[]
  warnings?: string[]
}

/** 批量扫描结果（经 metrics 下发）：聚合行（每行 = 一组变量取值 + GLOW + 运力）。 */
export interface SweepResult {
  rows: OptimizeSolutionRow[]
  warnings?: string[]
}

/** 逆向设计结果（经 metrics 下发）：满足目标运力的最小构型。 */
export interface InverseResult {
  config: OptimizeSolutionRow
  warnings?: string[]
}

function isStringRecord(value: unknown): value is Record<string, string> {
  return (
    isRecord(value) && Object.values(value).every((item) => typeof item === 'string')
  )
}

function isSolutionRow(value: unknown): value is OptimizeSolutionRow {
  if (!isRecord(value)) return false
  return (
    typeof value.glow_kg === 'number' &&
    typeof value.payload_kg === 'number' &&
    isRecord(value.params) &&
    Object.values(value.params).every((item) => typeof item === 'number') &&
    (value.label === undefined || typeof value.label === 'string') &&
    (value.provenance === undefined || isStringRecord(value.provenance))
  )
}

function isWarnings(value: unknown): value is string[] {
  return value === undefined || (Array.isArray(value) && value.every((item) => typeof item === 'string'))
}

function isNsga2Result(value: unknown): value is Nsga2Result {
  return (
    isRecord(value) &&
    Array.isArray(value.pareto_front) &&
    value.pareto_front.every(isSolutionRow) &&
    isWarnings(value.warnings)
  )
}

function isTradeStudyResult(value: unknown): value is TradeStudyResult {
  return (
    isRecord(value) &&
    Array.isArray(value.variants) &&
    value.variants.every(isSolutionRow) &&
    isWarnings(value.warnings)
  )
}

function isSweepResult(value: unknown): value is SweepResult {
  return (
    isRecord(value) &&
    Array.isArray(value.rows) &&
    value.rows.every(isSolutionRow) &&
    isWarnings(value.warnings)
  )
}

function isInverseResult(value: unknown): value is InverseResult {
  return isRecord(value) && isSolutionRow(value.config) && isWarnings(value.warnings)
}

export {
  isInverseResult,
  isNsga2Result,
  isSweepResult,
  isTradeStudyResult,
}

// ---------------------------------------------------------------------------
// 作业投递（四个端点；请求体类型 = 生成物，键名与原手写形态零偏离）
// ---------------------------------------------------------------------------

/**
 * 投递 NSGA-II 多目标优化作业。
 *
 * 请求体 = 生成物 `Nsga2Request`：variables 只含勾选项；种群 / 代数 / seed 的
 * 缺省值由前端输入提供（40 / 30 / 42），搜索边界由后端按变量类别推导；
 * `dv_supply` 显式下发 anchored（毫秒级）。
 */
export function startNsga2(
  vehicle: Vehicle,
  variables: OptimizeVariableSpec[],
  options: {
    populationSize: number
    generations: number
    seed: number
    objectives: OptimizeObjectiveKey[]
  },
): Promise<string> {
  const body: Nsga2Request = {
    vehicle,
    variables,
    population_size: options.populationSize,
    generations: options.generations,
    seed: options.seed,
    objectives: options.objectives,
    dv_supply: DV_SUPPLY,
  }
  return postJson('/api/optimize/nsga2', body, isJobStartResponse).then(
    (response) => response.job_id,
  )
}

/**
 * 投递权衡研究作业（规格 §14：方案数 ≤ 20 由后端把关，超限 422）。
 *
 * 请求体 = 生成物 `TradeStudyRequest`：围绕基线构型并行评估 N 个方案。
 */
export function startTradeStudy(vehicle: Vehicle, variantCount: number): Promise<string> {
  const body: TradeStudyRequest = { vehicle, variant_count: variantCount, dv_supply: DV_SUPPLY }
  return postJson('/api/optimize/trade-study', body, isJobStartResponse).then(
    (response) => response.job_id,
  )
}

/**
 * 投递批量扫描作业（轴数 ≤ 3、组合 ≤ 10⁴ 由后端把关，超限 422）。
 *
 * 请求体 = 生成物 `SweepRequest`：每轴 = 变量路径 + 范围 + 步数。
 */
export function startSweep(vehicle: Vehicle, axes: SweepAxisSpec[]): Promise<string> {
  const body: SweepRequest = { vehicle, axes, dv_supply: DV_SUPPLY }
  return postJson('/api/optimize/sweep', body, isJobStartResponse).then(
    (response) => response.job_id,
  )
}

/**
 * 投递逆向设计作业。
 *
 * 请求体 = 生成物 `InverseRequest`：给定轨道与运力目标，求最小构型。
 */
export function startInverse(
  vehicle: Vehicle,
  targetOrbit: InverseTargetOrbit,
  targetPayloadKg: number,
): Promise<string> {
  const body: InverseRequest = {
    vehicle,
    target_orbit: targetOrbit,
    target_payload_kg: targetPayloadKg,
    dv_supply: DV_SUPPLY,
  }
  return postJson('/api/optimize/inverse', body, isJobStartResponse).then(
    (response) => response.job_id,
  )
}

// ---------------------------------------------------------------------------
// 作业通道（复用既有形态：WS 优先 → 异常降级轮询 → 总超时兜底，§10.2 / §9.3）
// ---------------------------------------------------------------------------

/** WS 不可用时轮询 `GET /api/jobs/{id}` 的间隔（与 perf / vehicleSummary 同值同源）。 */
export const OPTIMIZE_POLL_INTERVAL_MS = 500

/**
 * 取回优化作业终态的总超时：长任务可达数十秒，两分钟仍未终态即显式兜底——
 * 「既不成功也不失败」的悬置必须显式暴露（M2 握手教训同族）。
 */
export const OPTIMIZE_RESULT_TIMEOUT_MS = 120_000

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
      record.status === 'cancelled' ? '优化作业已取消' : '优化作业失败（后端未附详细原因）',
    suggestion: '重试优化；若反复失败请查看后端日志中的优化作业阶段耗时',
  })
}

/**
 * 等待优化作业终态并返回**结果载荷**（`JobRecord.metrics`）。
 *
 * 形态与 `fetchMcResult` / `waitForExportJob` 同款：WS 优先（握手悬置由其内置
 * 3 s 超时兜底）→ 传输异常 / 非终态关闭时降级轮询 → 总超时兜底。在途快照经
 * `onProgress` 透出（进度条 / 阶段文案），终态后 metrics 由调用方按能力守卫还原。
 */
export function waitForOptimizeJob(
  jobId: string,
  onProgress?: (record: JobRecord) => void,
): Promise<Record<string, unknown>> {
  return new Promise<Record<string, unknown>>((resolve, reject) => {
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
      if (!isTerminal(record)) {
        onProgress?.(record)
        return
      }
      if (record.status !== 'succeeded') {
        fail(jobFailureError(record))
        return
      }
      const metrics = record.metrics ?? null
      if (isRecord(metrics)) {
        settle(() => resolve(metrics))
        return
      }
      fail(
        new ApiError({
          code: 'CONTRACT_MISMATCH',
          stage: 'api',
          message: `优化作业 ${jobId} 成功终态但未返回结果载荷（metrics）`,
          suggestion: '重试优化；若持续出现请确认后端优化作业的结果组装',
        }),
      )
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
      pollTimer = window.setInterval(poll, OPTIMIZE_POLL_INTERVAL_MS)
    }

    timeoutTimer = window.setTimeout(() => {
      fail(
        new ApiError({
          code: 'OPTIMIZE_RESULT_TIMEOUT',
          stage: 'api',
          message: `优化作业 ${jobId} 在 ${OPTIMIZE_RESULT_TIMEOUT_MS / 1000} s 内未完成`,
          suggestion: '重试优化；若持续超时请查看后端优化侧（NSGA-II 进程池）日志',
        }),
      )
    }, OPTIMIZE_RESULT_TIMEOUT_MS)

    try {
      stream = openJobStream(jobId, {
        onRecord: applyRecord,
        onError: () => {
          stream?.close()
          stream = null
          startPolling()
        },
        onClose: () => {
          // 终态由 applyRecord 收线；非终态关闭（后端重启等）必须降级，否则界面静默卡"优化中"
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
