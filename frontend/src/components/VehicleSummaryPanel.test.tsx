import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { JobRecord } from '../api/geometry'
import type { Vehicle } from '../api/params'
import { REPORT_EXPORT_FORMATS, type VehicleSummaryResponse } from '../api/vehicleSummary'
import { usePerfStore } from '../store/perf'
import { useVehicleStore } from '../store/vehicle'
import { VehicleSummaryPanel } from './VehicleSummaryPanel'

/**
 * 整箭数据 + 轨道运力面板（FR-10 / FR-11 / OI-04 冻结语义 / §11.12 报告导出）。
 *
 * 被钉住的契约：
 *
 * 1. **七字段渲染**：总质量 kg+t 双值（t 用后端 glow_t，前端零换算）、推进剂 / 干重
 *    （按单位表 factor 显示）、总高 / 最大直径 / 整流罩直径；
 * 2. **四轨道小表** + 当前 Mission 目标轨道点值；
 * 3. **冻结（OI-04）**：关闭自动更新 → 常驻「已冻结 / 可能过期」；冻结期间输入变更 →
 *    警示态「输入已变更」且**不再发请求**、保留最后一次结果；
 * 4. **导出报告**：POST /api/export（formats 契约）→ 既有作业通道（WS 优先）→ 产物
 *    blob 下载；导出中置灰；缺 provenance 置灰并说明（§11.12 规则 2）；
 * 5. **失败降级**：summary 422 → 提示行（含 code 与 suggestion），不白屏。
 */

/** CI 时序教训：waitFor 显式给足超时（300 ms 防抖 + 真实定时器下偶发慢一拍）。 */
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

/** 整箭数据响应（契约定死的手写形态；glow_kg / glow_t 双值由后端下发）。 */
function makeSummary(patch: Partial<VehicleSummaryResponse> = {}): VehicleSummaryResponse {
  return {
    glow_kg: 549000,
    glow_t: 549.0,
    propellant_total_kg: 487000,
    dry_mass_kg: 62000,
    total_length_m: 70,
    max_diameter_m: 3.7,
    fairing_diameter_m: 4.6,
    orbit: {
      target: 'LEO',
      payload_kg: 21951.1,
      payload_by_orbit: {
        LEO: { payload_kg: 21951.1 },
        SSO: { payload_kg: 16000 },
        GTO: { payload_kg: 5500 },
        GEO: { payload_kg: 1200 },
      },
    },
    warnings: [],
    provenance: { 'geometry.kernel': 'OCCT 7.8' },
    ...patch,
  }
}

const UNITS_T = {
  units: [{ quantity: 'mass', label: '质量', symbol: 't', si_symbol: 'kg', factor: 1000 }],
}

function jobRecord(input: {
  jobId: string
  status: 'running' | 'succeeded' | 'failed'
  resultKey?: string
}): JobRecord {
  return {
    job_id: input.jobId,
    status: input.status,
    stage: input.status === 'succeeded' ? 'done' : 'step',
    progress: input.status === 'succeeded' ? 1 : 0.4,
    result_key: input.status === 'succeeded' ? (input.resultKey ?? 'sha256-report-key') : null,
    metrics: null,
    error: null,
    created_at: '2026-09-21T00:00:00Z',
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
    /* 仅记录关闭意图 */
  }

  emit(record: unknown): void {
    this.onmessage?.({ data: JSON.stringify(record) } as MessageEvent)
  }
}

type FetchMock = Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>

function jsonResponse(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as unknown as Response
}

function errorResponse(status: number, body: unknown): Response {
  return { ok: false, status, json: async () => body } as unknown as Response
}

function blobResponse(): Response {
  return {
    ok: true,
    status: 200,
    json: async () => {
      throw new Error('blob 响应没有 JSON 体')
    },
    blob: async () => new Blob(['artifact-bytes']),
  } as unknown as Response
}

interface FetchRoutes {
  summary?: () => Response
  exportStart?: () => Response
}

