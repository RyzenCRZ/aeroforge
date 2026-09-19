import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { MaterialEntry, MaterialsResponse } from '../api/materials'
import type { DiagnoseResponse, TemplateResponse, Vehicle } from '../api/params'
import { useVehicleStore } from '../store/vehicle'
import { VehiclePanel } from './VehiclePanel'

/**
 * QA-3 材料引用化的 UI 侧（§7.4 / OI-32）。
 *
 * 必须由用例钉死的性质：
 *
 * 1. **选项来自后端材料库**：各层 material 下拉的 options 由
 *    `GET /api/catalog/materials` 下发（九项）；label = 「名称（id）」，
 *    value = 库 id；typical 条目尾缀「[典型值]」（§1.4-4）。
 * 2. **选择走既有通路**：commitField → setField（material 仍是 string，值域 = 库 id）
 *    → 防抖后重发诊断（requestDiagnose 的既有行为）。
 * 3. **降级不白屏**：材料库拉取失败 → 面板顶部一行「材料库不可用」+ 空 options，
 *    其余字段照常渲染。
 */

const DIAGNOSE_OK: DiagnoseResponse = { constraints: [], diagnostics: [], rules: [] }

const UNITS_EMPTY = { units: [] }

const UNMATCHED = { matched: false, template_id: null, name: null, note: null }

/** 材料记录的最小合法形（UI 只消费 id / name / quality，其余字段仅满足契约形状）。 */
function makeMaterial(id: string, name: string, quality: MaterialEntry['quality']): MaterialEntry {
  return {
    id,
    name,
    category: '铝合金',
    density_kg_m3: 2700,
    elastic_modulus_pa: 70e9,
    yield_strength_pa: 300e6,
    service_temp_min_c: -100,
    service_temp_max_c: 150,
    typical_min_wall_thickness_m: 0.0015,
    heat_treatment: 'T6',
    source: '公开手册',
    quality,
    specific_strength_m2_s2: 111_111,
    specific_stiffness_m2_s2: 25_925_926,
  }
}

/** 九条材料：八条 literature + 一条 typical（碳纤维复材 = 工程典型值）。 */
const MATERIALS: MaterialsResponse = {
  materials: [
    makeMaterial('al-2024', '2024铝合金', 'literature'),
    makeMaterial('al-2219', '2219铝合金', 'literature'),
    makeMaterial('al-2195', '2195铝锂合金', 'literature'),
    makeMaterial('ss-301', '301不锈钢', 'literature'),
    makeMaterial('ss-316l', '316L不锈钢', 'literature'),
    makeMaterial('ti-tc4', 'TC4钛合金', 'literature'),
    makeMaterial('cf-epoxy', '碳纤维/环氧复材', 'typical'),
    makeMaterial('gh4169', 'GH4169高温合金', 'literature'),
    makeMaterial('al-7075', '7075铝合金', 'literature'),
  ],
}

