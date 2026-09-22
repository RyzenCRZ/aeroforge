import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { ErrorBody } from '../api/client'
import type { JobRecord } from '../api/geometry'
import type { Vehicle } from '../api/params'
import type { Nsga2Result, OptimizeSolutionRow } from '../api/optimize'
import { useVehicleStore } from '../store/vehicle'
import { OptimizePanel } from './OptimizePanel'

/**
 * 优化与权衡面板（规格 §14 / M6 缺项补做的前端片）。
 *
 * 被钉住的契约：
 *
 * 1. **四标签渲染** + 基线构型名 + 变量清单从基线 Vehicle 推导（默认勾选第一项）；
 * 2. **作业提交请求体形状**：vehicle + 仅勾选变量 + 种群/代数/seed 缺省 + 目标键；
 * 3. **Pareto 前沿表**：按 GLOW 升序（排版约定），行内 provenance 可展开（§14 审计）；
 * 4. **一次一个作业**：进行中四个触发按钮置灰，完成恢复；
 * 5. **422 超限**（方案 > 20 / 组合 > 10⁴）与**作业失败**：code / message / suggestion
 *    可见（§10.3）；
 * 6. **未配置变量**：开始按钮禁用 + 提示；
 * 7. **载入此构型**：走既有参数载入通路（store/vehicle 的 setField），覆盖当前参数。
 */

/** CI 时序教训：waitFor 显式给足超时（真实定时器下偶发慢一拍）。 */
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

/** 一个候选方案行（参数摘要 + GLOW + 运力 + 行级 provenance）。 */
function makeRow(patch: Partial<OptimizeSolutionRow> = {}): OptimizeSolutionRow {
  return {
    label: '方案A',
    params: { 'stages[0].engine_count': 6 },
    glow_kg: 500_000,
    payload_kg: 20_000,
    provenance: { 'perf.engine': 'CEA', 'params.isp_source': 'default' },
    ...patch,
  }
}

/** NSGA-II 结果（故意乱序：验证面板按 GLOW 升序的排版约定）。 */
function makeNsga2Result(): Nsga2Result {
  return {
    pareto_front: [
      makeRow({ glow_kg: 520_000, payload_kg: 21_000 }),
      makeRow({
        label: '方案B',
        params: { 'stages[0].engine_count': 4 },
        glow_kg: 480_000,
        payload_kg: 18_000,
      }),
    ],
    warnings: [],
  }
}

/** 作业快照（metrics 即优化结果载荷，与 MC 结果同族形态）。 */
function jobRecord(input: {
  jobId: string
  status: 'running' | 'succeeded' | 'failed'
  metrics?: Record<string, unknown> | null
  error?: ErrorBody | null
}): JobRecord {
  return {
    job_id: input.jobId,
    status: input.status,
    stage: input.status === 'succeeded' ? 'done' : 'step',
    progress: input.status === 'succeeded' ? 1 : 0.4,
    result_key: null,
    metrics: input.metrics ?? null,
    error: input.error ?? null,
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

interface FetchRoutes {
  nsga2Start?: () => Response
  tradeStart?: () => Response
  sweepStart?: () => Response
  inverseStart?: () => Response
}

/** fetch 替身：路由 units / diagnose / 四个优化端点 / jobs；未覆盖路径直接失败。 */
function makeFetchMock(routes: FetchRoutes = {}): FetchMock {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/params/units') return jsonResponse({ units: [] })
    if (url === '/api/params/diagnose') {
      return jsonResponse({ constraints: [], diagnostics: [], rules: [] })
    }
    if (url === '/api/optimize/nsga2') {
      return routes.nsga2Start ? routes.nsga2Start() : jsonResponse({ job_id: 'opt-nsga2-1' })
    }
    if (url === '/api/optimize/trade-study') {
      return routes.tradeStart ? routes.tradeStart() : jsonResponse({ job_id: 'opt-trade-1' })
    }
    if (url === '/api/optimize/sweep') {
      return routes.sweepStart ? routes.sweepStart() : jsonResponse({ job_id: 'opt-sweep-1' })
    }
    if (url === '/api/optimize/inverse') {
      return routes.inverseStart ? routes.inverseStart() : jsonResponse({ job_id: 'opt-inverse-1' })
    }
    if (url.startsWith('/api/jobs/')) {
      const jobId = url.split('/').pop() ?? 'job'
      return jsonResponse(jobRecord({ jobId, status: 'running' }))
    }
    throw new Error(`测试未覆盖的请求：${url} ${init?.method ?? ''}`)
  })
}

