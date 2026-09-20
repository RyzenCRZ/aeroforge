import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { DiagnoseResponse, TemplateResponse, Vehicle } from '../api/params'
import type { TemplateMatchResponse } from '../api/templates'
import type {
  SourcedField,
  VehicleRecordResponse,
  VehicleSearchResponse,
} from '../api/vehicleSearch'
import { useVehicleStore } from '../store/vehicle'
import { VehiclePanel } from './VehiclePanel'

/**
 * OI-39 型号检索候选下拉与 GCAT 已知参数集载入（规格 §7.7 / §11.5）。
 *
 * 几条必须由用例钉死的性质：
 *
 * 1. **候选下拉只呈现、点选才载入**：系统不静默代选；空名 / 单字符连请求都不发。
 * 2. **点选载入保留用户输入**（与模板载入同一纪律）：名称与发射场不回写；
 *    GCAT 缺失的字段留空不造值；GCAT 级数多于骨架时多余级不建。
 * 3. **GCAT 出处随写随记**：写入字段的标注 = 后端下发的溯源锚点。
 * 4. **精校置顶但通路不混**：模板命中排在 GCAT 候选之前并标「精校」，
 *    点击仍走既有 OI-34 模板载入通路。
 * 5. **reference_only 必须可见**：数据缺口较多的记录载入后给出参照提示。
 */

const DIAGNOSE_OK: DiagnoseResponse = { constraints: [], diagnostics: [], rules: [] }

const UNITS_EMPTY = { units: [] }

/** 构造一个结构合法的单级 Vehicle（与 TemplateMatch.test.tsx 同一夹具口径）。 */
function makeVehicle(input: {
  name: string
  payloadMassKg: number
  thrustSeaLevelN: number
  mission: Vehicle['mission']
}): Vehicle {
  return {
    schema_version: '1',
    name: input.name,
    payload_mass_kg: input.payloadMassKg,
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
          thrust_sea_level_n: input.thrustSeaLevelN,
          thrust_vacuum_n: input.thrustSeaLevelN * 1.13,
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
    mission: input.mission,
  }
}

const START_VEHICLE = makeVehicle({
  name: '示例箭',
  payloadMassKg: 1000,
  thrustSeaLevelN: 3_000_000,
  mission: {
    orbit_type: 'LEO',
    altitude_m: 500_000,
    inclination_deg: 97,
    launch_site: { name: '酒泉', latitude_deg: 40.96, altitude_m: 1000, azimuth_deg: 95 },
  },
})

const START: TemplateResponse = {
  template_id: 'starter',
  label: '示例骨架（未经来源核对）',
  note: '占位说明：以下数值未经来源核对。',
  sourced_fields: { payload_mass_kg: '示例骨架占位来源' },
  vehicle: START_VEHICLE,
}

const DETAIL_VEHICLE = makeVehicle({
  name: '长三乙',
  payloadMassKg: 11_500,
  thrustSeaLevelN: 4_600_000,
  mission: {
    orbit_type: 'GTO',
    altitude_m: 35_786_000,
    inclination_deg: 0,
    launch_site: { name: '西昌', latitude_deg: 28.2, altitude_m: 1500, azimuth_deg: 96 },
  },
})

const TEMPLATE_DETAIL = {
  id: 'cz-3b',
  name: '长三乙',
  note: '与 §13.2 基准表同源的公开数据。',
  aliases: ['CZ-3B', '长三乙'],
  reference_payload_leo_kg: 11_500,
  sourced_fields: { payload_mass_kg: '公开手册 p.12' },
  vehicle: DETAIL_VEHICLE,
}

const MATCHED: TemplateMatchResponse = {
  matched: true,
  template_id: 'cz-3b',
  name: '长三乙',
  note: '来源已核对',
}

const UNMATCHED: TemplateMatchResponse = { matched: false, template_id: null, name: null, note: null }

