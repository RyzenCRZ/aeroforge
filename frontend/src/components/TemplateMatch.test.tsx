import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent, { type UserEvent } from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { DiagnoseResponse, TemplateResponse, Vehicle } from '../api/params'
import type { TemplateDetail, TemplateMatchResponse } from '../api/templates'
import { useVehicleStore } from '../store/vehicle'
import { VehiclePanel } from './VehiclePanel'

/**
 * OI-34 型号名称匹配提示条与模板载入流（规格 §11.5 ⑤ 规则 3/5）。
 *
 * 几条必须由用例钉死的性质：
 *
 * 1. **命中只提示、不静默改参**：提示条 + 两个按钮，载不载入由用户决定；
 *    未命中 / 空名**无任何提示**（宁漏勿错），空名连请求都不发。
 * 2. **载入不覆盖用户输入**（规则 3）：名称与 mission 里的发射场保留，其余整套换成模板值。
 * 3. **出处标注的转换**（规则 5）：模板值被用户改动后，该字段标注转为「用户修改」。
 * 4. **[忽略] 的记忆**：同一名称本轮内不再提示；名称再次变更后重新匹配。
 */

const DIAGNOSE_OK: DiagnoseResponse = { constraints: [], diagnostics: [], rules: [] }

const UNITS_EMPTY = { units: [] }

/** 构造一个结构合法的单级 Vehicle（字段集满足 §6.1 各层的必填项）。 */
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

/** 模板值与起始箭刻意不同：载入后断言「换了什么、留了什么」才有区分度。 */
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

const START: TemplateResponse = {
  template_id: 'starter',
  label: '示例骨架（未经来源核对）',
  note: '占位说明：以下数值未经来源核对。',
  sourced_fields: { payload_mass_kg: '示例骨架占位来源' },
  vehicle: START_VEHICLE,
}

const DETAIL: TemplateDetail = {
  id: 'cz-3b',
  name: '长三乙',
  note: '与 §13.2 基准表同源的公开数据。',
  aliases: ['CZ-3B', '长三乙'],
  reference_payload_leo_kg: 11_500,
  sourced_fields: {
    payload_mass_kg: '公开手册 p.12',
    'stages[0].engine.thrust_sea_level_n': '公开手册 p.15',
  },
  vehicle: DETAIL_VEHICLE,
}

/** 命中走**别名**（CZ-3B）：载入后名称保留断言才有意义——保留的是用户输入的别名而非模板名。 */
const MATCHED: TemplateMatchResponse = {
  matched: true,
  template_id: 'cz-3b',
  name: '长三乙',
  note: '来源已核对',
}

const UNMATCHED: TemplateMatchResponse = { matched: false, template_id: null, name: null, note: null }

type FetchMock = Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response
}

/** 按 URL 分流的 fetch 替身：覆盖面板挂载时的三件套 + 模板三端点。 */
function makeFetchMock(matchResponse: TemplateMatchResponse): FetchMock {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (init?.method === 'POST') return jsonResponse(DIAGNOSE_OK)
    if (url.startsWith('/api/templates/match')) return jsonResponse(matchResponse)
    if (url.startsWith('/api/templates/')) return jsonResponse(DETAIL)
    if (url === '/api/templates') return jsonResponse({ templates: [] })
    if (url === '/api/params/template') return jsonResponse(START)
    if (url === '/api/params/units') return jsonResponse(UNITS_EMPTY)
    throw new Error(`测试未覆盖的请求：${url}`)
  })
}