/** fetch 替身：路由 units / summary / export / jobs / artifacts；未覆盖路径直接失败。 */
function makeFetchMock(routes: FetchRoutes = {}): FetchMock {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/params/units') return jsonResponse(UNITS_T)
    if (url === '/api/vehicle/summary') {
      return routes.summary ? routes.summary() : jsonResponse(makeSummary())
    }
    if (url === '/api/export') {
      return routes.exportStart
        ? routes.exportStart()
        : jsonResponse({
            job_id: 'export-job-1',
            formats: [...REPORT_EXPORT_FORMATS],
            files: Object.fromEntries(
              REPORT_EXPORT_FORMATS.map((fmt) => [fmt, `export.${fmt.replace('_', '.')}`]),
            ),
          })
    }
    if (url.startsWith('/api/jobs/')) return jsonResponse(jobRecord({ jobId: 'export-job-1', status: 'running' }))
    if (url.startsWith('/api/artifacts/')) return blobResponse()
    if (url === '/api/perf/evaluate') return jsonResponse({ point: {} })
    throw new Error(`测试未覆盖的请求：${url} ${init?.method ?? ''}`)
  })
}

/** 下载动作替身（jsdom 无 URL.createObjectURL）：记录 blob 与文件名。 */
function stubDownloads(): { createObjectURL: Mock; clicks: () => number } {
  const createObjectURL = vi.fn(() => 'blob:mock-url')
  const revokeObjectURL = vi.fn()
  Object.defineProperty(URL, 'createObjectURL', {
    value: createObjectURL,
    configurable: true,
    writable: true,
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    value: revokeObjectURL,
    configurable: true,
    writable: true,
  })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
  return { createObjectURL, clicks: () => click.mock.calls.length }
}

function summaryCalls(fetchMock: FetchMock): number {
  return fetchMock.mock.calls.filter(([input]) => String(input) === '/api/vehicle/summary').length
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
  vi.restoreAllMocks()
  delete (URL as { createObjectURL?: unknown }).createObjectURL
  delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL
  useVehicleStore.getState().reset()
  usePerfStore.getState().reset()
})

