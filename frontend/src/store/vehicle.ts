import { create } from 'zustand'

import { ApiError, isRecord } from '../api/client'
import {
  diagnose,
  fetchTemplate,
  type Booster,
  type Diagnostic,
  type DiagnoseResponse,
  type Vehicle,
} from '../api/params'
import { createDebouncedScheduler } from './model'
import { writeFieldPath } from './fieldPath'

/**
 * 参数面板的状态（规格 §11.6 的 `store/params`：用户参数的**唯一可写来源**）。
 *
 * ⚠ 与 `store/params.ts` 的关系：那个文件承载 M1 的**母线草稿**（几何探针的输入），
 * 本文件承载 M2 的**飞行器参数**（§6.1 的 `Vehicle`）。二者各自只被自己的面板写入，
 * 互不复制对方的数据——同一条物理量出现两份副本正是 P1 要拦的东西。
 *
 * 起始箭一律由后端下发（`GET /api/params/template`，§11.5 ① 第 6 条）：
 * 前端**不得**硬编码一套"看起来像真的"型号数字，否则会形成与 §13.2 基准表冲突的第二套数字。
 */

/** §11.4：改值后经 200 ms 防抖发出请求（与几何校验同一口径，避免逐字符请求风暴）。 */
export const DIAGNOSE_DEBOUNCE_MS = 200

/**
 * 用户改动后的出处标注（§11.5 ⑤ 规则 5）：模板出处一旦被用户覆写即失去「已核对」
 * 身份，标注整体转为这两个字——不得把模板出处与用户改值混在一句话里。
 */
export const USER_MODIFIED_SOURCE = '用户修改'

/** 需要展示给用户的错误（§10.3 的 `suggestion` 必填且必须可见）。 */
export interface VehicleError {
  code: string
  stage: string
  message: string
  suggestion: string
}