beforeEach(() => {
  useVehicleStore.getState().reset()
  useVehicleStore.setState({ vehicle: makeVehicle() })
  FakeSocket.latest = null
})

afterEach(() => {
  FakeSocket.latest = null
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  useVehicleStore.getState().reset()
})

describe('渲染骨架与基线构型', () => {
  it('四标签渲染 + 基线构型名 + 变量清单来自基线 Vehicle（默认勾选第一项并显示当前基值）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)

    render(<OptimizePanel />)

    // 四标签 + 基线构型名
    expect(screen.getByTestId('optimize-tab-nsga2')).toHaveTextContent('多目标优化')
    expect(screen.getByTestId('optimize-tab-trade')).toHaveTextContent('权衡研究')
    expect(screen.getByTestId('optimize-tab-sweep')).toHaveTextContent('批量扫描')
    expect(screen.getByTestId('optimize-tab-inverse')).toHaveTextContent('逆向设计')
    expect(screen.getByTestId('optimize-baseline-name')).toHaveTextContent('示例箭')

    // 变量清单从基线 Vehicle 推导（单级：长度缩放 + 发动机数；基值照抄当前参数）
    const lengthCheck = await screen.findByRole(
      'checkbox',
      { name: '第1级 长度缩放（当前 20）' },
      { timeout: WAIT_TIMEOUT },
    )
    expect(lengthCheck).toBeChecked()
    expect(screen.getByRole('checkbox', { name: '第1级 发动机数（当前 4）' })).not.toBeChecked()

    // 默认勾选后「开始优化」可用（未配置变量的禁用态见下一用例）
    await waitFor(
      () => {
        expect(screen.getByTestId('nsga2-start-button')).toBeEnabled()
      },
      { timeout: WAIT_TIMEOUT },
    )
  })

  it('未勾选任何变量 → 开始按钮禁用 + 提示可见', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<OptimizePanel />)

    const startButton = screen.getByTestId('nsga2-start-button')
    await waitFor(() => expect(startButton).toBeEnabled(), { timeout: WAIT_TIMEOUT })

    await user.click(screen.getByRole('checkbox', { name: '第1级 长度缩放（当前 20）' }))
    expect(startButton).toBeDisabled()
    expect(screen.getByTestId('nsga2-disabled-hint')).toHaveTextContent(
      '未选择任何优化变量——至少勾选一个变量才能开始优化',
    )
  })
})

