import { create } from 'zustand'

import { ApiError } from '../api/client'
import {
  evaluatePerformance,
  fetchMcResult,
  type McResult,
  type PerfEvaluateResponse,
} from '../api/perf'
import { createDebouncedScheduler } from './model'
import { useVehicleStore } from './vehicle'

/**
 * 计算评估面板的状态（规格 §11.6 的 `store/computed` 角色：只由后端响应写入，
 * 前端不得修改；两阶段契约 OI-25 的「点值 → 区间覆盖」状态机在此收口）。
 *
 * 两阶段（OI-25）：
 * - **阶段①（同步）**：`runEvaluate` 发出 `POST /api/perf/evaluate`，毫秒级返回
 *   `response`（点值 + ΔV 瀑布 + `interval_pending` / `mc_job_id`）。
 * - **阶段②（异步）**：`fetchMcResult` 经作业通道取回 MC 区间；**未到达前**区间列只能
 *   是占位态或错误态——`mc` 为 `null` 且 `mcError` 为 `null` 即「区间计算中」。
 *
 * 不混排规则（§8.7 OI-25 规则 2）：**每次发起新评估都清空 `mc` / `mcError`**——旧区间
 * 属于旧输入，留着它会让人误以为已更新；新阶段①响应到达（`interval_pending=true`）
 * 后区间列继续占位，直到新区间整体覆盖。
 */

/** 参数变更后自动重算的防抖窗口（与诊断同一口径 §11.4，避免逐字符请求风暴）。 */
export const EVALUATE_DEBOUNCE_MS = 200

/** 需要展示给用户的错误（§10.3 的 `suggestion` 必填且必须可见）。 */
export interface PerfError {
  code: string
  stage: string
  message: string
  suggestion: string
}

function toPerfError(error: unknown): PerfError {
  if (error instanceof ApiError) {
    return {
      code: error.code,
      stage: error.stage,
      message: error.message,
      suggestion: error.suggestion,
    }
  }
  return {
    code: 'UNEXPECTED',
    stage: 'api',
    message: error instanceof Error ? error.message : '未知错误',
    suggestion: '确认后端已启动（GET /api/health）后重试；若持续失败请查看后端日志',
  }
}

interface PerfState {
  /** 底部计算面板是否弹出（§11.5 ③：弹出不遮挡 3D 视口——由布局保证，非遮挡式）。 */
  panelOpen: boolean
  /** 自动更新开关（§11.5 / OI-04：关闭 = 冻结——参数变更不再触发自动重算）。 */
  autoUpdate: boolean
  /** 阶段①在途（点值评估请求已发出）。 */
  evaluating: boolean
  response: PerfEvaluateResponse | null
  evaluateError: PerfError | null
  /** 阶段②结果（MC 区间；`null` = 未到达——区间列据此显示占位态或错误态）。 */
  mc: McResult | null
  mcError: PerfError | null
  openPanel: () => void
  closePanel: () => void
  setAutoUpdate: (on: boolean) => void
  /** 立即发一次阶段①评估（「计算」按钮 / 重开自动更新时用，OI-04 规则 4）。 */
  runEvaluate: () => void
  /** 自动更新开启时由参数变更触发：经防抖后自动调（§11.5 ③）。 */
  requestAutoEvaluate: () => void
  /** 阶段②失败后的重试：按当前 response 的 mc_job_id 重新取回。 */
  retryMc: () => void
  reset: () => void
}

export const usePerfStore = create<PerfState>((set, get) => {
  const scheduler = createDebouncedScheduler(EVALUATE_DEBOUNCE_MS)
  /** 评估代数：新评估发起即递增；迟到的旧响应 / 旧 MC 结果按它作废（不覆盖新结果）。 */
  let evaluateToken = 0

  /** 订阅阶段②：按代数守卫写入（迟到即丢弃）。 */
  function watchMcResult(jobId: string, token: number): void {
    fetchMcResult(jobId)
      .then((mc) => {
        if (token !== evaluateToken) return
        set({ mc, mcError: null })
      })
      .catch((error: unknown) => {
        if (token !== evaluateToken) return
        // 失败只占住区间列（占位态变错误 + 重试），不阻塞点值显示（OI-25）
        set({ mcError: toPerfError(error) })
      })
  }

  return {
    panelOpen: false,
    autoUpdate: true,
    evaluating: false,
    response: null,
    evaluateError: null,
    mc: null,
    mcError: null,

    openPanel: () => {
      set({ panelOpen: true })
    },

    closePanel: () => {
      set({ panelOpen: false })
    },

    setAutoUpdate: (on) => {
      set({ autoUpdate: on })
      // OI-04 规则 4：重新开启时立即按当前输入重算一次（关闭期间的触发接入只有这一条，
      // 冻结标记等 UI 语义不在本片范围——见 OI-04 裁决）。
      if (on) get().runEvaluate()
    },

    runEvaluate: () => {
      const vehicle = useVehicleStore.getState().vehicle
      if (vehicle === null) return

      const token = ++evaluateToken
      // 不混排（OI-25 规则 2）：新评估发起即清旧区间与旧错误——旧数字属于旧输入，
      // 与新区间点值混排正是本条要拦的形态。
      set({ evaluating: true, evaluateError: null, mc: null, mcError: null })

      evaluatePerformance(vehicle)
        .then((response) => {
          if (token !== evaluateToken) return
          set({ response, evaluating: false })
          const mcJobId = response.mc_job_id ?? null
          if (response.interval_pending && mcJobId !== null) {
            watchMcResult(mcJobId, token)
          }
        })
        .catch((error: unknown) => {
          if (token !== evaluateToken) return
          set({ evaluating: false, evaluateError: toPerfError(error) })
        })
    },

    requestAutoEvaluate: () => {
      scheduler.schedule(() => {
        // 冻结语义（OI-04）：自动更新关闭时，参数变更不再触发自动重算。
        if (!usePerfStore.getState().autoUpdate) return
        get().runEvaluate()
      })
    },

    retryMc: () => {
      const jobId = get().response?.mc_job_id ?? null
      if (jobId === null) return
      set({ mcError: null }) // 回到占位态（区间计算中）
      watchMcResult(jobId, evaluateToken)
    },

    reset: () => {
      scheduler.cancel()
      evaluateToken += 1
      set({
        panelOpen: false,
        autoUpdate: true,
        evaluating: false,
        response: null,
        evaluateError: null,
        mc: null,
        mcError: null,
      })
    },
  }
})
