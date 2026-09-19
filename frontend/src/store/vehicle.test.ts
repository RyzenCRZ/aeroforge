import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DiagnoseResponse, TemplateResponse, Vehicle } from '../api/params'
import { DIAGNOSE_DEBOUNCE_MS, useVehicleStore } from './vehicle'

/**
 * 参数面板状态（规格 §11.5 ① / §11.6 / §10.3）。
 *
 * 三条被钉住的契约：
 *
 * 1. **起始箭只能来自后端**（§11.5 ① 第 6 条）——`label` / `note` / `sourced_fields`
 *    必须**原样**存下来，界面才有依据把"未经来源核对"和占位值如实显示出来。
 * 2. **422 通路的字段级裁定不得丢**（§6.3 末注）：硬约束违反时错误体里带着
 *    `details.diagnostics`，它是「路径 → 控件」映射的另一半；取不到就返回空数组，
 *    **不构造**占位条目。
 * 3. **改值经 200 ms 防抖**（§11.4）：逐字符改值不得变成请求风暴。
 */

const ENGINE = {
  model: '示例发动机',
  propellant_phase: 'liquid',
  cycle: 'gas_generator',
  chamber_pressure_pa: 68.9e5,
  expansion_ratio: 40,
  efficiency_factor: 0.98,
  thrust_sea_level_n: 700_000,
  thrust_vacuum_n: 800_000,
  isp_sea_level_s: 339.1,
  isp_vacuum_s: 356.6,
  mixture_ratio: 2.56,
} as const

const TANK = {
  tank_type: 'separate',
  wall_thickness_m: 0.005,
  material: 'Al-2219',
  fill_fraction: 0.95,
  feed_system: 'pump_fed',
  delivery_pipe_routing: 'external',
} as const

const VEHICLE: Vehicle = {
  schema_version: '1',
  name: '示例骨架 · 单级 LOX/RP-1（未经来源核对）',
  stages: [
    {
      index: 1,
      propellant: 'LOX/RP-1',
      diameter_m: 2,
      length_m: 20,
      wall_thickness_m: 0.005,
      material: 'Al-2219',
      structure_coefficient: 0.05,
      fill_fraction: 0.95,
      engine_count: 1,
      engine: { ...ENGINE },
      engine_height_m: 2,
      interstage_type: 'none',
      isp_vacuum_s: 356.6,
      isp_sea_level_s: 339.1,
      isp_source: 'default',
      recoverable: false,
      geometry: {
        common_bulkhead: false,
        tank_arrangement: 'oxidizer_upper',
        fins_enabled: false,
        oxidizer_tank: { ...TANK },
        fuel_tank: { ...TANK },
      },
    },
  ],
  payload_mass_kg: 1000,
  material: 'Al-2219',
  propellant: 'LOX/RP-1',
  mission: { orbit_type: 'LEO', altitude_m: 200_000, inclination_deg: 28.5 },
}

const NOTE = '这是一份未经来源核对的示例骨架，除 sourced_fields 列出的项之外，全部数值均为占位值。'

const TEMPLATE: TemplateResponse = {
  template_id: 'skeleton-1stage-lox-rp1',
  label: '示例骨架 · 单级 LOX/RP-1（未经来源核对）',
  note: NOTE,
  sourced_fields: {
    'stages[0].engine.mixture_ratio': '规格 §3.2：LOX/RP-1 黄金锚点 O/F = 2.56',
  },
  vehicle: VEHICLE,
}

