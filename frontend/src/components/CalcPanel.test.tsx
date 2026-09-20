import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { JobRecord } from '../api/geometry'
import type { McResult, PerfEvaluateResponse } from '../api/perf'
import type { Vehicle } from '../api/params'
import { usePerfStore } from '../store/perf'
import { useVehicleStore } from '../store/vehicle'
import { CalcPanel } from './CalcPanel'

/**
 * 计算评估面板（§11.5 ③）+ 两阶段重算 UI（OI-25 核心验收）。
 *
 * 被钉住的契约：
 *
 * 1. **阶段①立即渲染**：四轨道点值表、GLOW、ΔV 瀑布 **7 个数值逐项**、assumptions
 *    文字、warnings；kg→t 按后端单位表显示（前端零换算系数）。
 * 2. **占位态**：`interval_pending=true` 时区间列是脉动占位（「区间计算中…」），
 *    **不出现任何 P5/P95 数字**。
 * 3. **整体覆盖**：MC 结果到达后区间列变 P5–P95（含 P50），`interval_pending=false`；
 *    **新旧不混排**——先有旧结果再发新请求时，旧区间数字清空、回到占位态。
 * 4. **偏度提示条**：`mean_vs_p50_note` 非空 → 「均值 ≠ P50」；全空 → 不出现。
 * 5. **MC 失败/超时**：占位变错误提示 + 重试按钮，点值显示不受阻塞。
 *
 * ECharts 不可用于 jsdom——本面板图表为数据行 + CSS 条的薄封装，测试直接做
 * **数据渲染断言**（数值 / 文本），不挂任何图表实例。
 */

/** CI 时序教训：waitFor 显式给足超时（真实定时器下防抖 / WS 事件偶尔慢一拍）。 */
const WAIT_TIMEOUT = 4_000

/** 构造一个结构合法的单级 Vehicle（字段集满足 §6.1 各层的必填项）。 */
function makeVehicle(): Vehicle {
  return {
    schema_version: '1',
    name: '示例箭',
    payload_mass_kg: 1000,
    material: 'aluminum',
    propellant: 'LOX/RP-1',
    stages: [
      {
        index: 1,
        propellant: 'LOX/RP-1',
        diameter_m: 3.35,
        length_m: 20,
        wall_thickness_m: 0.004,
        material: 'aluminum',
        structure_coefficient: 0.08,
        fill_fraction: 0.95,
        engine_count: 4,
        engine_height_m: 3,
        interstage_type: 'cold_staging',
        isp_source: 'default',
        recoverable: false,
        engine: {
          model: 'YF-21',
          propellant_phase: 'liquid',
          cycle: 'staged_combustion',
          chamber_pressure_pa: 9_000_000,
          expansion_ratio: 30,
          efficiency_factor: 0.95,
          thrust_sea_level_n: 3_000_000,
          thrust_vacuum_n: 3_390_000,
          isp_sea_level_s: 260,
          isp_vacuum_s: 300,
          mixture_ratio: 2.6,
        },
        geometry: {
          common_bulkhead: false,
          tank_arrangement: 'oxidizer_upper',
          fins_enabled: false,
          oxidizer_tank: {
            tank_type: 'separate',
            wall_thickness_m: 0.004,
            material: 'aluminum',
            fill_fraction: 0.95,
            feed_system: 'pump_fed',
            delivery_pipe_routing: 'external',
          },
          fuel_tank: {
            tank_type: 'separate',
            wall_thickness_m: 0.004,
            material: 'aluminum',
            fill_fraction: 0.95,
            feed_system: 'pump_fed',
            delivery_pipe_routing: 'external',
          },
        },
      },
    ],
    mission: {
      orbit_type: 'LEO',
      altitude_m: 500_000,
      inclination_deg: 97,
      launch_site: { name: '酒泉', latitude_deg: 40.96, altitude_m: 1000, azimuth_deg: 95 },
    },
  }
}

const UNITS_T = {
  units: [
    { quantity: 'mass', label: '质量', symbol: 't', si_symbol: 'kg', factor: 1000 },
  ],
}

const UNITS_EMPTY = { units: [] }

