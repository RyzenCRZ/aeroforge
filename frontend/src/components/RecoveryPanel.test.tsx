import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { Vehicle } from '../api/params'
import type { SequenceReport } from '../api/sequence'
import { useVehicleStore } from '../store/vehicle'
import { RecoveryPanel } from './RecoveryPanel'

/**
 * 回收质量代价面板（§8.9 / M5 验收「回收质量代价可解释」）。
 *
 * 被钉住的契约：
 *
 * 1. **三项分解**（§8.9 规则 3：独立建模、不得合并）：每项 kg + 一句可解释文案；
 *    加总行 = 后端 `inert_cost_kg`（前端绝不自行求和，预留推进剂不计入）；
 * 2. **未启用不隐藏**：`recovery` 未启用时常驻「未启用回收，无代价」，且不发请求；
 * 3. **失败降级**：sequence 请求失败 → 提示行含 code / message / suggestion，不白屏；
 * 4. **数值一律后端下发**（ADR-011）：请求体携带用户输入的目标 ΔV，响应数值只格式化。
 */

/** CI 时序教训：waitFor 显式给足超时（300 ms 防抖 + 真实定时器下偶发慢一拍）。 */
const WAIT_TIMEOUT = 4_000

/** 构造一个结构合法的单级 Vehicle（字段集满足 §6.1 各层的必填项；recovery 可选挂载）。 */
function makeVehicle(recovery?: Vehicle['recovery']): Vehicle {
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
    ...(recovery === undefined ? {} : { recovery }),
  }
}

/** 已启用回收的最小 Recovery 层（其余字段可省略 = 后端按未配置处理）。 */
function enabledRecovery(): NonNullable<Vehicle['recovery']> {
  return { enabled: true, stage_indices: [1], method: 'propulsive' }
}

/** sequence 响应（契约定死的手写形态；三项代价与合计全部来自后端）。 */
function makeReport(patch: Partial<SequenceReport> = {}): SequenceReport {
  return {
    events: [],
    glow_kg: 560_000,
    glow_kg_expendable: 549_000,
    payload_mass_kg: 1000,
    twr_liftoff: 1.3,
    recovery: {
      system_mass_kg: 1200,
      reinforcement_mass_kg: 800,
      landing_propellant_margin_fraction: 0.06,
      landing_propellant_kg: 2100,
      inert_cost_kg: 2000,
      recovered_stage_indices: [1],
    },
    payload_capacity_expendable_kg: 22_000,
    payload_capacity_recoverable_kg: 20_000,
    capacity_penalty_kg: 2000,
    writeback_iterations: 1,
    warnings: [],
    provenance: { 'recovery.model': '§8.9 规则 3' },
    ...patch,
  }
}

type FetchMock = Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response
}

function errorResponse(status: number, body: unknown): Response {
  return { ok: false, status, json: async () => body } as unknown as Response
}

interface FetchRoutes {
  sequence?: () => Response
}

/** fetch 替身：只路由 /api/sizing/sequence；未覆盖路径直接失败。 */
function makeFetchMock(routes: FetchRoutes = {}): FetchMock {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/sizing/sequence') {
      return routes.sequence ? routes.sequence() : jsonResponse(makeReport())
    }
    throw new Error(`测试未覆盖的请求：${url} ${init?.method ?? ''}`)
  })
}

function sequenceCalls(fetchMock: FetchMock): number {
  return fetchMock.mock.calls.filter(([input]) => String(input) === '/api/sizing/sequence').length
}

beforeEach(() => {
  useVehicleStore.getState().reset()
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  useVehicleStore.getState().reset()
})

describe('未启用态（不隐藏面板——可发现性）', () => {
  it('recovery 未启用 → 常驻「未启用回收，无代价」，且不发任何请求', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    useVehicleStore.setState({ vehicle: makeVehicle() })

    render(<RecoveryPanel />)

    expect(screen.getByTestId('recovery-disabled')).toHaveTextContent('未启用回收，无代价')
    // 防抖窗口过去也不发请求：未启用时 sequence 调用毫无意义
    await new Promise((resolve) => {
      setTimeout(resolve, 400)
    })
    expect(sequenceCalls(fetchMock)).toBe(0)
  })

  it('尚未载入参数 → 提示行，不崩溃', () => {
    render(<RecoveryPanel />)

    expect(screen.getByText('尚未载入参数，无法计算回收代价')).toBeInTheDocument()
  })
})

