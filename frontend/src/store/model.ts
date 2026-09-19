import { create } from 'zustand'

import {
  ApiError,
  buildGeometry,
  fetchJob,
  openJobStream,
  validateProfile,
  type JobRecord,
  type JobStream,
  type MeridianProfile,
  type ValidationReport,
} from '../api/geometry'
import { useParamsStore } from './params'

/** §11.4：改值后经 200 ms 防抖发出请求，避免连续拖拽造成的请求风暴（NFR-01）。 */
export const VALIDATE_DEBOUNCE_MS = 200

/** WS 不可用时轮询 `GET /api/jobs/{id}` 的间隔（§10.2 降级路径）。 */
export const JOB_POLL_INTERVAL_MS = 500

/** 3D 视口的两条通道（ADR-012）。 */
export type Channel = 'schematic' | 'authoritative'

/** 错误来源：决定状态栏文案（校验未通过 / 构建失败）。 */
export type ErrorOrigin = 'validate' | 'build'

/** 需要展示给用户的错误（§10.3 的 `suggestion` 必填且必须可见）。 */
export interface ModelError {
  code: string
  stage: string
  message: string
  suggestion: string
  origin: ErrorOrigin
}

/** 可注入的防抖调度器：把"何时发请求"与"发什么请求"解耦，便于用假定时器测（§13.6 防抖有效性）。 */
export interface DebouncedScheduler {
  schedule: (task: () => void) => void
  cancel: () => void
}

export function createDebouncedScheduler(delayMs: number): DebouncedScheduler {
  let timer: number | null = null
  return {
    schedule: (task) => {
      if (timer !== null) window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        timer = null
        task()
      }, delayMs)
    },
    cancel: () => {
      if (timer !== null) {
        window.clearTimeout(timer)
        timer = null
      }
    },
  }
}

function toModelError(error: unknown, origin: ErrorOrigin): ModelError {
  if (error instanceof ApiError) {
    return {
      code: error.code,
      stage: error.stage,
      message: error.message,
      suggestion: error.suggestion,
      origin,
    }
  }
  return {
    code: 'UNEXPECTED',
    stage: 'api',
    message: error instanceof Error ? error.message : '未知错误',
    suggestion: '确认后端已启动（GET /api/health）后重试；若持续失败请查看后端日志',
    origin,
  }
}

function isTerminal(record: JobRecord | null): boolean {
  return (
    record !== null &&
    (record.status === 'succeeded' || record.status === 'failed' || record.status === 'cancelled')
  )
}

interface ModelState {
  /** 后端母线层校验报告（唯一几何数值来源，前端只消费）。 */
  report: ValidationReport | null
  validating: boolean
  error: ModelError | null
  /** 是否存在未完成的构建作业（构建中禁止重复提交）。 */
  building: boolean
  job: JobRecord | null
  /** 构建产物指标（命中缓存时来自 BuildResponse，否则来自 JobRecord）。 */
  metrics: Record<string, unknown> | null
  /** 权威产物的内容寻址键；示意通道下也可保留，供切换回权威通道。 */
  authoritativeKey: string | null
  channel: Channel
  runValidation: (profile: MeridianProfile) => void
  startBuild: () => Promise<void>
  useAuthoritative: (key?: string) => void
  useSchematic: () => void
  clearError: () => void
  reset: () => void
}

/**
 * 校验与构建状态。
 *
 * 通道切换时机（ADR-012 / R-25）：
 * - 权威 GLB 可用（缓存命中、或作业成功且有 `result_key`）→ **强制**切到权威通道；
 * - 作业失败 / 取消了 → 退回示意通道并保留错误（不得留下空白视口）。
 */