describe('多目标优化（NSGA-II）', () => {
  it('作业提交请求体形状（vehicle + 仅勾选变量 + 缺省种群/代数/seed + 目标键）→ 进行中置灰 + 进度 → 完成恢复', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await waitFor(
      () => expect(screen.getByTestId('nsga2-start-button')).toBeEnabled(),
      { timeout: WAIT_TIMEOUT },
    )
    await user.click(screen.getByTestId('nsga2-start-button'))

    // 投递契约：vehicle + 仅勾选变量（默认勾选第一项 = 长度缩放）+ 输入缺省值 + 两个目标
    await waitFor(
      () => {
        expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/optimize/nsga2')).toBe(true)
      },
      { timeout: WAIT_TIMEOUT },
    )
    const call = fetchMock.mock.calls.find(([input]) => String(input) === '/api/optimize/nsga2')
    const body = JSON.parse(String(call?.[1]?.body)) as {
      vehicle: { name: string }
      variables: { path: string; kind: string; base: number }[]
      population_size: number
      generations: number
      seed: number
      objectives: string[]
    }
    expect(body.vehicle.name).toBe('示例箭')
    expect(body.variables).toEqual([
      { path: 'stages[0].length_m', kind: 'continuous_scale', base: 20 },
    ])
    expect(body.population_size).toBe(40)
    expect(body.generations).toBe(30)
    expect(body.seed).toBe(42)
    expect(body.objectives).toEqual(['min_glow', 'max_payload_leo'])

    // 作业通道：WS 建立（复用既有 /ws/jobs/{id}）
    await waitFor(() => expect(FakeSocket.latest).not.toBeNull(), { timeout: WAIT_TIMEOUT })

    // 作业在途：进度条可见（progress=0.4 → 40%），四个触发按钮全部置灰
    await act(async () => {
      FakeSocket.latest?.emit(jobRecord({ jobId: 'opt-nsga2-1', status: 'running' }))
    })
    const progress = await screen.findByTestId('optimize-job-progress', {}, { timeout: WAIT_TIMEOUT })
    expect(progress).toHaveTextContent('40%')
    expect(screen.getByTestId('nsga2-start-button')).toBeDisabled()
    expect(screen.getByTestId('trade-start-button')).toBeDisabled()
    expect(screen.getByTestId('sweep-start-button')).toBeDisabled()
    expect(screen.getByTestId('inverse-start-button')).toBeDisabled()

    // 作业成功：Pareto 表渲染，触发按钮恢复可用
    await act(async () => {
      FakeSocket.latest?.emit({
        ...jobRecord({ jobId: 'opt-nsga2-1', status: 'succeeded' }),
        metrics: makeNsga2Result() as unknown as Record<string, unknown>,
      })
    })
    expect(
      await screen.findByTestId('nsga2-pareto-table', {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('nsga2-start-button')).toBeEnabled()
    expect(screen.queryByTestId('optimize-job-progress')).not.toBeInTheDocument()
  })

  it('Pareto 表按 GLOW 升序渲染 + 行内 provenance 可展开（§14 审计纪律）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await waitFor(
      () => expect(screen.getByTestId('nsga2-start-button')).toBeEnabled(),
      { timeout: WAIT_TIMEOUT },
    )
    await user.click(screen.getByTestId('nsga2-start-button'))
    await waitFor(() => expect(FakeSocket.latest).not.toBeNull(), { timeout: WAIT_TIMEOUT })
    await act(async () => {
      FakeSocket.latest?.emit({
        ...jobRecord({ jobId: 'opt-nsga2-1', status: 'succeeded' }),
        metrics: makeNsga2Result() as unknown as Record<string, unknown>,
      })
    })

    const table = await screen.findByTestId('nsga2-pareto-table', {}, { timeout: WAIT_TIMEOUT })
    const rows = within(table).getAllByRole('row')
    // 数据行按 GLOW 升序：480000（方案B）在 520000（方案A）之前（表头占第 0 行）
    expect(rows[1]).toHaveTextContent('480000.0 kg')
    expect(rows[1]).toHaveTextContent('第1级 发动机数 = 4')
    expect(rows[2]).toHaveTextContent('520000.0 kg')
    expect(rows[2]).toHaveTextContent('第1级 发动机数 = 6')

    // provenance 展开：点击「来源」→ 键值对可见；再点收起
    await user.click(within(rows[1]).getByTestId('prov-toggle-0'))
    const detail = screen.getByTestId('prov-detail-0')
    expect(detail).toHaveTextContent('perf.engine')
    expect(detail).toHaveTextContent('CEA')
    await user.click(within(rows[1]).getByTestId('prov-toggle-0'))
    expect(screen.queryByTestId('prov-detail-0')).not.toBeInTheDocument()
  })

  it('作业失败：§10.3 降级 code / message / suggestion 可见，触发按钮恢复', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await waitFor(
      () => expect(screen.getByTestId('nsga2-start-button')).toBeEnabled(),
      { timeout: WAIT_TIMEOUT },
    )
    await user.click(screen.getByTestId('nsga2-start-button'))
    await waitFor(() => expect(FakeSocket.latest).not.toBeNull(), { timeout: WAIT_TIMEOUT })

    await act(async () => {
      FakeSocket.latest?.emit({
        ...jobRecord({ jobId: 'opt-nsga2-1', status: 'failed' }),
        error: {
          code: 'OPTIMIZE_FAILED',
          stage: 'sampling',
          message: 'NSGA-II 进程池耗尽',
          suggestion: '降低种群规模后重试；若反复失败请查看后端日志',
        } satisfies ErrorBody,
      })
    })

    expect(
      await screen.findByTestId('optimize-job-error', {}, { timeout: WAIT_TIMEOUT }),
    ).toHaveTextContent(
      '优化作业失败（OPTIMIZE_FAILED）：NSGA-II 进程池耗尽——降低种群规模后重试；若反复失败请查看后端日志',
    )
    expect(screen.getByTestId('nsga2-start-button')).toBeEnabled()
  })

  it('载入此构型：走既有参数载入通路（store.setField 覆盖当前参数）并触发诊断', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await waitFor(
      () => expect(screen.getByTestId('nsga2-start-button')).toBeEnabled(),
      { timeout: WAIT_TIMEOUT },
    )
    await user.click(screen.getByTestId('nsga2-start-button'))
    await waitFor(() => expect(FakeSocket.latest).not.toBeNull(), { timeout: WAIT_TIMEOUT })
    await act(async () => {
      FakeSocket.latest?.emit({
        ...jobRecord({ jobId: 'opt-nsga2-1', status: 'succeeded' }),
        metrics: makeNsga2Result() as unknown as Record<string, unknown>,
      })
    })

    const table = await screen.findByTestId('nsga2-pareto-table', {}, { timeout: WAIT_TIMEOUT })
    // 第一数据行 = 方案B（engine_count=4）
    await user.click(within(table).getByTestId('load-config-0'))

    await waitFor(
      () => {
        expect(useVehicleStore.getState().vehicle?.stages[0]?.engine_count).toBe(4)
      },
      { timeout: WAIT_TIMEOUT },
    )
    expect(screen.getByTestId('optimize-load-note')).toHaveTextContent('覆盖当前参数')

    // 既有参数载入通路会自动触发诊断（防抖 200 ms）
    await act(async () => {
      await new Promise((resolve) => {
        setTimeout(resolve, 300)
      })
    })
    expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/params/diagnose')).toBe(true)
  })
})