/** SourcedField 便捷构造（来源 = 溯源锚点原样）。 */
function sf(value: number | string | null, source: string): SourcedField {
  return { value, source, unit_uncertain: false }
}

const LV_SOURCE = 'GCAT gcat-2026Q3 lv#175'
const ST1_SOURCE = 'GCAT gcat-2026Q3 st#132'
const ST2_SOURCE = 'GCAT gcat-2026Q3 st#138'

const SEARCH_RESPONSE: VehicleSearchResponse = {
  query: 'falcon 9',
  hits: [
    {
      record_id: 175,
      name: 'Falcon 9',
      variant: null,
      family: 'Falcon9',
      country: 'US',
      stage_count: 2,
      availability: { glow: true, length_m: true, diameter_m: true, payload_leo_kg: true },
    },
  ],
}

/** GCAT 已知参数集夹具：payload/stage 可得，其余按 null 缺失（禁止编造）。 */
const GCAT_RECORD: VehicleRecordResponse = {
  name: 'Falcon 9',
  variant: null,
  record_id: 175,
  quality: 'literature',
  snapshot: { id: 'gcat-2026Q3', release: '1.8.7', total_records: 6000 },
  vehicle: {
    name: sf('Falcon 9', LV_SOURCE),
    family: sf('Falcon9', LV_SOURCE),
    variant: sf(null, LV_SOURCE),
    manufacturer: sf('SpaceX', LV_SOURCE),
    min_stage_no: sf(1, LV_SOURCE),
    max_stage_no: sf(2, LV_SOURCE),
    length_m: sf(54.9, LV_SOURCE),
    diameter_m: sf(3.65, LV_SOURCE),
    launch_mass_kg: sf(333_400, LV_SOURCE),
    payload_leo_kg: sf(9_300, LV_SOURCE),
    payload_gto_kg: sf(null, LV_SOURCE),
    liftoff_thrust_n: sf(null, LV_SOURCE),
    vehicle_class: sf('O', LV_SOURCE),
  },
  stages: [
    {
      stage_no: ' 1',
      qualifier: null,
      missing_reference: false,
      record: {
        name: sf('Falcon 9 St 1', ST1_SOURCE),
        family: sf(null, ST1_SOURCE),
        manufacturer: sf(null, ST1_SOURCE),
        length_m: sf(29, ST1_SOURCE),
        diameter_m: sf(3.65, ST1_SOURCE),
        full_mass_kg: sf(null, ST1_SOURCE),
        dry_mass_kg: sf(null, ST1_SOURCE),
        thrust_vacuum_n: sf(null, ST1_SOURCE),
        thrust_sea_level_n: sf(null, ST1_SOURCE),
        burn_duration_s: sf(null, ST1_SOURCE),
        engine_name: sf('Merlin 1C', ST1_SOURCE),
        engine_count: sf(9, ST1_SOURCE),
      },
    },
    {
      stage_no: ' 2',
      qualifier: null,
      missing_reference: false,
      record: {
        name: sf('Falcon 9 St 2', ST2_SOURCE),
        family: sf(null, ST2_SOURCE),
        manufacturer: sf(null, ST2_SOURCE),
        length_m: sf(10, ST2_SOURCE),
        diameter_m: sf(3.65, ST2_SOURCE),
        full_mass_kg: sf(null, ST2_SOURCE),
        dry_mass_kg: sf(null, ST2_SOURCE),
        thrust_vacuum_n: sf(null, ST2_SOURCE),
        thrust_sea_level_n: sf(null, ST2_SOURCE),
        burn_duration_s: sf(null, ST2_SOURCE),
        engine_name: sf('Merlin 1C-Vac', ST2_SOURCE),
        engine_count: sf(1, ST2_SOURCE),
      },
    },
  ],
  engines: [],
  missing: [
    'vehicle.variant',
    'vehicle.payload_gto_kg',
    'vehicle.liftoff_thrust_n',
    'stages[0].family',
    'stages[0].full_mass_kg',
  ],
  warnings: [],
  reference_only: false,
}