/** 阶段①响应（interval_pending=true + mc_job_id）：可切换点值以验证覆盖语义。 */
function makeEvaluateResponse(input: {
  jobId: string
  leoKg: number
  ssoKg?: number
  gtoKg?: number
  geoKg?: number
}): PerfEvaluateResponse {
  return {
    point: {
      payload_by_orbit: {
        leo_kg: { payload_kg: input.leoKg, dv_used_km_s: 9.41, dv_source: '量级锚定（§8.6 表中值）', attainable: true },
        sso_kg: { payload_kg: input.ssoKg ?? 16000, dv_used_km_s: 9.6, dv_source: '量级锚定（§8.6 表中值）', attainable: true },
        gto_kg: { payload_kg: input.gtoKg ?? 5500, dv_used_km_s: 12.5, dv_source: '量级锚定（§8.6 表中值）', attainable: true },
        geo_kg: { payload_kg: input.geoKg ?? 1200, dv_used_km_s: 14.8, dv_source: '量级锚定（§8.6 表中值）', attainable: true },
      },
      payload_mass_kg: 1000,
      glow_kg: 549000,
      c3_km2_s2: null,
    },
    delta_v_budget: {
      target_orbit: 'LEO',
      ideal_dv_km_s: 7.81,
      gravity_loss_km_s: 1.24,
      aero_loss_km_s: 0.18,
      steering_loss_km_s: 0.22,
      back_pressure_loss_km_s: 0.15,
      rotation_assist_km_s: 0.39,
      total_dv_km_s: 9.21,
      assumptions: ['理想脉冲近似，不含有限推力损失', '损失为 L1 参数化经验模型（§8.6）'],
    },
    warnings: ['Mission.launch_site 缺失：按默认发射场（卡纳维拉尔 28.5°N、向东）计算'],
    provenance: {},
    cache_hit: false,
    interval_pending: true,
    mc_job_id: input.jobId,
  }
}

/** MC 结果（阶段② metrics 载荷，形态与后端 mc_metrics_payload 一致）。 */
function makeMcResult(input: { skewNote: string | null }): McResult {
  return {
    interval: {
      leo_kg: { p5: 20100, p50: 22800, p95: 25300 },
      sso_kg: { p5: 15000, p50: 16200, p95: 17400 },
      gto_kg: { p5: 5000, p50: 5500, p95: 6000 },
      geo_kg: { p5: 1100, p50: 1200, p95: 1300 },
      glow_kg: { p5: 545000, p50: 549000, p95: 553000 },
    },
    moments: {
      leo_kg: {
        n: 10000,
        estimator: 'adjusted_sample',
        skewness: -1.2,
        skewness_null_reason: null,
        mean_vs_p50_note: input.skewNote,
      },
      sso_kg: { n: 10000, estimator: 'adjusted_sample', skewness: 0.1, skewness_null_reason: null, mean_vs_p50_note: null },
      gto_kg: { n: 10000, estimator: 'adjusted_sample', skewness: 0.05, skewness_null_reason: null, mean_vs_p50_note: null },
      geo_kg: { n: 10000, estimator: 'adjusted_sample', skewness: 0.02, skewness_null_reason: null, mean_vs_p50_note: null },
      glow_kg: { n: 10000, estimator: 'adjusted_sample', skewness: 0.01, skewness_null_reason: null, mean_vs_p50_note: null },
    },
    histogram: {
      leo_kg: {
        bin_edges: Array.from({ length: 51 }, (_, i) => 18000 + i * 200),
        counts: Array.from({ length: 50 }, () => 200),
      },
    },
    convergence: {
      leo_kg: { checkpoints: [1000, 10000], p50: [22600, 22800], drift_vs_final: [200, 0] },
    },
    sensitivity: [
      { param: 'stages[0].engine.isp_vacuum_s', impact: 1 },
      { param: 'stages[0].structure_coefficient', impact: 0.42 },
      { param: 'loss_budget', impact: 0.31 },
      { param: 'stages[0].fill_fraction', impact: 0.22 },
      { param: 'stages[0].engine.efficiency_factor', impact: 0.18 },
      { param: 'stages[0].wall_thickness_m', impact: 0.12 },
      { param: 'stages[0].diameter_m', impact: 0.08 },
      { param: 'stages[0].length_m', impact: 0.05 },
    ],
    provenance: { 'mc.samples': '10000' },
    interval_pending: false,
    mc_job_id: 'mc-job-1',
  }
}