describe('权衡研究', () => {
  it('提交请求体 { vehicle, variant_count } → 对比表渲染（方案名列）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await user.click(screen.getByTestId('optimize-tab-trade'))
    await user.click(screen.getByTestId('trade-start-button'))

    await waitFor(
      () => {
        expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/optimize/trade-study')).toBe(true)
      },
      { timeout: WAIT_TIMEOUT },
    )
    const call = fetchMock.mock.calls.find(([input]) => String(input) === '/api/optimize/trade-study')
    const body = JSON.parse(String(call?.[1]?.body)) as {
      vehicle: { name: string }
      variant_count: number
    }
    expect(body.vehicle.name).toBe('示例箭')
    expect(body.variant_count).toBe(6)

    await waitFor(() => expect(FakeSocket.latest).not.toBeNull(), { timeout: WAIT_TIMEOUT })
    await act(async () => {
      FakeSocket.latest?.emit({
        ...jobRecord({ jobId: 'opt-trade-1', status: 'succeeded' }),
        metrics: {
          variants: [
            makeRow({ label: '变体1', glow_kg: 510_000, payload_kg: 19_000 }),
            makeRow({ label: '变体2', glow_kg: 495_000, payload_kg: 20_500 }),
          ],
          warnings: [],
        } as unknown as Record<string, unknown>,
      })
    })

    const table = await screen.findByTestId('trade-table', {}, { timeout: WAIT_TIMEOUT })
    expect(within(table).getAllByRole('row')).toHaveLength(3)
    expect(within(table).getByText('变体1')).toBeInTheDocument()
    expect(within(table).getByText('495000.0 kg')).toBeInTheDocument()
  })

  it('方案数超限（422）：错误体 code / message / suggestion 可见', async () => {
    const fetchMock = makeFetchMock({
      tradeStart: () =>
        errorResponse(422, {
          error: {
            code: 'VARIANT_LIMIT_EXCEEDED',
            stage: 'api',
            message: '方案数超过上限 20（规格 §14）',
            suggestion: '把方案数降到 20 以内后重试',
          } satisfies ErrorBody,
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await user.click(screen.getByTestId('optimize-tab-trade'))
    await user.clear(screen.getByTestId('trade-variant-count'))
    await user.type(screen.getByTestId('trade-variant-count'), '25')
    await user.click(screen.getByTestId('trade-start-button'))

    expect(
      await screen.findByTestId('optimize-job-error', {}, { timeout: WAIT_TIMEOUT }),
    ).toHaveTextContent(
      '优化作业失败（VARIANT_LIMIT_EXCEEDED）：方案数超过上限 20（规格 §14）——把方案数降到 20 以内后重试',
    )
  })
})

describe('批量扫描', () => {
  it('组合超限（422，> 10⁴）：错误体 code / message / suggestion 可见', async () => {
    const fetchMock = makeFetchMock({
      sweepStart: () =>
        errorResponse(422, {
          error: {
            code: 'SWEEP_LIMIT_EXCEEDED',
            stage: 'api',
            message: '扫描组合数超过上限 10⁴（规格 §14）',
            suggestion: '减少轴数或步数后重试',
          } satisfies ErrorBody,
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await user.click(screen.getByTestId('optimize-tab-sweep'))
    await user.selectOptions(screen.getByTestId('sweep-axis-path-0'), 'stages[0].engine_count')
    await user.type(screen.getByTestId('sweep-axis-min-0'), '1')
    await user.type(screen.getByTestId('sweep-axis-max-0'), '8')
    await user.type(screen.getByTestId('sweep-axis-steps-0'), '5')
    await user.click(screen.getByTestId('sweep-start-button'))

    expect(
      await screen.findByTestId('optimize-job-error', {}, { timeout: WAIT_TIMEOUT }),
    ).toHaveTextContent(
      '优化作业失败（SWEEP_LIMIT_EXCEEDED）：扫描组合数超过上限 10⁴（规格 §14）——减少轴数或步数后重试',
    )
  })

  it('扫描成功：请求体含轴定义 → 聚合表渲染', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await user.click(screen.getByTestId('optimize-tab-sweep'))
    await user.selectOptions(screen.getByTestId('sweep-axis-path-0'), 'stages[0].engine_count')
    await user.type(screen.getByTestId('sweep-axis-min-0'), '2')
    await user.type(screen.getByTestId('sweep-axis-max-0'), '6')
    await user.type(screen.getByTestId('sweep-axis-steps-0'), '3')
    await user.click(screen.getByTestId('sweep-start-button'))

    await waitFor(
      () => {
        expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/optimize/sweep')).toBe(true)
      },
      { timeout: WAIT_TIMEOUT },
    )
    const call = fetchMock.mock.calls.find(([input]) => String(input) === '/api/optimize/sweep')
    const body = JSON.parse(String(call?.[1]?.body)) as {
      vehicle: { name: string }
      axes: { path: string; min: number; max: number; steps: number }[]
    }
    expect(body.vehicle.name).toBe('示例箭')
    expect(body.axes).toEqual([{ path: 'stages[0].engine_count', min: 2, max: 6, steps: 3 }])

    await waitFor(() => expect(FakeSocket.latest).not.toBeNull(), { timeout: WAIT_TIMEOUT })
    await act(async () => {
      FakeSocket.latest?.emit({
        ...jobRecord({ jobId: 'opt-sweep-1', status: 'succeeded' }),
        metrics: {
          rows: [
            makeRow({
              label: undefined,
              params: { 'stages[0].engine_count': 5 },
              glow_kg: 490_000,
              payload_kg: 21_500,
            }),
          ],
          warnings: [],
        } as unknown as Record<string, unknown>,
      })
    })

    const table = await screen.findByTestId('sweep-table', {}, { timeout: WAIT_TIMEOUT })
    expect(within(table).getByText('方案 1')).toBeInTheDocument()
    expect(within(table).getByText('第1级 发动机数 = 5')).toBeInTheDocument()
    expect(within(table).getByText('21500.0 kg')).toBeInTheDocument()
  })
})

describe('逆向设计', () => {
  it('提交请求体 { vehicle, target_orbit, target_payload_kg } → 最小构型结果表渲染', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<OptimizePanel />)
    await user.click(screen.getByTestId('optimize-tab-inverse'))

    // 未填目标运力：禁用 + 提示
    expect(screen.getByTestId('inverse-start-button')).toBeDisabled()
    expect(screen.getByTestId('inverse-disabled-hint')).toHaveTextContent('请填写目标运力')

    await user.type(screen.getByTestId('inverse-payload'), '5000')
    await user.click(screen.getByTestId('inverse-start-button'))

    await waitFor(
      () => {
        expect(fetchMock.mock.calls.some(([input]) => String(input) === '/api/optimize/inverse')).toBe(true)
      },
      { timeout: WAIT_TIMEOUT },
    )
    const call = fetchMock.mock.calls.find(([input]) => String(input) === '/api/optimize/inverse')
    const body = JSON.parse(String(call?.[1]?.body)) as {
      vehicle: { name: string }
      target_orbit: string
      target_payload_kg: number
    }
    expect(body.vehicle.name).toBe('示例箭')
    expect(body.target_orbit).toBe('LEO')
    expect(body.target_payload_kg).toBe(5000)

    await waitFor(() => expect(FakeSocket.latest).not.toBeNull(), { timeout: WAIT_TIMEOUT })
    await act(async () => {
      FakeSocket.latest?.emit({
        ...jobRecord({ jobId: 'opt-inverse-1', status: 'succeeded' }),
        metrics: {
          config: makeRow({
            label: '最小构型',
            params: { 'stages[0].engine_count': 3, 'stages[0].length_m': 18 },
            glow_kg: 450_000,
            payload_kg: 5_000,
          }),
          warnings: [],
        } as unknown as Record<string, unknown>,
      })
    })

    const table = await screen.findByTestId('inverse-table', {}, { timeout: WAIT_TIMEOUT })
    expect(within(table).getByText('最小构型')).toBeInTheDocument()
    expect(within(table).getByText('450000.0 kg')).toBeInTheDocument()
    expect(within(table).getByText('第1级 发动机数 = 3；第1级 长度缩放 = 18')).toBeInTheDocument()
  })
})