type FetchMock = Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response
}

/** 按 URL 分流的 fetch 替身：面板挂载三件套 + OI-34 模板两端点 + OI-39 检索两端点。 */
function makeFetchMock(options: {
  match?: TemplateMatchResponse
  search?: VehicleSearchResponse
  record?: VehicleRecordResponse
}): FetchMock {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') return jsonResponse(DIAGNOSE_OK)
    if (url.startsWith('/api/catalog/vehicles/search')) {
      return jsonResponse(options.search ?? { query: '', hits: [] })
    }
    if (url.startsWith('/api/catalog/vehicles/record')) {
      return jsonResponse(options.record ?? GCAT_RECORD)
    }
    if (url.startsWith('/api/templates/match')) return jsonResponse(options.match ?? UNMATCHED)
    if (url.startsWith('/api/templates/')) return jsonResponse(TEMPLATE_DETAIL)
    if (url === '/api/templates') return jsonResponse({ templates: [] })
    if (url === '/api/params/template') return jsonResponse(START)
    if (url === '/api/params/units') return jsonResponse(UNITS_EMPTY)
    throw new Error(`测试未覆盖的请求：${url}`)
  })
}

function searchCalls(fetchMock: FetchMock) {
  return fetchMock.mock.calls.filter(([url]) =>
    String(url).startsWith('/api/catalog/vehicles/search'),
  )
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

beforeEach(() => {
  useVehicleStore.getState().reset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('OI-39 型号检索候选下拉', () => {
  it('候选渲染：名称 + 变体 + 国家 + 级数 + 可用性勾叉，300ms 防抖只发一次且 query 已编码', async () => {
    const fetchMock = makeFetchMock({ search: SEARCH_RESPONSE })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, 'falcon 9')

    const hit = await screen.findByRole('button', { name: '载入 GCAT 记录 Falcon 9' })
    expect(hit).toBeInTheDocument()
    expect(screen.getByText(/US · 2 级 · 质量✓ · 长度✓ · 直径✓ · LEO✓/)).toBeInTheDocument()

    const calls = searchCalls(fetchMock)
    expect(calls).toHaveLength(1)
    expect(String(calls[0][0])).toBe(
      `/api/catalog/vehicles/search?name=${encodeURIComponent('falcon 9')}`,
    )
  })

  it('空名与单字符不发起检索请求（宁漏勿错）', async () => {
    const fetchMock = makeFetchMock({ search: SEARCH_RESPONSE })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, 'F')

    // 先等起始箭触发的诊断落定，再越过检索防抖窗口断言：若发了请求这里必然已计数。
    await waitFor(() => {
      expect(useVehicleStore.getState().report).not.toBeNull()
    })
    await act(async () => {
      await sleep(400)
    })
    expect(searchCalls(fetchMock)).toHaveLength(0)
    expect(screen.queryByRole('list')).not.toBeInTheDocument()

    // 清空后同样不请求
    await user.clear(nameInput)
    await act(async () => {
      await sleep(400)
    })
    expect(searchCalls(fetchMock)).toHaveLength(0)
  })

  it('检索无命中：给出未找到提示（不静默）', async () => {
    const fetchMock = makeFetchMock({ search: { query: 'zzzz', hits: [] } })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, 'zzzz')

    expect(await screen.findByText(/未找到与/)).toBeInTheDocument()
  })
})

