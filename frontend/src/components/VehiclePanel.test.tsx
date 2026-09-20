import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { DiagnoseResponse, TemplateResponse, Vehicle } from '../api/params'
import { useVehicleStore } from '../store/vehicle'
import { VehiclePanel } from './VehiclePanel'

/**
 * 参数面板扩展（M4 第四片遗留待办 m4ui）：
 *
 * 1. **扁度系数（OI-37）**：`flatness_ratio` 数值输入写入 store；空 = 默认 0.5
 *    （清空写回 null，后端按 2:1 椭圆封头处理）；placeholder 注明。
 * 2. **助推器组（OI-36）**：新增 / 删除 / 编辑走**既有 field_path 通路**
 *    （`boosters[0].stage.diameter_m` 等，下标解析复用 fieldPath.ts）；新增组骨架
 *    = 芯一级克隆（级号 0，前端不造数）。
 */

/** CI 时序教训：waitFor 显式给足超时（诊断防抖 200 ms + 挂载链路偶尔慢一拍）。 */
const WAIT_TIMEOUT = 4_000

const DIAGNOSE_OK: DiagnoseResponse = { constraints: [], diagnostics: [], rules: [] }

const UNITS_EMPTY = { units: [] }

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

const START: TemplateResponse = {
  template_id: 'starter',
  label: '示例骨架（未经来源核对）',
  note: '占位说明：以下数值未经来源核对。',
  sourced_fields: { payload_mass_kg: '示例骨架占位来源' },
  vehicle: makeVehicle(),
}

type FetchMock = Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response
}

/** 按路由分流的 fetch 替身：覆盖面板挂载三件套（template / units / materials）+ 诊断。 */
function makeFetchMock(): FetchMock {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') return jsonResponse(DIAGNOSE_OK)
    if (url === '/api/params/template') return jsonResponse(START)
    if (url === '/api/params/units') return jsonResponse(UNITS_EMPTY)
    if (url === '/api/catalog/materials') return jsonResponse({ materials: [] })
    throw new Error(`测试未覆盖的请求：${url}`)
  })
}

function diagnoseCalls(fetchMock: FetchMock): number {
  return fetchMock.mock.calls.filter(
    ([url, init]) => init?.method === 'POST' && String(url) === '/api/params/diagnose',
  ).length
}

beforeEach(() => {
  useVehicleStore.getState().reset()
})

afterEach(() => {
  vi.unstubAllGlobals()
  useVehicleStore.getState().reset()
})

describe('扁度系数（OI-37：封头短长轴比，空 = 默认 0.5）', () => {
  it('数值输入写入 store（stages[0].flatness_ratio）；清空写回 null；placeholder 注明默认', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    const { container } = render(<VehiclePanel />)
    await screen.findByLabelText(/火箭名称/, {}, { timeout: WAIT_TIMEOUT })

    const flatness = await screen.findByLabelText(/扁度系数（封头短长轴比）/, {}, { timeout: WAIT_TIMEOUT })
    expect(flatness).toHaveAttribute('data-field-path', 'stages[0].flatness_ratio')
    // placeholder 注明省略语义：空 = 0.5（2:1 椭圆封头）
    expect(flatness).toHaveAttribute('placeholder', '空 = 0.5（2:1 椭圆封头）')
    // 起始箭不带扁度 → 控件为空（不是 0——「没填」不得渲染成 0，§1.4-4）
    expect((flatness as HTMLInputElement).value).toBe('')

    await user.type(flatness, '0.6')
    expect(useVehicleStore.getState().vehicle?.stages[0]?.flatness_ratio).toBe(0.6)

    // 清空 = 写回 null（从载荷移除，后端按 0.5 处理）
    await user.clear(flatness)
    expect(useVehicleStore.getState().vehicle?.stages[0]?.flatness_ratio).toBeNull()

    // 面板上只有一个扁度控件（booster 未添加时不重复渲染）
    expect(container.querySelectorAll('[data-field-path="stages[0].flatness_ratio"]')).toHaveLength(1)
  })
})