/** 构造一个结构合法的单级 Vehicle：三层 material 都引用库内 id（al-2024）。 */
function makeVehicle(): Vehicle {
  return {
    schema_version: '1',
    name: '示例箭',
    payload_mass_kg: 1000,
    material: 'al-2024',
    propellant: 'LOX/RP-1',
    stages: [
      {
        index: 1,
        propellant: 'LOX/RP-1',
        diameter_m: 3.35,
        length_m: 20,
        wall_thickness_m: 0.004,
        material: 'al-2024',
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
            material: 'al-2024',
            fill_fraction: 0.95,
            feed_system: 'pump_fed',
            delivery_pipe_routing: 'external',
          },
          fuel_tank: {
            tank_type: 'separate',
            wall_thickness_m: 0.004,
            material: 'al-2024',
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
  sourced_fields: {},
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

function errorResponse(): Response {
  return {
    ok: false,
    status: 503,
    json: async () => ({
      error: {
        code: 'SERVICE_UNAVAILABLE',
        stage: 'api',
        message: '材料库暂不可用',
        suggestion: '确认后端已启动（GET /api/health）后重试',
      },
    }),
  } as unknown as Response
}

/** 按 URL 分流的 fetch 替身：`materialsOk = false` 时材料端点返回 503。 */
function makeFetchMock(materialsOk: boolean): FetchMock {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') return jsonResponse(DIAGNOSE_OK)
    if (url === '/api/catalog/materials') {
      return materialsOk ? jsonResponse(MATERIALS) : errorResponse()
    }
    if (url === '/api/params/template') return jsonResponse(START)
    if (url === '/api/params/units') return jsonResponse(UNITS_EMPTY)
    if (url.startsWith('/api/templates/match')) return jsonResponse(UNMATCHED)
    throw new Error(`测试未覆盖的请求：${url}`)
  })
}

const diagnoseCalls = (fetchMock: FetchMock) =>
  fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')

beforeEach(() => {
  useVehicleStore.getState().reset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('QA-3 材料引用化下拉（GET /api/catalog/materials）', () => {
  it('各层 material 下拉渲染九项：label 为「名称（id）」，typical 条目尾缀 [典型值]，value = 库 id', async () => {
    vi.stubGlobal('fetch', makeFetchMock(true))
    render(<VehiclePanel />)

    const vehicleSelect = await screen.findByLabelText(/箭体材料/)
    expect(within(vehicleSelect).getAllByRole('option')).toHaveLength(9)
    expect(
      within(vehicleSelect).getByRole('option', { name: '2024铝合金（al-2024）' }),
    ).toBeInTheDocument()
    expect(
      within(vehicleSelect).getByRole('option', { name: '碳纤维/环氧复材（cf-epoxy）[典型值]' }),
    ).toBeInTheDocument()

    // 级层与贮箱层同库同构（field_path 前缀不同，选项同源）
    expect(within(screen.getByLabelText(/该级材料/)).getAllByRole('option')).toHaveLength(9)
    expect(within(screen.getByLabelText(/氧化剂箱材料/)).getAllByRole('option')).toHaveLength(9)
    expect(within(screen.getByLabelText(/燃料箱材料/)).getAllByRole('option')).toHaveLength(9)

    // value = 库 id：起始箭的 al-2024 直接命中选项
    expect(vehicleSelect).toHaveValue('al-2024')
  })

  it('选择典型值材料：store 写入库 id、控件显示 [典型值] 标注，并经防抖重发诊断', async () => {
    const fetchMock = makeFetchMock(true)
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const vehicleSelect = await screen.findByLabelText(/箭体材料/)
    await user.selectOptions(vehicleSelect, 'cf-epoxy')

    // material 仍是 string，值域 = 库 id（store 结构不变，走既有 setField 通路）
    expect(useVehicleStore.getState().vehicle?.material).toBe('cf-epoxy')
    expect(vehicleSelect).toHaveValue('cf-epoxy')
    expect(vehicleSelect).toHaveDisplayValue('碳纤维/环氧复材（cf-epoxy）[典型值]')

    // 既有行为：选择后 200ms 防抖重发诊断（起始箭载入时已发过一次），载荷里已是新库 id
    await waitFor(() => {
      expect(diagnoseCalls(fetchMock)).toHaveLength(2)
    })
    const [, init] = diagnoseCalls(fetchMock)[1]
    expect(JSON.parse(String(init?.body))).toMatchObject({ material: 'cf-epoxy' })

    // 级层同通路（stages[0].material 的前缀路径写入）
    const stageSelect = screen.getByLabelText(/该级材料/)
    await user.selectOptions(stageSelect, 'ti-tc4')
    expect(useVehicleStore.getState().vehicle?.stages[0]?.material).toBe('ti-tc4')
  })

  it('材料库拉取失败：面板顶部出现「材料库不可用」，下拉为空 options，其余字段照常渲染', async () => {
    vi.stubGlobal('fetch', makeFetchMock(false))
    render(<VehiclePanel />)

    expect(await screen.findByText(/材料库不可用/)).toBeInTheDocument()

    const vehicleSelect = await screen.findByLabelText(/箭体材料/)
    expect(within(vehicleSelect).queryAllByRole('option')).toHaveLength(0)

    // 不崩溃：其余字段照常渲染，诊断链路仍在工作
    expect(screen.getByLabelText(/火箭名称/)).toBeInTheDocument()
    expect(screen.getByLabelText(/有效载荷质量/)).toBeInTheDocument()
    expect(screen.getByLabelText(/该级材料/)).toBeInTheDocument()
    await waitFor(() => {
      expect(useVehicleStore.getState().report).not.toBeNull()
    })
  })
})