const REPORT: DiagnoseResponse = {
  constraints: [],
  diagnostics: [],
  rules: [
    {
      code: 'TWR_TOO_LOW',
      title: '起飞推重比',
      level: 'hard',
      threshold: '≥ 1.2（液体）',
      source: '规格 §6.5 表 · 通用工程惯例',
      diagnostics: [],
      uncovered: [],
    },
  ],
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

function diagnosisResponse(rules: DiagnoseResponse['rules']): DiagnoseResponse {
  return { constraints: [], diagnostics: [], rules: rules.map((rule) => ({ ...rule })) }
}

/** 手动控制兑现时机的 promise（用于钉住"迟到响应不得覆盖新结果"）。 */
function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => {}
  const promise = new Promise<T>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

async function flushMicrotasks(): Promise<void> {
  for (let round = 0; round < 10; round += 1) await Promise.resolve()
}

beforeEach(() => {
  vi.useFakeTimers()
  useVehicleStore.getState().reset()
})

afterEach(() => {
  useVehicleStore.getState().reset()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('起始箭载入（§11.5 ① 第 6 条）', () => {
  it('label / note / sourced_fields 原样落地（含「未经来源核对」字样）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(TEMPLATE)))

    await useVehicleStore.getState().loadTemplate()

    const state = useVehicleStore.getState()
    expect(state.loading).toBe(false)
    expect(state.loadError).toBeNull()
    expect(state.templateId).toBe(TEMPLATE.template_id)
    expect(state.label).toBe(TEMPLATE.label)
    expect(state.note).toBe(NOTE)
    expect(state.note).toContain('未经来源核对')
    expect(state.sourcedFields['stages[0].engine.mixture_ratio']).toContain('§3.2')
    expect(state.vehicle?.stages[0]?.engine.mixture_ratio).toBe(2.56)
  })

  it('载入后立即跑一次诊断（面板打开就有结论，不必等用户改值）', async () => {
    const fetchMock = vi.fn(async (path: string) =>
      jsonResponse(path === '/api/params/template' ? TEMPLATE : REPORT),
    )
    vi.stubGlobal('fetch', fetchMock)

    await useVehicleStore.getState().loadTemplate()
    await vi.advanceTimersByTimeAsync(DIAGNOSE_DEBOUNCE_MS)
    await flushMicrotasks()

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/params/template',
      '/api/params/diagnose',
    ])
    expect(useVehicleStore.getState().report?.rules).toHaveLength(1)
    expect(useVehicleStore.getState().diagnosing).toBe(false)
  })

  it('起始箭载入失败时给出 §10.3 的 suggestion，不静默留空', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            error: {
              code: 'PARAMS_TEMPLATE_UNAVAILABLE',
              stage: 'params',
              message: '起始箭不可用',
              suggestion: '确认后端已启动（GET /api/health）后重试',
            },
          },
          500,
        ),
      ),
    )

    await useVehicleStore.getState().loadTemplate()

    const error = useVehicleStore.getState().loadError
    expect(error?.code).toBe('PARAMS_TEMPLATE_UNAVAILABLE')
    expect(error?.suggestion).not.toBe('')
    expect(useVehicleStore.getState().vehicle).toBeNull()
  })
})

describe('改值：写回 + 防抖重诊断（§11.4）', () => {
  async function loadWithDiagnose(): Promise<ReturnType<typeof vi.fn>> {
    const fetchMock = vi.fn(async (path: string) =>
      jsonResponse(path === '/api/params/template' ? TEMPLATE : REPORT),
    )
    vi.stubGlobal('fetch', fetchMock)
    await useVehicleStore.getState().loadTemplate()
    await vi.advanceTimersByTimeAsync(DIAGNOSE_DEBOUNCE_MS)
    await flushMicrotasks()
    fetchMock.mockClear()
    return fetchMock
  }

  it('路径可达时写入新对象，并经 200 ms 防抖只发一次诊断', async () => {
    const fetchMock = await loadWithDiagnose()

    useVehicleStore.getState().setField('stages[0].engine.mixture_ratio', 2.7)
    useVehicleStore.getState().setField('stages[0].engine.mixture_ratio', 2.8)
    useVehicleStore.getState().setField('stages[0].engine.mixture_ratio', 2.9)

    await vi.advanceTimersByTimeAsync(DIAGNOSE_DEBOUNCE_MS - 1)
    expect(fetchMock).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(call[0]).toBe('/api/params/diagnose')
    const body = JSON.parse(String(call[1].body)) as Vehicle
    expect(body.stages[0]?.engine.mixture_ratio).toBe(2.9)
    expect(useVehicleStore.getState().vehicle?.stages[0]?.engine.mixture_ratio).toBe(2.9)
  })

  it('路径与本模块字段清单不符时显式暴露，不抛给调用方也不静默', async () => {
    await loadWithDiagnose()

    expect(() => useVehicleStore.getState().setField('stages[9].fill_fraction', 0.5)).not.toThrow()

    const state = useVehicleStore.getState()
    expect(state.diagnoseError?.message).toContain('stages[9].fill_fraction')
    expect(state.errorDiagnostics).toEqual([])
    // 参数本体不得被半途改动
    expect(state.vehicle?.stages[0]?.fill_fraction).toBe(0.95)
  })
})