describe('助推器组（OI-36：field_path 通路 boosters[i].…）', () => {
  it('新增组 → 芯一级克隆 + 级号 0 + count 2；删除组 → boosters 清空', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<VehiclePanel />)
    await screen.findByLabelText(/火箭名称/, {}, { timeout: WAIT_TIMEOUT })
    expect(useVehicleStore.getState().vehicle?.boosters).toBeUndefined()

    await user.click(screen.getByRole('button', { name: '新增助推器组' }))

    const state = useVehicleStore.getState()
    expect(state.vehicle?.boosters).toHaveLength(1)
    // 侧级骨架 = 芯一级克隆（级号改 0，数值来自用户自己的输入）
    expect(state.vehicle?.boosters?.[0]?.stage.index).toBe(0)
    expect(state.vehicle?.boosters?.[0]?.stage.diameter_m).toBe(3.35)
    expect(state.vehicle?.boosters?.[0]?.count).toBe(2)
    expect(state.vehicle?.boosters?.[0]?.separation_s).toBeNull()

    // 组标题与展开控件（新组默认展开）
    expect(screen.getByText(/助推器组 1（boosters\[0\] · 2 枚）/)).toBeInTheDocument()
    expect(screen.getByText('并联助推器（boosters）')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '删除本组' }))
    expect(useVehicleStore.getState().vehicle?.boosters).toHaveLength(0)
    expect(screen.queryByText(/助推器组 1/)).not.toBeInTheDocument()
  })

  it('编辑侧级核心数值字段：field_path 取值与后端口径一致（boosters[0].stage.…）并写入 store', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    const { container } = render(<VehiclePanel />)
    await screen.findByLabelText(/火箭名称/, {}, { timeout: WAIT_TIMEOUT })

    await user.click(screen.getByRole('button', { name: '新增助推器组' }))

    const boosterDiameter = container.querySelector(
      '[data-field-path="boosters[0].stage.diameter_m"]',
    ) as HTMLInputElement
    expect(boosterDiameter).not.toBeNull()
    await user.clear(boosterDiameter)
    await user.type(boosterDiameter, '2.2')
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.stage.diameter_m).toBe(2.2)

    const boosterLength = container.querySelector(
      '[data-field-path="boosters[0].stage.length_m"]',
    ) as HTMLInputElement
    await user.clear(boosterLength)
    await user.type(boosterLength, '16')
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.stage.length_m).toBe(16)

    const boosterSigma = container.querySelector(
      '[data-field-path="boosters[0].stage.structure_coefficient"]',
    ) as HTMLInputElement
    await user.clear(boosterSigma)
    await user.type(boosterSigma, '0.1')
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.stage.structure_coefficient).toBe(0.1)

    const boosterFill = container.querySelector(
      '[data-field-path="boosters[0].stage.fill_fraction"]',
    ) as HTMLInputElement
    await user.clear(boosterFill)
    await user.type(boosterFill, '0.9')
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.stage.fill_fraction).toBe(0.9)

    const boosterThrust = container.querySelector(
      '[data-field-path="boosters[0].stage.engine.thrust_sea_level_n"]',
    ) as HTMLInputElement
    await user.clear(boosterThrust)
    await user.type(boosterThrust, '1200000')
    expect(
      useVehicleStore.getState().vehicle?.boosters?.[0]?.stage.engine.thrust_sea_level_n,
    ).toBe(1_200_000)

    const boosterIsp = container.querySelector(
      '[data-field-path="boosters[0].stage.engine.isp_vacuum_s"]',
    )
    // QA-1：default 比冲语义下侧级不存比冲——展开控件不渲染这两个字段（载荷不含）
    expect(boosterIsp).toBeNull()

    // 组级参数：count 与 separation_s（空 = null → 芯一级关机时刻）
    const count = screen.getByLabelText(/并联数量/)
    await user.clear(count)
    await user.type(count, '4')
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.count).toBe(4)

    const separation = screen.getByLabelText(/分离时刻/)
    expect(separation).toHaveAttribute('data-field-path', 'boosters[0].separation_s')
    expect(separation).toHaveAttribute('placeholder', '空 = 芯一级关机时刻')
    await user.type(separation, '150')
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.separation_s).toBe(150)
    await user.clear(separation)
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.separation_s).toBeNull()

    // 增删经防抖触发诊断重算（§11.4：结构变更也算参数变更）
    await waitFor(
      () => {
        expect(diagnoseCalls(fetchMock)).toBeGreaterThanOrEqual(2)
      },
      { timeout: WAIT_TIMEOUT },
    )
  })

  it('多组并存：第二组的 field_path 下标正确（boosters[1].…）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    const { container } = render(<VehiclePanel />)
    await screen.findByLabelText(/火箭名称/, {}, { timeout: WAIT_TIMEOUT })

    await user.click(screen.getByRole('button', { name: '新增助推器组' }))
    await user.click(screen.getByRole('button', { name: '新增助推器组' }))

    expect(useVehicleStore.getState().vehicle?.boosters).toHaveLength(2)
    const secondCount = container.querySelector(
      '[data-field-path="boosters[1].count"]',
    ) as HTMLInputElement
    expect(secondCount).not.toBeNull()
    await user.clear(secondCount)
    await user.type(secondCount, '3')
    expect(useVehicleStore.getState().vehicle?.boosters?.[1]?.count).toBe(3)
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.count).toBe(2)

    // 删除第二组：第一组仍在，下标不漂移
    const removeButtons = screen.getAllByRole('button', { name: '删除本组' })
    await user.click(removeButtons[1])
    expect(useVehicleStore.getState().vehicle?.boosters).toHaveLength(1)
    expect(useVehicleStore.getState().vehicle?.boosters?.[0]?.count).toBe(2)
  })
})
