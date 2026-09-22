import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { Vehicle } from '../api/params'
import { useVehicleStore } from '../store/vehicle'
import { ChatPanel } from './ChatPanel'

/**
 * AI 助手 ChatPanel 测试（规格 §10.2 / OI-10 / M7）。
 *
 * 被钉住的契约：
 *
 * 1. **未配置 → 入口隐藏**（OI-10 降级① 机检）：`status.configured === false`
 *    时 `chat-fab` 带 `chat-fab--hidden` 类（display:none），展开面板可见引导提示；
 * 2. **配置表单保存**：POST /api/assistant/config 请求体含 base_url / model / api_key
 *    （api_key=null=保留），成功后状态刷新为 configured；
 * 3. **降级提示渲染**：degraded 事件 → 提示块可见（code / message / suggestion）；
 * 4. **方案诊断**（零 LLM）：按钮触发 store.requestDiagnose，不经过 WS；
 * 5. **diff 确认制**：param_diff 渲染「确认 / 拒绝」按钮；点击后 stream.send
 *    发出 apply_diff / reject_diff；diff_applied 后调用 store.applyCandidate。
 */

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

/** AI 状态响应（AssistantStatusResponse）。 */
function makeStatus(patch: Partial<Record<string, unknown>> = {}): Record<string, unknown> {
  return {
    configured: true,
    base_url: 'https://api.example.com/v1',
    model: 'gpt-4o',
    api_key_masked: 'sk-…abcd',
    reachable: true,
    config_file: 'data/assistant.json',
    ...patch,
  }
}

/** WS 替身：把连接与事件投递握在测试手里，避免 jsdom 真去连 ws://。 */
class FakeSocket {
  static latest: FakeSocket | null = null
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  sent: unknown[] = []

  constructor(readonly url: string) {
    FakeSocket.latest = this
  }

  close(): void {
    /* 仅记录关闭意图 */
  }

  emit(record: unknown): void {
    this.onmessage?.({ data: JSON.stringify(record) } as MessageEvent)
  }

  fireOpen(): void {
    this.onopen?.(new Event('open'))
  }

  fireError(): void {
    this.onerror?.(new Event('error'))
  }

  get readyState(): number {
    return WebSocket.OPEN
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data))
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
  status?: () => Response
  config?: () => Response
}

function makeFetchMock(routes: FetchRoutes = {}): FetchMock {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url === '/api/assistant/status') {
      return routes.status ? routes.status() : jsonResponse(makeStatus())
    }
    if (url === '/api/assistant/config') {
      return routes.config ? routes.config() : jsonResponse(makeStatus({ configured: true }))
    }
    throw new Error(`测试未覆盖的请求：${url} ${init?.method ?? ''}`)
  })
}

beforeEach(() => {
  useVehicleStore.getState().reset()
  useVehicleStore.setState({ vehicle: makeVehicle() })
  FakeSocket.latest = null
  // jsdom 没有 scrollIntoView（ChatPanel 自动滚动用）；桩掉即可
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  FakeSocket.latest = null
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  useVehicleStore.getState().reset()
})

describe('OI-10 降级①：未配置 → 入口隐藏', () => {
  it('configured=false 时 chat-fab 带 chat-fab--hidden 类（display:none）', async () => {
    const fetchMock = makeFetchMock({
      status: () => jsonResponse(makeStatus({ configured: false, base_url: null, model: null, api_key_masked: null, reachable: null })),
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        const fab = document.querySelector('.chat-fab')
        expect(fab).not.toBeNull()
        expect(fab).toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )
  })

  it('configured=true 时 chat-fab 可见且无 hidden 类', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        const fab = document.querySelector('.chat-fab')
        expect(fab).not.toBeNull()
        expect(fab).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )
  })
})