describe('两条通路的裁定同形（§6.3 末注 / §6.5）', () => {
  it('422 的 details.diagnostics 被取出并保留 field_path', async () => {
    const fetchMock = vi.fn(async (path: string) => {
      if (path === '/api/params/template') return jsonResponse(TEMPLATE)
      return jsonResponse(
        {
          error: {
            code: 'PARAMS_HARD_CONSTRAINT',
            stage: 'params',
            message: '起飞推重比 0.8 低于硬约束下限',
            details: {
              diagnostics: [
                {
                  level: 'hard',
                  code: 'HARD_TWR_TOO_LOW',
                  field_path: 'stages[0].engine.thrust_sea_level_n',
                  message: '起飞推重比 0.8 低于下限 1.2',
                  suggestion: '提高推力或降低起飞质量',
                },
              ],
            },
            suggestion: '按 details.diagnostics 逐条修正',
          },
        },
        422,
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    await useVehicleStore.getState().loadTemplate()
    await vi.advanceTimersByTimeAsync(DIAGNOSE_DEBOUNCE_MS)
    await flushMicrotasks()

    const state = useVehicleStore.getState()
    expect(state.report).toBeNull()
    expect(state.diagnoseError?.code).toBe('PARAMS_HARD_CONSTRAINT')
    expect(state.errorDiagnostics).toHaveLength(1)
    expect(state.errorDiagnostics[0]?.field_path).toBe('stages[0].engine.thrust_sea_level_n')
    expect(state.errorDiagnostics[0]?.suggestion).not.toBe('')
  })

  it('错误体里没有裁定明细时返回空数组，不构造占位条目', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string) =>
        path === '/api/params/template'
          ? jsonResponse(TEMPLATE)
          : jsonResponse(
              {
                error: {
                  code: 'PARAMS_INVALID',
                  stage: 'params',
                  message: '请求结构不合法',
                  suggestion: '检查字段类型',
                },
              },
              422,
            ),
      ),
    )

    await useVehicleStore.getState().loadTemplate()
    await vi.advanceTimersByTimeAsync(DIAGNOSE_DEBOUNCE_MS)
    await flushMicrotasks()

    expect(useVehicleStore.getState().errorDiagnostics).toEqual([])
    expect(useVehicleStore.getState().diagnoseError?.code).toBe('PARAMS_INVALID')
  })
})

describe('竞态保护', () => {
  it('迟到的旧诊断不得覆盖新结果', async () => {
    const first = deferred<Response>()
    const second = deferred<Response>()
    let diagnoseCalls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (path: string) => {
        if (path === '/api/params/template') return jsonResponse(TEMPLATE)
        diagnoseCalls += 1
        return diagnoseCalls === 1 ? first.promise : second.promise
      }),
    )

    await useVehicleStore.getState().loadTemplate()

    // 第一次诊断（载入后触发）挂住不兑现
    await vi.advanceTimersByTimeAsync(DIAGNOSE_DEBOUNCE_MS)
    expect(diagnoseCalls).toBe(1)

    // 改值触发第二次诊断，并让它先兑现
    useVehicleStore.getState().setField('stages[0].fill_fraction', 0.9)
    await vi.advanceTimersByTimeAsync(DIAGNOSE_DEBOUNCE_MS)
    expect(diagnoseCalls).toBe(2)

    second.resolve(
      jsonResponse(
        diagnosisResponse([
          {
            ...REPORT.rules[0],
            threshold: '第二次',
          },
        ]),
      ),
    )
    await flushMicrotasks()
    expect(useVehicleStore.getState().report?.rules[0]?.threshold).toBe('第二次')

    // 旧响应迟到：不得把它写回去
    first.resolve(
      jsonResponse(
        diagnosisResponse([
          {
            ...REPORT.rules[0],
            threshold: '第一次',
          },
        ]),
      ),
    )
    await flushMicrotasks()
    expect(useVehicleStore.getState().report?.rules[0]?.threshold).toBe('第二次')
  })
})