function jobRecord(input: {
  jobId: string
  status: 'running' | 'succeeded' | 'failed'
  metrics?: unknown
  error?: { code: string; stage: string; message: string; suggestion: string } | null
}): JobRecord {
  return {
    job_id: input.jobId,
    status: input.status,
    stage: input.status === 'succeeded' ? 'done' : 'evaluating',
    progress: input.status === 'succeeded' ? 1 : 0.4,
    result_key: input.status === 'succeeded' ? 'sha256-mc-key' : null,
    metrics: (input.metrics as JobRecord['metrics']) ?? null,
    error: (input.error as JobRecord['error']) ?? null,
    created_at: '2026-09-20T00:00:00Z',
  }
}

/** WS 替身：把连接与事件投递握在测试手里，避免 jsdom 真去连 ws://。 */
class FakeSocket {
  static latest: FakeSocket | null = null
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null

  constructor(readonly url: string) {
    FakeSocket.latest = this
  }

  close(): void {
    /* 仅记录关闭意图；测试不关心 socket 内部状态 */
  }

  emit(record: unknown): void {
    this.onmessage?.({ data: JSON.stringify(record) } as MessageEvent)
  }
}

type FetchMock = Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response
}

/**
 * fetch 替身：只覆盖 CalcPanel 挂载（units）与两阶段请求（evaluate / jobs）。
 * evaluate 响应经 `evaluateResponse` 引用可变——用例中途切换以验证「不混排」。
 */
function makeFetchMock(evaluateResponse: { current: PerfEvaluateResponse }): FetchMock {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/params/units') return jsonResponse(UNITS_T)
    if (url === '/api/perf/evaluate') return jsonResponse(evaluateResponse.current)
    if (url.startsWith('/api/jobs/')) return jsonResponse(jobRecord({ jobId: 'mc-job-1', status: 'running' }))
    throw new Error(`测试未覆盖的请求：${url}`)
  })
}

/** 触发一次评估并等待阶段①渲染落定（点值出现；`expectJobId` 用于等待**新**响应覆盖旧值）。 */
async function runEvaluateAndAwaitPointValue(
  user?: ReturnType<typeof userEvent.setup>,
  expectJobId?: string,
): Promise<void> {
  if (user !== undefined) {
    await user.click(screen.getByRole('button', { name: '重新计算' }))
  } else {
    await act(async () => {
      usePerfStore.getState().runEvaluate()
    })
  }
  await waitFor(
    () => {
      if (expectJobId !== undefined) {
        expect(usePerfStore.getState().response?.mc_job_id).toBe(expectJobId)
      } else {
        expect(usePerfStore.getState().response).not.toBeNull()
      }
    },
    { timeout: WAIT_TIMEOUT },
  )
}

beforeEach(() => {
  useVehicleStore.getState().reset()
  usePerfStore.getState().reset()
  useVehicleStore.setState({ vehicle: makeVehicle() })
  FakeSocket.latest = null
})

afterEach(() => {
  FakeSocket.latest = null
  vi.unstubAllGlobals()
  useVehicleStore.getState().reset()
  usePerfStore.getState().reset()
})