function matchCalls(fetchMock: FetchMock) {
  return fetchMock.mock.calls.filter(([url]) => String(url).startsWith('/api/templates/match'))
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

/** 走完整载入流：输入别名 → 提示条 → 点[载入参数] → 等模板出处标注出现。 */
async function loadTemplateViaNameMatch(user: UserEvent): Promise<void> {
  const nameInput = await screen.findByLabelText(/火箭名称/)
  await user.clear(nameInput)
  await user.type(nameInput, 'CZ-3B')
  await screen.findByText(/检测到已知型号/)
  await user.click(screen.getByRole('button', { name: '载入参数' }))
  await screen.findByText('公开手册 p.12')
}

beforeEach(() => {
  useVehicleStore.getState().reset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('OI-34 名称匹配提示条（§11.5 ⑤ 规则 5）', () => {
  it('别名命中：300ms 防抖后出提示条，逐字符输入只发一次请求且 query 已编码', async () => {
    const fetchMock = makeFetchMock(MATCHED)
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, 'CZ-3B')

    expect(
      await screen.findByText(/检测到已知型号 长三乙（来源已核对）——载入其参数？/),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '载入参数' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '忽略' })).toBeInTheDocument()

    const calls = matchCalls(fetchMock)
    expect(calls).toHaveLength(1)
    expect(String(calls[0][0])).toBe(`/api/templates/match?name=${encodeURIComponent('CZ-3B')}`)
  })

  it('未命中：不出现提示条（宁漏勿错）', async () => {
    const fetchMock = makeFetchMock(UNMATCHED)
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, '无名之箭')

    await waitFor(() => {
      expect(matchCalls(fetchMock)).toHaveLength(1)
    })
    expect(screen.queryByText(/检测到已知型号/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '载入参数' })).not.toBeInTheDocument()
  })

  it('空名不发起匹配请求', async () => {
    const fetchMock = makeFetchMock(MATCHED)
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)

    // 先等起始箭触发的诊断落定，再越过匹配防抖窗口断言：
    // 若实现错误地发了请求，这里必然已计数。sleep 包进 act：期间落定的异步更新在 act 内消化。
    await waitFor(() => {
      expect(useVehicleStore.getState().report).not.toBeNull()
    })
    await act(async () => {
      await sleep(400)
    })
    expect(matchCalls(fetchMock)).toHaveLength(0)
    expect(screen.queryByText(/检测到已知型号/)).not.toBeInTheDocument()
  })

  it('载入参数：name 与发射场保留，其余字段换成模板值，出处标注出现', async () => {
    const fetchMock = makeFetchMock(MATCHED)
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    await loadTemplateViaNameMatch(user)

    // 用户名称（输入的别名）保留——模板名「长三乙」不回写
    expect(screen.getByLabelText(/火箭名称/)).toHaveValue('CZ-3B')
    // 其余字段整套换成模板值
    expect(screen.getByLabelText(/有效载荷质量/)).toHaveValue(11_500)
    expect(screen.getByLabelText(/目标轨道/)).toHaveValue('GTO')
    // 发射场保留起始箭的「酒泉」（模板详情里是「西昌」，规则 3 禁止覆盖）
    const state = useVehicleStore.getState()
    expect(state.vehicle?.name).toBe('CZ-3B')
    expect(state.vehicle?.mission.launch_site?.name).toBe('酒泉')
    expect(state.sourcedFields['payload_mass_kg']).toBe('公开手册 p.12')
    // 提示条收起
    expect(screen.queryByText(/检测到已知型号/)).not.toBeInTheDocument()
    expect(screen.getByText('公开手册 p.15')).toBeInTheDocument()
  })

  it('模板值被用户改动后，该字段出处标注转为「用户修改」', async () => {
    const fetchMock = makeFetchMock(MATCHED)
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    await loadTemplateViaNameMatch(user)

    const payloadInput = screen.getByLabelText(/有效载荷质量/)
    await user.type(payloadInput, '0') // 11500 → 115000：一次提交即算改动

    expect(await screen.findByText('用户修改')).toBeInTheDocument()
    expect(screen.queryByText('公开手册 p.12')).not.toBeInTheDocument()
    // 未被改动的字段保持模板出处
    expect(screen.getByText('公开手册 p.15')).toBeInTheDocument()
  })

  it('忽略后：同一名称不再提示；名称再次变更后重新匹配', async () => {
    const fetchMock = makeFetchMock(MATCHED)
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    render(<VehiclePanel />)

    const nameInput = await screen.findByLabelText(/火箭名称/)
    await user.clear(nameInput)
    await user.type(nameInput, 'CZ-3B')
    await screen.findByText(/检测到已知型号/)

    await user.click(screen.getByRole('button', { name: '忽略' }))
    expect(screen.queryByText(/检测到已知型号/)).not.toBeInTheDocument()

    // 同名称重新输入：匹配请求照发（重新匹配），但同名不再提示
    await user.clear(nameInput)
    await user.type(nameInput, 'CZ-3B')
    await waitFor(() => {
      expect(matchCalls(fetchMock)).toHaveLength(2)
    })
    await act(async () => {
      await sleep(100)
    })
    expect(screen.queryByText(/检测到已知型号/)).not.toBeInTheDocument()

    // 名称再次变更后重新匹配：新名称命中则重新出提示
    await user.clear(nameInput)
    await user.type(nameInput, '长征三号乙改')
    await waitFor(() => {
      expect(matchCalls(fetchMock)).toHaveLength(3)
    })
    expect(
      await screen.findByText(/检测到已知型号 长三乙（来源已核对）——载入其参数？/),
    ).toBeInTheDocument()
  })
})