describe('FR-10 七字段 + FR-11 轨道运力渲染', () => {
  it('七字段渲染：总质量 kg+t 双值（glow_t 照抄后端）、推进剂 / 干重按单位表、三个尺寸字段', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)

    render(<VehicleSummaryPanel />)

    // 总质量双值：kg（SI 原值）· t（后端 glow_t，前端不换算）
    expect(
      await screen.findByText('549000.0 kg · 549.0 t', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
    // 推进剂 / 干重：按单位表 factor 显示（487000 kg → 487.0 t；62000 kg → 62.0 t）
    expect(screen.getByText('487.0 t')).toBeInTheDocument()
    expect(screen.getByText('62.0 t')).toBeInTheDocument()
    // 尺寸字段只格式化（m）
    expect(screen.getByText('70.00 m')).toBeInTheDocument()
    expect(screen.getByText('3.70 m')).toBeInTheDocument()
    expect(screen.getByText('4.60 m')).toBeInTheDocument()
  })

  it('四轨道小表 + 当前 Mission 目标轨道点值（数值照抄后端，按单位表显示）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)

    render(<VehicleSummaryPanel />)

    expect(await screen.findByText('487.0 t', {}, { timeout: WAIT_TIMEOUT })).toBeInTheDocument()
    // 目标轨道点值与 LEO 行是同一后端数值（target=LEO）→ "22.0 t" 恰好出现 2 处
    expect(screen.getAllByText('22.0 t')).toHaveLength(2)
    expect(screen.getAllByText(/16\.0 t/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('5.5 t')).toBeInTheDocument()
    expect(screen.getByText('1.2 t')).toBeInTheDocument()
    // LEO 出现 2 处（目标轨道行 + 表行头）；SSO/GTO/GEO 只在表行头
    expect(screen.getAllByText('LEO')).toHaveLength(2)
    expect(screen.getByText('SSO')).toBeInTheDocument()
    expect(screen.getByText('GTO')).toBeInTheDocument()
    expect(screen.getByText('GEO')).toBeInTheDocument()
  })

  it('无整流罩构型（fairing_diameter_m = null）显示占位符而非崩溃', async () => {
    const fetchMock = makeFetchMock({ summary: () => jsonResponse(makeSummary({ fairing_diameter_m: null })) })
    vi.stubGlobal('fetch', fetchMock)

    render(<VehicleSummaryPanel />)
    expect(await screen.findByText('487.0 t', {}, { timeout: WAIT_TIMEOUT })).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('防抖 300 ms：连续两次 vehicle 变更只发最后一次请求', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)

    render(<VehicleSummaryPanel />)
    // 防抖窗口内换一次输入（直接换 vehicle 引用，绕开诊断触发，聚焦本面板的防抖）
    await act(async () => {
      useVehicleStore.setState({
        vehicle: { ...makeVehicle(), payload_mass_kg: 2000 },
      })
    })

    expect(
      await screen.findByText('549000.0 kg · 549.0 t', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
    expect(summaryCalls(fetchMock)).toBe(1)
    // 请求体带的是最后一次的 vehicle
    const call = fetchMock.mock.calls.find(([input]) => String(input) === '/api/vehicle/summary')
    const body = JSON.parse(String(call?.[1]?.body)) as { vehicle: { payload_mass_kg: number } }
    expect(body.vehicle.payload_mass_kg).toBe(2000)
  })
})

describe('冻结语义（OI-04：复用 store/perf 的 autoUpdate，同一状态源）', () => {
  it('关闭自动更新 → 常驻「已冻结 / 可能过期」；输入变更 → 警示态且不再请求、结果保留', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<VehicleSummaryPanel />)
    expect(
      await screen.findByText('549000.0 kg · 549.0 t', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
    expect(summaryCalls(fetchMock)).toBe(1)
    expect(screen.queryByTestId('frozen-marker')).not.toBeInTheDocument()

    // 关闭自动更新：冻结标记常驻呈现
    await user.click(screen.getByRole('checkbox', { name: '自动更新' }))
    const marker = screen.getByTestId('frozen-marker')
    expect(marker).toHaveTextContent('已冻结 / 可能过期')
    expect(marker).not.toHaveClass('vehicle-summary__frozen--warn')

    // 冻结期间改输入：零新增请求（OI-04 裁决 1），标记升级为警示态（裁决 3）
    await act(async () => {
      useVehicleStore.setState({
        vehicle: { ...makeVehicle(), payload_mass_kg: 2000 },
      })
    })
    await new Promise((resolve) => {
      setTimeout(resolve, 400)
    })
    expect(summaryCalls(fetchMock)).toBe(1)
    const warned = screen.getByTestId('frozen-marker')
    expect(warned).toHaveTextContent('输入已变更')
    expect(warned).toHaveClass('vehicle-summary__frozen--warn')

    // 裁决 2：界面保留最后一次结果（不触发 2D 与面板重绘/清空）
    expect(screen.getByText('549000.0 kg · 549.0 t')).toBeInTheDocument()
  })
})

describe('导出报告（FR-11 / §11.12 规则 2、4）', () => {
  it('POST /api/export（formats 契约）→ 作业通道 → 产物 blob 下载；导出中置灰', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const { createObjectURL, clicks } = stubDownloads()
    const user = userEvent.setup()

    render(<VehicleSummaryPanel />)
    expect(
      await screen.findByText('549000.0 kg · 549.0 t', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()

    await user.click(screen.getByTestId('export-report-button'))

    // 投递契约：body 含 vehicle 与至少三种 formats
    await waitFor(
      () => {
        expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/export')).toBe(true)
      },
      { timeout: WAIT_TIMEOUT },
    )
    const exportCall = fetchMock.mock.calls.find(([input]) => String(input) === '/api/export')
    const body = JSON.parse(String(exportCall?.[1]?.body)) as {
      vehicle: { name: string }
      formats: string[]
    }
    expect(body.vehicle.name).toBe('示例箭')
    expect(body.formats).toEqual([...REPORT_EXPORT_FORMATS])

    // 作业通道：WS 建立（复用既有 /ws/jobs/{id}）
    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
        expect(FakeSocket.latest?.url).toContain('/ws/jobs/export-job-1')
      },
      { timeout: WAIT_TIMEOUT },
    )

    // 作业在途：导出中提示 + 按钮置灰
    await act(async () => {
      FakeSocket.latest?.emit(jobRecord({ jobId: 'export-job-1', status: 'running' }))
    })
    expect(await screen.findByText('导出中…')).toBeInTheDocument()
    expect(screen.getByTestId('export-report-button')).toBeDisabled()

    // 作业成功 → 按响应 files 映射逐一经 /api/artifacts/{key}/{file} 下载 blob
    // （命名单一事实源在后端，前端按映射取 file 段——`export.params.json` 等）
    await act(async () => {
      FakeSocket.latest?.emit(
        jobRecord({ jobId: 'export-job-1', status: 'succeeded', resultKey: 'sha256-report-key' }),
      )
    })
    await waitFor(
      () => {
        expect(clicks()).toBe(REPORT_EXPORT_FORMATS.length)
      },
      { timeout: WAIT_TIMEOUT },
    )
    for (const format of REPORT_EXPORT_FORMATS) {
      const mapped = `export.${format.replace('_', '.')}`
      expect(
        fetchMock.mock.calls.some(([input]) => String(input) === `/api/artifacts/sha256-report-key/${mapped}`),
      ).toBe(true)
    }
    expect(createObjectURL.mock.calls.length).toBe(REPORT_EXPORT_FORMATS.length)

    // 收尾：导出中提示消失、按钮恢复
    await waitFor(
      () => {
        expect(screen.queryByText('导出中…')).not.toBeInTheDocument()
      },
      { timeout: WAIT_TIMEOUT },
    )
    expect(screen.getByTestId('export-report-button')).toBeEnabled()
  })

  it('作业失败：导出中复位并显示 §10.3 错误（code / message / suggestion 可见）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    stubDownloads()
    const user = userEvent.setup()

    render(<VehicleSummaryPanel />)
    expect(
      await screen.findByText('549000.0 kg · 549.0 t', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()

    await user.click(screen.getByTestId('export-report-button'))
    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )
    await act(async () => {
      FakeSocket.latest?.emit({
        ...jobRecord({ jobId: 'export-job-1', status: 'failed' }),
        error: {
          code: 'EXPORT_FAILED',
          stage: 'step',
          message: '产物落盘失败',
          suggestion: '重试导出；若反复失败请查看后端日志',
        },
      })
    })

    expect(
      await screen.findByText(/导出失败（EXPORT_FAILED）：产物落盘失败——重试导出/, {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('export-report-button')).toBeEnabled()
  })

  it('缺 provenance（CON-04）→ 导出置灰并说明原因（§11.12 规则 2）', async () => {
    const fetchMock = makeFetchMock({
      summary: () => jsonResponse(makeSummary({ provenance: {} })),
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<VehicleSummaryPanel />)
    expect(
      await screen.findByText('487.0 t', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()

    const button = screen.getByTestId('export-report-button')
    expect(button).toBeDisabled()
    expect(screen.getByText('缺少 provenance（CON-04），暂不可导出')).toBeInTheDocument()
  })
})

describe('失败降级（§10.3 / 不白屏）', () => {
  it('summary 返回 422 错误体 → 提示行含 code / message / suggestion，面板其余结构仍在', async () => {
    const fetchMock = makeFetchMock({
      summary: () =>
        errorResponse(422, {
          error: {
            code: 'VALIDATION_ERROR',
            stage: 'api',
            message: '参数不合法',
            suggestion: '检查 stages 字段后重试',
          },
        }),
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<VehicleSummaryPanel />)

    expect(
      await screen.findByText(
        /整箭数据不可用（VALIDATION_ERROR）：参数不合法——检查 stages 字段后重试/,
        {},
        { timeout: WAIT_TIMEOUT },
      ),
    ).toBeInTheDocument()
    // 降级不白屏：面板结构与导出按钮（数据未就绪置灰）仍在
    expect(screen.getByText('轨道运力')).toBeInTheDocument()
    expect(screen.getByTestId('export-report-button')).toBeDisabled()
    expect(screen.getByText('数据未就绪，暂不可导出')).toBeInTheDocument()
  })

  it('summary 契约不符（缺字段）→ 同样走降级提示行', async () => {
    const fetchMock = makeFetchMock({ summary: () => jsonResponse({ glow_kg: 1 }) })
    vi.stubGlobal('fetch', fetchMock)

    render(<VehicleSummaryPanel />)
    expect(
      await screen.findByText(/整箭数据不可用（CONTRACT_MISMATCH）/, {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
  })
})