export const useModelStore = create<ModelState>((set, get) => {
  const scheduler = createDebouncedScheduler(VALIDATE_DEBOUNCE_MS)
  let pendingProfile: MeridianProfile | null = null
  let validationToken = 0
  let pollTimer: number | null = null
  let stream: JobStream | null = null

  function stopPolling(): void {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
  }

  function closeStream(): void {
    if (stream !== null) {
      stream.close()
      stream = null
    }
  }

  function applyRecord(record: JobRecord): void {
    set({ job: record })
    if (!isTerminal(record)) return

    stopPolling()
    closeStream()

    if (record.status === 'succeeded') {
      set({
        building: false,
        channel: 'authoritative',
        authoritativeKey: record.result_key ?? null,
        metrics: record.metrics ?? null,
      })
      return
    }

    const failure: ModelError =
      record.error == null
        ? {
            code: record.status === 'cancelled' ? 'JOB_CANCELLED' : 'JOB_FAILED',
            stage: record.stage,
            message: record.status === 'cancelled' ? '构建作业已取消' : '构建作业失败（后端未附详细原因）',
            suggestion: '检查母线参数后重试；若反复失败请查看后端日志中的作业阶段耗时',
            origin: 'build',
          }
        : {
            code: record.error.code,
            stage: record.error.stage,
            message: record.error.message,
            suggestion: record.error.suggestion,
            origin: 'build',
          }
    set({ building: false, channel: 'schematic', error: failure })
  }

  function startPolling(jobId: string): void {
    if (pollTimer !== null) return
    pollTimer = window.setInterval(() => {
      fetchJob(jobId)
        .then((record) => {
          applyRecord(record)
        })
        .catch((error: unknown) => {
          stopPolling()
          set({ building: false, channel: 'schematic', error: toModelError(error, 'build') })
        })
    }, JOB_POLL_INTERVAL_MS)
  }

  function subscribeJob(jobId: string): void {
    let degraded = false
    const degrade = (): void => {
      if (degraded) return
      degraded = true
      startPolling(jobId)
    }

    try {
      stream = openJobStream(jobId, {
        onRecord: (record) => {
          applyRecord(record)
        },
        onError: () => {
          degrade()
        },
        onClose: () => {
          // 终态由 applyRecord 收线；非终态关闭（后端重启等）必须降级，否则会静默卡在"构建中"
          if (!isTerminal(get().job)) degrade()
        },
      })
    } catch {
      degrade()
    }
  }

  return {
    report: null,
    validating: false,
    error: null,
    building: false,
    job: null,
    metrics: null,
    authoritativeKey: null,
    channel: 'schematic',

    runValidation: (profile) => {
      pendingProfile = profile
      scheduler.schedule(() => {
        const target = pendingProfile
        pendingProfile = null
        if (target === null) return

        const token = ++validationToken
        set({ validating: true })
        validateProfile(target)
          .then((report) => {
            if (token !== validationToken) return
            set({ report, validating: false, error: null })
          })
          .catch((error: unknown) => {
            if (token !== validationToken) return
            set({ validating: false, error: toModelError(error, 'validate') })
          })
      })
    },

    startBuild: async () => {
      const profile = useParamsStore.getState().profile
      stopPolling()
      closeStream()
      set({ building: true, error: null, job: null, metrics: null })

      try {
        const response = await buildGeometry(profile)
        if (response.cache_hit) {
          set({
            building: false,
            channel: 'authoritative',
            authoritativeKey: response.key,
            metrics: response.metrics ?? null,
          })
          return
        }
        const jobId = response.job_id
        if (jobId === null || jobId === undefined) {
          set({
            building: false,
            channel: 'schematic',
            error: {
              code: 'BUILD_NO_JOB',
              stage: 'api',
              message: '构建未命中缓存，但后端未返回 job_id',
              suggestion: '重试构建；若持续失败，检查后端作业执行器是否已启动（GET /api/health）',
              origin: 'build',
            },
          })
          return
        }
        subscribeJob(jobId)
      } catch (error: unknown) {
        set({ building: false, channel: 'schematic', error: toModelError(error, 'build') })
      }
    },

    useAuthoritative: (key) => {
      const next = key ?? get().authoritativeKey
      if (next === null) return
      set({ authoritativeKey: next, channel: 'authoritative' })
    },

    useSchematic: () => set({ channel: 'schematic' }),

    clearError: () => set({ error: null }),

    reset: () => {
      scheduler.cancel()
      stopPolling()
      closeStream()
      pendingProfile = null
      validationToken += 1
      set({
        report: null,
        validating: false,
        error: null,
        building: false,
        job: null,
        metrics: null,
        authoritativeKey: null,
        channel: 'schematic',
      })
    },
  }
})