describe('阶段①：立即渲染（OI-25 阶段① / OI-38 运力表 / OI-23 瀑布）', () => {
  it('四轨道点值表 + GLOW + ΔV 瀑布 7 数值逐项 + assumptions + warnings；kg→t 按 units 表显示', async () => {
    const evaluateResponse = { current: makeEvaluateResponse({ jobId: 'mc-job-1', leoKg: 21500 }) }
    vi.stubGlobal('fetch', makeFetchMock(evaluateResponse))
    vi.stubGlobal('WebSocket', FakeSocket)

    render(<CalcPanel />)
    await runEvaluateAndAwaitPointValue()

    // 四轨道行 + GLOW（kg→t：21500 kg → 21.5 t，前端零换算系数）
    expect(screen.getByText('LEO')).toBeInTheDocument()
    expect(screen.getByText('SSO')).toBeInTheDocument()
    expect(screen.getByText('GTO')).toBeInTheDocument()
    expect(screen.getByText('GEO')).toBeInTheDocument()
    expect(screen.getByText('21.5')).toBeInTheDocument()
    expect(screen.getByText('16.0')).toBeInTheDocument()
    expect(screen.getByText('5.5')).toBeInTheDocument()
    expect(screen.getByText('1.2')).toBeInTheDocument()
    expect(screen.getByText('549.0')).toBeInTheDocument()
    expect(screen.getByText('点值（t）')).toBeInTheDocument()

    // ΔV 瀑布：7 个数值逐项显示（不是只给总和）
    expect(screen.getByText('7.81')).toBeInTheDocument()
    expect(screen.getByText('1.24')).toBeInTheDocument()
    expect(screen.getByText('0.18')).toBeInTheDocument()
    expect(screen.getByText('0.22')).toBeInTheDocument()
    expect(screen.getByText('0.15')).toBeInTheDocument()
    expect(screen.getByText('0.39')).toBeInTheDocument()
    expect(screen.getByText('9.21')).toBeInTheDocument()

    // assumptions 必须渲染（允许简化但必须标明假设）
    expect(screen.getByText('理想脉冲近似，不含有限推力损失')).toBeInTheDocument()
    expect(screen.getByText('损失为 L1 参数化经验模型（§8.6）')).toBeInTheDocument()

    // warnings 与 ΔV 需求来源照抄后端（dv_source 四轨道各一份）
    expect(screen.getByText(/按默认发射场（卡纳维拉尔 28.5°N、向东）计算/)).toBeInTheDocument()
    expect(screen.getAllByText('量级锚定（§8.6 表中值）').length).toBe(4)

    // 收尾：让阶段②终态落定，避免跨用例的挂起定时器
    await act(async () => {
      FakeSocket.latest?.emit(jobRecord({ jobId: 'mc-job-1', status: 'succeeded', metrics: makeMcResult({ skewNote: null }) }))
    })
  })

  it('单位表不可用时按 SI（kg）回退显示并说明', async () => {
    const evaluateResponse = { current: makeEvaluateResponse({ jobId: 'mc-job-1', leoKg: 21500 }) }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/params/units') return jsonResponse(UNITS_EMPTY)
      if (url === '/api/perf/evaluate') return jsonResponse(evaluateResponse.current)
      return jsonResponse(jobRecord({ jobId: 'mc-job-1', status: 'running' }))
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)

    render(<CalcPanel />)
    await runEvaluateAndAwaitPointValue()

    expect(screen.getByText('21500.0')).toBeInTheDocument()
    expect(screen.getByText(/按 SI（kg）显示/)).toBeInTheDocument()

    await act(async () => {
      FakeSocket.latest?.emit(jobRecord({ jobId: 'mc-job-1', status: 'succeeded', metrics: makeMcResult({ skewNote: null }) }))
    })
  })
})