function toVehicleError(error: unknown): VehicleError {
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

/**
 * 从错误体的 `details.diagnostics` 里取出字段级裁定。
 *
 * §6.3 末注：请求校验通路（422）与诊断通路（200）下发的 `field_path` **同构**，
 * 故两条通路共用同一张「路径 → 控件」映射。取不到就返回空数组——**不构造**占位条目，
 * 那会让清单看起来有内容。
 */
function diagnosticsFromError(error: unknown): Diagnostic[] {
  if (!(error instanceof ApiError)) return []
  const raw = error.details?.['diagnostics']
  if (!Array.isArray(raw)) return []
  return raw.filter((item): item is Diagnostic => {
    if (!isRecord(item)) return false
    return (
      typeof item.level === 'string' &&
      typeof item.code === 'string' &&
      typeof item.field_path === 'string' &&
      typeof item.message === 'string' &&
      typeof item.suggestion === 'string'
    )
  })
}

interface VehicleState {
  /** 当前飞行器参数（`null` = 尚未载入起始箭）。 */
  vehicle: Vehicle | null
  templateId: string | null
  /** 起始箭的显示名与说明（**原样呈现**，不得改写为更肯定的措辞）。 */
  label: string
  note: string
  /** 有出处的字段路径 → 出处；不在此表里的数值一律按**占位值**渲染（§1.4-4）。 */
  sourcedFields: Record<string, string>
  loading: boolean
  loadError: VehicleError | null
  report: DiagnoseResponse | null
  /** 422 通路下发的字段级裁定（与 `report.constraints` 同形，两类都要渲染）。 */
  errorDiagnostics: Diagnostic[]
  diagnosing: boolean
  diagnoseError: VehicleError | null
  loadTemplate: () => Promise<void>
  setField: (path: string, value: unknown) => void
  /** 整体替换出处表（载入模板时用——替换即清掉旧表，包括「用户修改」标注）。 */
  setSourcedFields: (map: Record<string, string>) => void
  /** 用户改动某字段：该字段出处转为「用户修改」（§11.5 ⑤ 规则 5）。 */
  markUserModified: (path: string) => void
  requestDiagnose: () => void
  /** 追加一组并联助推器（OI-36：侧级骨架 = 芯一级克隆，级号改 0）。 */
  addBooster: () => void
  /** 删除第 index 组助推器（数组下标，field_path 同口径）。 */
  removeBooster: (index: number) => void
  reset: () => void
}

export const useVehicleStore = create<VehicleState>((set, get) => {
  const scheduler = createDebouncedScheduler(DIAGNOSE_DEBOUNCE_MS)
  let diagnoseToken = 0

  function runDiagnose(): void {
    const vehicle = get().vehicle
    if (vehicle === null) return

    const token = ++diagnoseToken
    set({ diagnosing: true })
    diagnose(vehicle)
      .then((report) => {
        if (token !== diagnoseToken) return
        set({ report, diagnosing: false, diagnoseError: null, errorDiagnostics: [] })
      })
      .catch((error: unknown) => {
        if (token !== diagnoseToken) return
        // 硬约束违反（422）也走这里：错误体里带着字段级裁定，必须一并渲染。
        set({
          report: null,
          diagnosing: false,
          diagnoseError: toVehicleError(error),
          errorDiagnostics: diagnosticsFromError(error),
        })
      })
  }

  return {
    vehicle: null,
    templateId: null,
    label: '',
    note: '',
    sourcedFields: {},
    loading: false,
    loadError: null,
    report: null,
    errorDiagnostics: [],
    diagnosing: false,
    diagnoseError: null,

    loadTemplate: async () => {
      set({ loading: true, loadError: null })
      try {
        const template = await fetchTemplate()
        set({
          vehicle: template.vehicle,
          templateId: template.template_id,
          label: template.label,
          note: template.note,
          sourcedFields: template.sourced_fields,
          loading: false,
        })
        runDiagnose()
      } catch (error: unknown) {
        set({ loading: false, loadError: toVehicleError(error) })
      }
    },

    setField: (path, value) => {
      const vehicle = get().vehicle
      if (vehicle === null) return
      try {
        set({ vehicle: writeFieldPath(vehicle, path, value) })
      } catch (error: unknown) {
        // 路径与本模块的字段清单不符 = 实现缺陷。**显式暴露**而不是静默吞掉。
        set({ diagnoseError: toVehicleError(error), errorDiagnostics: [] })
        return
      }
      scheduler.schedule(runDiagnose)
    },

    setSourcedFields: (map) => {
      set({ sourcedFields: map })
    },

    markUserModified: (path) => {
      set({ sourcedFields: { ...get().sourcedFields, [path]: USER_MODIFIED_SOURCE } })
    },

    requestDiagnose: () => {
      scheduler.schedule(runDiagnose)
    },

    addBooster: () => {
      const vehicle = get().vehicle
      if (vehicle === null || vehicle.stages.length === 0) return
      // 侧级骨架 = **芯一级整套克隆**（数值全部来自用户自己的输入，前端不造任何"看起来
      // 像真的"数字，§1.4-4 / P1），级号按 OI-36 记 0（与 GCAT 助推器记法对齐）。
      const stage = JSON.parse(JSON.stringify(vehicle.stages[0])) as Booster['stage']
      stage.index = 0
      const booster: Booster = {
        stage,
        count: 2, // §6.1 Booster 层默认（radial_even 周向均布）
        layout: 'radial_even',
        separation_s: null, // 省略 = 芯一级关机时刻（§6.1 语义：null 即移除）
      }
      set({ vehicle: { ...vehicle, boosters: [...(vehicle.boosters ?? []), booster] } })
      scheduler.schedule(runDiagnose)
    },

    removeBooster: (index) => {
      const vehicle = get().vehicle
      if (vehicle === null || vehicle.boosters === undefined) return
      set({ vehicle: { ...vehicle, boosters: vehicle.boosters.filter((_, i) => i !== index) } })
      scheduler.schedule(runDiagnose)
    },

    reset: () => {
      scheduler.cancel()
      diagnoseToken += 1
      set({
        vehicle: null,
        templateId: null,
        label: '',
        note: '',
        sourcedFields: {},
        loading: false,
        loadError: null,
        report: null,
        errorDiagnostics: [],
        diagnosing: false,
        diagnoseError: null,
      })
    },
  }
})