describe('三项分解渲染（数值照抄后端，ADR-011）', () => {
  it('启用回收 + 输入 ΔV → 防抖后请求，三项分解 + 加总行 + 可解释文案呈现', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    useVehicleStore.setState({ vehicle: makeVehicle(enabledRecovery()) })

    render(<RecoveryPanel />)

    // 目标 ΔV 是请求体必填项：未填时不发请求
    await new Promise((resolve) => {
      setTimeout(resolve, 400)
    })
    expect(sequenceCalls(fetchMock)).toBe(0)

    await user.type(screen.getByLabelText(/目标总 ΔV/), '9200')

    // 三项分解：每项 kg 值照抄后端响应
    expect(await screen.findByTestId('recovery-costs', {}, { timeout: WAIT_TIMEOUT })).toBeInTheDocument()
    expect(screen.getByText('1200.0 kg')).toBeInTheDocument()
    expect(screen.getByText('800.0 kg')).toBeInTheDocument()
    expect(screen.getByText('2100.0 kg')).toBeInTheDocument()
    // 加总行 = 后端 inert_cost_kg（2000 = 系统 + 加强；预留 2100 明确不计入）
    expect(screen.getByText('2000.0 kg')).toBeInTheDocument()
    // 可解释文案：加强质量 = 结构加强、预留推进剂 = 回收点火
    expect(screen.getByText(/回收系统本体/)).toBeInTheDocument()
    expect(screen.getByText(/再入加固/)).toBeInTheDocument()
    expect(screen.getByText(/回收点火消耗的预留推进剂/)).toBeInTheDocument()
    // 补充行：运力损失与回收级号（同样后端下发）
    expect(screen.getByTestId('recovery-context')).toHaveTextContent('运力损失 2000.0 kg')
    expect(screen.getByTestId('recovery-context')).toHaveTextContent('回收级号：1')

    // 请求体：目标 ΔV 用用户输入值，vehicle 原样携带
    const call = fetchMock.mock.calls.find(([input]) => String(input) === '/api/sizing/sequence')
    const body = JSON.parse(String(call?.[1]?.body)) as {
      target_delta_v_m_s: number
      vehicle: { name: string }
    }
    expect(body.target_delta_v_m_s).toBe(9200)
    expect(body.vehicle.name).toBe('示例箭')
  })

  it('响应 recovery = null（后端口径未启用）→ 显示「无代价」占位而非崩溃', async () => {
    const fetchMock = makeFetchMock({ sequence: () => jsonResponse(makeReport({ recovery: null })) })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    useVehicleStore.setState({ vehicle: makeVehicle(enabledRecovery()) })

    render(<RecoveryPanel />)
    await user.type(screen.getByLabelText(/目标总 ΔV/), '9200')

    expect(
      await screen.findByText('未启用回收，无代价', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
  })
})

describe('失败降级（§10.3 / 不白屏）', () => {
  it('sequence 返回 422 错误体 → 提示行含 code / message / suggestion', async () => {
    const fetchMock = makeFetchMock({
      sequence: () =>
        errorResponse(422, {
          error: {
            code: 'VALIDATION_ERROR',
            stage: 'api',
            message: '时序耦合求解失败',
            suggestion: '检查 Recovery 层参数后重试',
          },
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    useVehicleStore.setState({ vehicle: makeVehicle(enabledRecovery()) })

    render(<RecoveryPanel />)
    await user.type(screen.getByLabelText(/目标总 ΔV/), '9200')

    expect(
      await screen.findByText(
        /回收代价不可用（VALIDATION_ERROR）：时序耦合求解失败——检查 Recovery 层参数后重试/,
        {},
        { timeout: WAIT_TIMEOUT },
      ),
    ).toBeInTheDocument()
    // 降级不白屏：面板标题与 ΔV 输入仍在
    expect(screen.getByText('回收质量代价')).toBeInTheDocument()
  })

  it('响应契约不符（缺字段）→ 同样走降级提示行', async () => {
    const fetchMock = makeFetchMock({ sequence: () => jsonResponse({ glow_kg: 1 }) })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    useVehicleStore.setState({ vehicle: makeVehicle(enabledRecovery()) })

    render(<RecoveryPanel />)
    await user.type(screen.getByLabelText(/目标总 ΔV/), '9200')

    expect(
      await screen.findByText(/回收代价不可用（CONTRACT_MISMATCH）/, {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
  })

  it('恢复：失败后修正输入重新请求成功 → 错误行让位于代价分解', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    useVehicleStore.setState({ vehicle: makeVehicle(enabledRecovery()) })

    render(<RecoveryPanel />)
    // 先触发一次失败
    fetchMock.mockImplementationOnce(async (input: RequestInfo | URL) => {
      if (String(input) === '/api/sizing/sequence') {
        return errorResponse(500, {
          error: { code: 'HTTP_500', stage: 'api', message: '后端异常', suggestion: '稍后重试' },
        })
      }
      throw new Error(`测试未覆盖的请求：${String(input)}`)
    })
    await user.type(screen.getByLabelText(/目标总 ΔV/), '9200')
    await screen.findByText(/回收代价不可用（HTTP_500）/, {}, { timeout: WAIT_TIMEOUT })

    // 再触发一次变更（追加字符）→ 成功响应覆盖错误
    await user.type(screen.getByLabelText(/目标总 ΔV/), '0')
    await waitFor(
      () => {
        expect(screen.getByTestId('recovery-costs')).toBeInTheDocument()
      },
      { timeout: WAIT_TIMEOUT },
    )
    expect(screen.queryByText(/回收代价不可用/)).not.toBeInTheDocument()
  })
})