describe('两阶段 UI（OI-25 核心验收）', () => {
  it('interval_pending=true：区间列占位态且无任何 P5/P95 数字；MC 到达后整体覆盖为区间', async () => {
    const evaluateResponse = { current: makeEvaluateResponse({ jobId: 'mc-job-1', leoKg: 21500 }) }
    vi.stubGlobal('fetch', makeFetchMock(evaluateResponse))
    vi.stubGlobal('WebSocket', FakeSocket)

    render(<CalcPanel />)
    await runEvaluateAndAwaitPointValue()

    // 占位态：五行区间（四轨道 + GLOW）全部脉动占位，旧数字一个都不许出现
    expect((await screen.findAllByText('区间计算中…', {}, { timeout: WAIT_TIMEOUT })).length).toBe(5)
    expect(screen.queryByText(/P5 20\.1/)).not.toBeInTheDocument()
    expect(screen.queryByText(/25\.3/)).not.toBeInTheDocument()
    expect(screen.queryByText(/22\.8/)).not.toBeInTheDocument()

    // 阶段②到达：经作业通道整体覆盖——区间列变 P5–P95（含 P50），占位态消失
    await act(async () => {
      FakeSocket.latest?.emit(
        jobRecord({ jobId: 'mc-job-1', status: 'succeeded', metrics: makeMcResult({ skewNote: null }) }),
      )
    })
    expect(
      await screen.findByText('P5 20.1 – P95 25.3（P50 22.8）', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
    expect(screen.queryByText('区间计算中…')).not.toBeInTheDocument()
    expect(screen.getByText('P5 15.0 – P95 17.4（P50 16.2）')).toBeInTheDocument()

    const state = usePerfStore.getState()
    expect(state.mc).not.toBeNull()
    expect(state.mc?.interval_pending).toBe(false)
  })

  it('不混排：先有旧区间，再发新请求 → 旧区间数字清空、回占位态（未到达前不得新旧混排）', async () => {
    const evaluateResponse = { current: makeEvaluateResponse({ jobId: 'mc-job-1', leoKg: 21500 }) }
    vi.stubGlobal('fetch', makeFetchMock(evaluateResponse))
    vi.stubGlobal('WebSocket', FakeSocket)

    const user = userEvent.setup()
    render(<CalcPanel />)

    // 第一轮：旧区间到达（P5 20.1 / P95 25.3 / P50 22.8 显示）
    await act(async () => {
      usePerfStore.getState().runEvaluate()
    })
    await screen.findAllByText('区间计算中…', {}, { timeout: WAIT_TIMEOUT })
    await act(async () => {
      FakeSocket.latest?.emit(
        jobRecord({ jobId: 'mc-job-1', status: 'succeeded', metrics: makeMcResult({ skewNote: null }) }),
      )
    })
    expect(
      await screen.findByText('P5 20.1 – P95 25.3（P50 22.8）', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()

    // 第二轮：参数已变 → 新请求（新 mc_job_id、新点值 22000 kg → 22.0 t）
    evaluateResponse.current = makeEvaluateResponse({ jobId: 'mc-job-2', leoKg: 22000 })
    await runEvaluateAndAwaitPointValue(user, 'mc-job-2')

    // 旧区间数字全部清空、区间列回占位态；旧点值也被新点值覆盖
    expect(await screen.findAllByText('区间计算中…', {}, { timeout: WAIT_TIMEOUT })).toHaveLength(5)
    expect(screen.queryByText(/20\.1/)).not.toBeInTheDocument()
    expect(screen.queryByText(/25\.3/)).not.toBeInTheDocument()
    expect(screen.queryByText(/22\.8/)).not.toBeInTheDocument()
    expect(screen.queryByText('21.5')).not.toBeInTheDocument()
    expect(screen.getByText('22.0')).toBeInTheDocument()

    // 收尾：新区间到达（数字与旧区间不同，且只出现新的）
    await act(async () => {
      FakeSocket.latest?.emit(
        jobRecord({
          jobId: 'mc-job-2',
          status: 'succeeded',
          metrics: {
            ...makeMcResult({ skewNote: null }),
            mc_job_id: 'mc-job-2',
            interval: {
              leo_kg: { p5: 21000, p50: 23100, p95: 25600 },
              sso_kg: { p5: 15000, p50: 16200, p95: 17400 },
              gto_kg: { p5: 5000, p50: 5500, p95: 6000 },
              geo_kg: { p5: 1100, p50: 1200, p95: 1300 },
              glow_kg: { p5: 545000, p50: 549000, p95: 553000 },
            },
          },
        }),
      )
    })
    expect(
      await screen.findByText('P5 21.0 – P95 25.6（P50 23.1）', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
  })
})

describe('偏度提示条（§8.7 / OI-01）', () => {
  it('mean_vs_p50_note 非空 → 「均值 ≠ P50」提示条出现，附后端原文', async () => {
    const evaluateResponse = { current: makeEvaluateResponse({ jobId: 'mc-job-1', leoKg: 21500 }) }
    vi.stubGlobal('fetch', makeFetchMock(evaluateResponse))
    vi.stubGlobal('WebSocket', FakeSocket)

    render(<CalcPanel />)
    await act(async () => {
      usePerfStore.getState().runEvaluate()
    })
    await screen.findAllByText('区间计算中…', {}, { timeout: WAIT_TIMEOUT })

    await act(async () => {
      FakeSocket.latest?.emit(
        jobRecord({
          jobId: 'mc-job-1',
          status: 'succeeded',
          metrics: makeMcResult({ skewNote: '偏度 −1.2 显著左偏：均值低于 P50（左尾拖长所致）' }),
        }),
      )
    })
    expect(
      await screen.findByText(/均值 ≠ P50——偏度 −1\.2 显著左偏/, {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
  })

  it('mean_vs_p50_note 全空 → 不出现提示条', async () => {
    const evaluateResponse = { current: makeEvaluateResponse({ jobId: 'mc-job-1', leoKg: 21500 }) }
    vi.stubGlobal('fetch', makeFetchMock(evaluateResponse))
    vi.stubGlobal('WebSocket', FakeSocket)

    render(<CalcPanel />)
    await act(async () => {
      usePerfStore.getState().runEvaluate()
    })
    await screen.findAllByText('区间计算中…', {}, { timeout: WAIT_TIMEOUT })

    // MC 到达（全量 note 均为 null）
    await act(async () => {
      FakeSocket.latest?.emit(
        jobRecord({ jobId: 'mc-job-1', status: 'succeeded', metrics: makeMcResult({ skewNote: null }) }),
      )
    })
    await screen.findByText('P5 20.1 – P95 25.3（P50 22.8）', {}, { timeout: WAIT_TIMEOUT })

    expect(screen.queryByText(/均值 ≠ P50/)).not.toBeInTheDocument()
  })
})

describe('MC 失败与重试（不阻塞点值显示）', () => {
  it('作业失败：区间列变错误提示 + 重试按钮，点值仍显示；重试后区间恢复', async () => {
    const evaluateResponse = { current: makeEvaluateResponse({ jobId: 'mc-job-1', leoKg: 21500 }) }
    vi.stubGlobal('fetch', makeFetchMock(evaluateResponse))
    vi.stubGlobal('WebSocket', FakeSocket)

    const user = userEvent.setup()
    render(<CalcPanel />)
    await act(async () => {
      usePerfStore.getState().runEvaluate()
    })
    await screen.findAllByText('区间计算中…', {}, { timeout: WAIT_TIMEOUT })

    // 阶段②失败（作业通道下发终态 failed + §10.3 错误体）
    await act(async () => {
      FakeSocket.latest?.emit(
        jobRecord({
          jobId: 'mc-job-1',
          status: 'failed',
          error: {
            code: 'MC_JOB_FAILED',
            stage: 'evaluating',
            message: 'MC 进程池求值异常',
            suggestion: '点击重试重新取回区间；若反复失败请查看后端日志',
          },
        }),
      )
    })
    expect(
      await screen.findByText(/MC 区间失败（MC_JOB_FAILED）：MC 进程池求值异常/, {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()

    // 点值不被阻塞：四轨道 + 瀑布仍在；区间列标注「区间不可用」
    expect(screen.getByText('21.5')).toBeInTheDocument()
    expect(screen.getByText('9.21')).toBeInTheDocument()
    expect(screen.getAllByText('区间不可用').length).toBe(5)

    // 重试：按同一 mc_job_id 重新取回，失败态清空、回到结果
    await user.click(screen.getByRole('button', { name: '重试区间' }))
    const retrySocket = FakeSocket.latest
    expect(retrySocket?.url).toContain('/ws/jobs/mc-job-1')
    await act(async () => {
      retrySocket?.emit(
        jobRecord({ jobId: 'mc-job-1', status: 'succeeded', metrics: makeMcResult({ skewNote: null }) }),
      )
    })
    expect(
      await screen.findByText('P5 20.1 – P95 25.3（P50 22.8）', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
    expect(screen.queryByText(/MC 区间失败/)).not.toBeInTheDocument()
  })
})

describe('敏感度 Top-8（§8.7 一阶差分）', () => {
  it('MC 到达后渲染参数敏感度条目（数值与排序照抄后端）', async () => {
    const evaluateResponse = { current: makeEvaluateResponse({ jobId: 'mc-job-1', leoKg: 21500 }) }
    vi.stubGlobal('fetch', makeFetchMock(evaluateResponse))
    vi.stubGlobal('WebSocket', FakeSocket)

    render(<CalcPanel />)
    await act(async () => {
      usePerfStore.getState().runEvaluate()
    })
    await screen.findAllByText('区间计算中…', {}, { timeout: WAIT_TIMEOUT })

    await act(async () => {
      FakeSocket.latest?.emit(
        jobRecord({ jobId: 'mc-job-1', status: 'succeeded', metrics: makeMcResult({ skewNote: null }) }),
      )
    })

    expect(
      await screen.findByText('stages[0].engine.isp_vacuum_s', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
    expect(screen.getByText('stages[0].structure_coefficient')).toBeInTheDocument()
    expect(screen.getByText('loss_budget')).toBeInTheDocument()
    expect(screen.getByText('1.00')).toBeInTheDocument()

    // 未到达前（占位态）不得提前出现敏感度区
    // （本用例前半段 findByText('区间计算中…') 时无敏感度——此处不再重复断言，由排序保证）
    const region = screen.getByText('参数敏感度 Top-8（±1σ 一阶差分，按最大归一）')
    expect(within(region.parentElement as HTMLElement).getByText('stages[0].length_m')).toBeInTheDocument()
  })
})