describe('配置表单保存（OI-10：base_url / api_key / model，key 只写不回传）', () => {
  it('填写表单 → POST /api/assistant/config 请求体含三项；成功后状态刷新为 configured', async () => {
    const fetchMock = makeFetchMock({
      config: () => jsonResponse(makeStatus({ configured: true, api_key_masked: 'sk-…wxyz' })),
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    // 等初始状态加载完
    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    // 打开面板与设置表单
    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))
    await user.click(screen.getByRole('button', { name: 'AI 设置' }))

    // 填写
    const baseUrlInput = screen.getByPlaceholderText('https://api.example.com/v1')
    const modelInput = screen.getByPlaceholderText('gpt-4o / deepseek-chat')
    const apiKeyInput = screen.getByPlaceholderText('sk-…abcd')

    await user.clear(baseUrlInput)
    await user.type(baseUrlInput, 'https://api.deepseek.com/v1')
    await user.clear(modelInput)
    await user.type(modelInput, 'deepseek-chat')
    await user.type(apiKeyInput, 'sk-new-key')

    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(
      () => {
        const configCall = fetchMock.mock.calls.find(
          ([input]) => String(input) === '/api/assistant/config',
        )
        expect(configCall).toBeDefined()
        const body = JSON.parse(String(configCall?.[1]?.body)) as {
          base_url: string
          model: string
          api_key: string | null
        }
        expect(body.base_url).toBe('https://api.deepseek.com/v1')
        expect(body.model).toBe('deepseek-chat')
        expect(body.api_key).toBe('sk-new-key')
      },
      { timeout: WAIT_TIMEOUT },
    )
  })

  it('api_key 留空 → 请求体 api_key=null（保留现有值语义）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))
    await user.click(screen.getByRole('button', { name: 'AI 设置' }))

    // 不填 api_key，直接保存
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(
      () => {
        const configCall = fetchMock.mock.calls.find(
          ([input]) => String(input) === '/api/assistant/config',
        )
        expect(configCall).toBeDefined()
        const body = JSON.parse(String(configCall?.[1]?.body)) as { api_key: string | null }
        expect(body.api_key).toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )
  })

  it('保存失败 → 配置错误提示可见', async () => {
    const fetchMock = makeFetchMock({
      config: () =>
        errorResponse(422, {
          error: {
            code: 'VALIDATION_ERROR',
            stage: 'api',
            message: 'base_url 格式不合法',
            suggestion: '检查 URL 是否以 https:// 开头',
          },
        }),
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))
    await user.click(screen.getByRole('button', { name: 'AI 设置' }))
    await user.click(screen.getByRole('button', { name: '保存' }))

    expect(
      await screen.findByText(/检查 URL 是否以 https:\/\/ 开头/, {}, { timeout: WAIT_TIMEOUT }),
    ).toBeInTheDocument()
  })
})

describe('方案诊断（零 LLM，复用既有诊断通路）', () => {
  it('点击「方案诊断」按钮触发 store.requestDiagnose（不经过 WS）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    // 用 spy 监控 requestDiagnose（不破坏 scheduler）
    const store = useVehicleStore.getState()
    const diagnoseSpy = vi.spyOn(store, 'requestDiagnose')

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))
    await user.click(screen.getByRole('button', { name: '方案诊断' }))

    expect(diagnoseSpy).toHaveBeenCalledTimes(1)
    expect(
      screen.getByText(/已触发本地方案诊断（零 LLM）/),
    ).toBeInTheDocument()
  })

  it('vehicle=null 时方案诊断按钮置灰', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    useVehicleStore.setState({ vehicle: null })

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    // 展开面板后方案诊断按钮不可点击
    await act(async () => {
      const openButton = screen.getByRole('button', { name: '展开 AI 助手' })
      openButton.click()
    })

    const diagnoseButton = screen.getByRole('button', { name: '方案诊断' })
    expect(diagnoseButton).toBeDisabled()
  })
})

describe('降级提示渲染（§10.2 三降级收敛为事件）', () => {
  it('degraded 事件 → 提示块可见（code / message / suggestion）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))

    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )

    await act(async () => {
      FakeSocket.latest?.fireOpen()
      FakeSocket.latest?.emit({
        type: 'degraded',
        code: 'ASSISTANT_UPSTREAM_UNREACHABLE',
        message: 'AI 上游不可达',
        suggestion: '检查网络或 base_url 是否正确',
      })
    })

    expect(screen.getByText('AI 上游不可达')).toBeInTheDocument()
    expect(screen.getByText('检查网络或 base_url 是否正确')).toBeInTheDocument()
  })

  it('error 事件 → 提示块用 error 样式（chat-notice--error）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))

    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )

    await act(async () => {
      FakeSocket.latest?.fireOpen()
      FakeSocket.latest?.emit({
        type: 'error',
        code: 'ASSISTANT_UPSTREAM_ERROR',
        message: '上游返回 500',
        suggestion: '稍后重试',
      })
    })

    const notice = document.querySelector('.chat-notice--error')
    expect(notice).not.toBeNull()
    expect(screen.getByText('上游返回 500')).toBeInTheDocument()
  })
})