describe('OI-39 GCAT 已知参数集载入', () => {
  it('点选候选：名称与发射场保留，可得字段写入并带 GCAT 出处，缺失不造值', async () => {
    const fetchMock = makeFetchMock({ search: SEARCH_RESPONSE, record: GCAT_RECORD })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, 'falcon 9')

    // 用户输入是小写别名，点选后**不回写**为 GCAT 谱名
    await user.click(await screen.findByRole('button', { name: '载入 GCAT 记录 Falcon 9' }))
    await screen.findByText(/已从 GCAT gcat-2026Q3 载入 Falcon 9 的已知参数/)

    // 点选取详情用的是候选的规范名（不带变体）；URLSearchParams 把空格编码为 '+'（合法形态）
    const recordCall = fetchMock.mock.calls.find(([url]) =>
      String(url).startsWith('/api/catalog/vehicles/record'),
    )
    expect(String(recordCall?.[0])).toBe('/api/catalog/vehicles/record?name=Falcon+9')

    // 名称与发射场保留用户输入
    expect(useVehicleStore.getState().vehicle?.name).toBe('falcon 9')
    expect(useVehicleStore.getState().vehicle?.mission.launch_site?.name).toBe('酒泉')
    // 可得字段写入：LEO 运力 → 有效载荷质量；一级长径/台数来自 GCAT
    expect(screen.getByLabelText(/有效载荷质量/)).toHaveValue(9_300)
    const state = useVehicleStore.getState()
    expect(state.sourcedFields['payload_mass_kg']).toBe(LV_SOURCE)
    expect(state.sourcedFields['stages[0].length_m']).toBe(ST1_SOURCE)
    expect(state.vehicle?.stages[0].length_m).toBe(29)
    expect(state.vehicle?.stages[0].engine_count).toBe(9)
    // GCAT 有两级而骨架只有一级：多余级**不建**（不造结构）
    expect(state.vehicle?.stages).toHaveLength(1)
    // 候选下拉收起
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('reference_only=true：数据缺口提示条必须出现', async () => {
    const fetchMock = makeFetchMock({
      search: SEARCH_RESPONSE,
      record: { ...GCAT_RECORD, reference_only: true },
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, 'falcon 9')
    await user.click(await screen.findByRole('button', { name: '载入 GCAT 记录 Falcon 9' }))

    expect(
      await screen.findByText('GCAT 数据缺口较多，仅可作参照，建模需手动补参'),
    ).toBeInTheDocument()
  })

  it('装配警告原样透传（缺失引用可见，不吞不掉）', async () => {
    const fetchMock = makeFetchMock({
      search: SEARCH_RESPONSE,
      record: { ...GCAT_RECORD, warnings: ["stage_links 指向的级 'GO' 在 stages 表中不存在"] },
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, 'falcon 9')
    await user.click(await screen.findByRole('button', { name: '载入 GCAT 记录 Falcon 9' }))

    expect(await screen.findByText(/GCAT 装配提示：.*'GO'/)).toBeInTheDocument()
  })
})

describe('OI-39 精校模板置顶（与 GCAT 候选同列，通路不混）', () => {
  it('模板命中排在 GCAT 候选之前并标「精校」；点击走模板载入通路', async () => {
    const fetchMock = makeFetchMock({ match: MATCHED, search: SEARCH_RESPONSE })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, '长三乙')

    // OI-34 提示条照常出现（两条通路 UI 不混）
    await screen.findByText(/检测到已知型号/)
    await screen.findByRole('button', { name: '载入 GCAT 记录 Falcon 9' })

    const pinned = screen.getByText('精校').closest('button')
    const gcatHit = screen.getByRole('button', { name: '载入 GCAT 记录 Falcon 9' })
    const list = screen.getByRole('list')
    const buttons = Array.from(list.querySelectorAll('button')) as HTMLElement[]
    expect(buttons.indexOf(pinned!)).toBeLessThan(buttons.indexOf(gcatHit))

    // 点「精校」→ 走模板载入（/api/templates/cz-3b），GCAT record 端点不被调用
    await user.click(pinned!)
    expect(await screen.findByText('公开手册 p.12')).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes('/api/catalog/vehicles/record')),
    ).toBe(false)
    expect(useVehicleStore.getState().sourcedFields['payload_mass_kg']).toBe('公开手册 p.12')
  })
})