describe('diff 确认制（OI-10：确认前不应用，apply_diff 是唯一生效口）', () => {
  it('param_diff 事件 → 渲染「确认应用 / 拒绝」按钮', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))

    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )

    await act(async () => {
      FakeSocket.latest?.fireOpen()
      FakeSocket.latest?.emit({
        type: 'param_diff',
        diff_id: 'diff-1',
        changes: [
          { path: 'payload_mass_kg', old: 1000, new: 2000 },
        ],
        vehicle: { ...makeVehicle(), payload_mass_kg: 2000 },
      })
    })

    expect(screen.getByText('参数修改建议（确认后应用）')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认应用' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '拒绝' })).toBeInTheDocument()
    // 字段路径可见
    expect(screen.getByText('payload_mass_kg')).toBeInTheDocument()
  })

  it('点击「确认应用」→ stream.send 发出 apply_diff', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))

    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )

    await act(async () => {
      FakeSocket.latest?.fireOpen()
      FakeSocket.latest?.emit({
        type: 'param_diff',
        diff_id: 'diff-2',
        changes: [{ path: 'payload_mass_kg', old: 1000, new: 2000 }],
        vehicle: { ...makeVehicle(), payload_mass_kg: 2000 },
      })
    })

    await user.click(screen.getByRole('button', { name: '确认应用' }))

    expect(FakeSocket.latest?.sent).toContainEqual({
      type: 'apply_diff',
      diff_id: 'diff-2',
    })
  })

  it('点击「拒绝」→ stream.send 发出 reject_diff', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))

    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )

    await act(async () => {
      FakeSocket.latest?.fireOpen()
      FakeSocket.latest?.emit({
        type: 'param_diff',
        diff_id: 'diff-3',
        changes: [{ path: 'payload_mass_kg', old: 1000, new: 2000 }],
        vehicle: { ...makeVehicle(), payload_mass_kg: 2000 },
      })
    })

    await user.click(screen.getByRole('button', { name: '拒绝' }))

    expect(FakeSocket.latest?.sent).toContainEqual({
      type: 'reject_diff',
      diff_id: 'diff-3',
    })
  })

  it('diff_applied 事件 → 调用 store.applyCandidate 写入候选 Vehicle', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    const store = useVehicleStore.getState()
    const applySpy = vi.spyOn(store, 'applyCandidate')

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))

    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )

    const candidateVehicle = { ...makeVehicle(), payload_mass_kg: 2000 }

    await act(async () => {
      FakeSocket.latest?.fireOpen()
      FakeSocket.latest?.emit({
        type: 'param_diff',
        diff_id: 'diff-4',
        changes: [{ path: 'payload_mass_kg', old: 1000, new: 2000 }],
        vehicle: candidateVehicle,
      })
    })

    await user.click(screen.getByRole('button', { name: '确认应用' }))

    // 后端确认后下发 diff_applied
    await act(async () => {
      FakeSocket.latest?.emit({
        type: 'diff_applied',
        diff_id: 'diff-4',
        vehicle: candidateVehicle,
      })
    })

    await waitFor(
      () => {
        expect(applySpy).toHaveBeenCalledWith(candidateVehicle, ['payload_mass_kg'])
      },
      { timeout: WAIT_TIMEOUT },
    )
  })

  it('diff_rejected 事件 → diff 块标记为已拒绝（按钮撤掉）', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))

    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )

    await act(async () => {
      FakeSocket.latest?.fireOpen()
      FakeSocket.latest?.emit({
        type: 'param_diff',
        diff_id: 'diff-5',
        changes: [{ path: 'payload_mass_kg', old: 1000, new: 2000 }],
        vehicle: { ...makeVehicle(), payload_mass_kg: 2000 },
      })
    })

    expect(screen.getByRole('button', { name: '确认应用' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '拒绝' }))

    await act(async () => {
      FakeSocket.latest?.emit({
        type: 'diff_rejected',
        diff_id: 'diff-5',
      })
    })

    expect(screen.getByText('已拒绝')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认应用' })).not.toBeInTheDocument()
  })
})

describe('对话流（assistant_text 增量 + 最终整段）', () => {
  it('assistant_delta 增量拼装 → assistant_text 整段覆盖', async () => {
    const fetchMock = makeFetchMock()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('WebSocket', FakeSocket)
    const user = userEvent.setup()

    render(<ChatPanel />)
    // 状态加载是 mount 后的异步落账：在 act 内冲刷微任务，避免 act 告警
    await act(async () => {
      await Promise.resolve()
    })

    await waitFor(
      () => {
        expect(document.querySelector('.chat-fab')).not.toHaveClass('chat-fab--hidden')
      },
      { timeout: WAIT_TIMEOUT },
    )

    await user.click(screen.getByRole('button', { name: '展开 AI 助手' }))

    await waitFor(
      () => {
        expect(FakeSocket.latest).not.toBeNull()
      },
      { timeout: WAIT_TIMEOUT },
    )

    await act(async () => {
      FakeSocket.latest?.fireOpen()
      FakeSocket.latest?.emit({ type: 'assistant_delta', text: '建议' })
      FakeSocket.latest?.emit({ type: 'assistant_delta', text: '增大' })
      FakeSocket.latest?.emit({ type: 'assistant_delta', text: '载荷' })
    })

    expect(screen.getByText('建议增大载荷')).toBeInTheDocument()

    await act(async () => {
      FakeSocket.latest?.emit({ type: 'assistant_text', text: '建议增大载荷到 2000kg' })
    })

    expect(screen.getByText('建议增大载荷到 2000kg')).toBeInTheDocument()
  })
})
