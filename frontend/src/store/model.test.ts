import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ValidationReport } from '../api/geometry'
import { VALIDATE_DEBOUNCE_MS, useModelStore } from './model'
import { DEFAULT_PROFILE, useParamsStore } from './params'

/**
 * 校验防抖与双通道切换（§11.4 防抖 / §13.6 防抖有效性 / ADR-012 R-25）。
 *
 * 防抖断言按规格写死边界：199 ms 时**尚未**发请求、200 ms 时**只发一次**——
 * 这是"改值不再造成请求风暴"的唯一可证伪表述（NFR-01）。
 */

const REPORT: ValidationReport = {
  ok: true,
  checks: [{ check: '半径非负', severity: 'pass', detail: '全部半径 ≥ 0' }],
  joints: [{ index: 0, z: 1, radius: 1, angle_deg: 0, kind: 'g1', severity: 'pass' }],
  sample: [
    [0, 0],
    [1, 1],
    [1, 4],
    [0, 5],
  ],
  outline: [
    [0, 0],
    [1, 1],
    [1, 4],
    [0, 5],
  ],
  envelope: [2, 2, 5],
  volume: 5.2,
  surface_area: 20.1,
  centroid_z: 2.5,
  max_radius: 1,
  total_length: 5,
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response
}

/** 把已排队的微任务跑干（假定时器不会替我们 flush promise 链）。 */
async function flushMicrotasks(): Promise<void> {
  for (let round = 0; round < 10; round += 1) await Promise.resolve()
}

/** WS 替身：把连接与事件投递握在测试手里，避免 jsdom 真去连 ws://。 */
class FakeSocket {
  static latest: FakeSocket | null = null
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

beforeEach(() => {
  vi.useFakeTimers()
  useModelStore.getState().reset()
  useParamsStore.getState().reset()
})

afterEach(() => {
  useModelStore.getState().reset()
  useParamsStore.getState().reset()
  FakeSocket.latest = null
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('校验防抖（200 ms）', () => {
  it('连续改 5 次：199 ms 不发请求，200 ms 只发一次且带最后一次参数', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(REPORT))
    vi.stubGlobal('fetch', fetchMock)

    const profiles = [0, 1, 2, 3, 4].map((index) => ({
      ...DEFAULT_PROFILE,
      name: `第 ${index} 次`,
      base_radius: index * 0.1,
    }))
    for (const profile of profiles) useModelStore.getState().runValidation(profile)

    await vi.advanceTimersByTimeAsync(VALIDATE_DEBOUNCE_MS - 1)
    expect(fetchMock).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(call[0]).toBe('/api/geometry/validate')
    expect(JSON.parse(String(call[1].body))).toEqual(profiles[profiles.length - 1])

    await flushMicrotasks()
    expect(useModelStore.getState().validating).toBe(false)
    expect(useModelStore.getState().report?.envelope).toEqual([2, 2, 5])
  })

  it('校验失败时保留 §10.3 的 suggestion，并标记来源为 validate', async () => {
    const errorBody = {
      error: {
        code: 'GEOMETRY_INVALID',
        stage: 'meridian',
        message: '穹顶段两端中恰有一端必须落在轴线上（半径 0）',
        suggestion: '把 arc 段的 end_radius 改为 0，或让起点半径为 0',
      },
    }
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(errorBody, 422)))

    useModelStore.getState().runValidation(DEFAULT_PROFILE)
    await vi.advanceTimersByTimeAsync(VALIDATE_DEBOUNCE_MS)
    await flushMicrotasks()

    const error = useModelStore.getState().error
    expect(error?.origin).toBe('validate')
    expect(error?.code).toBe('GEOMETRY_INVALID')
    expect(error?.suggestion).toBe(errorBody.error.suggestion)
    expect(useModelStore.getState().validating).toBe(false)
  })
})

describe('双通道切换（ADR-012 / R-25）', () => {
  it('构建命中缓存即强制切到权威通道', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ cache_hit: true, key: 'sha256-cache-key', metrics: { volume: 5.2 } }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await useModelStore.getState().startBuild()

    const state = useModelStore.getState()
    expect(state.channel).toBe('authoritative')
    expect(state.authoritativeKey).toBe('sha256-cache-key')
    expect(state.building).toBe(false)
    expect(state.error).toBeNull()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('作业成功（WS 推送 result_key）后权威通道接管；失败则退回示意通道', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ cache_hit: false, key: 'k', job_id: 'job-1' })))
    vi.stubGlobal('WebSocket', FakeSocket)

    await useModelStore.getState().startBuild()
    expect(useModelStore.getState().building).toBe(true)
    expect(useModelStore.getState().channel).toBe('schematic')

    const socket = FakeSocket.latest
    expect(socket?.url.endsWith('/ws/jobs/job-1')).toBe(true)

    socket?.emit({
      job_id: 'job-1',
      status: 'succeeded',
      stage: 'done',
      progress: 1,
      result_key: 'sha256-job-key',
      metrics: { volume: 5.2 },
      created_at: '2026-09-19T00:00:00Z',
    })

    const success = useModelStore.getState()
    expect(success.building).toBe(false)
    expect(success.channel).toBe('authoritative')
    expect(success.authoritativeKey).toBe('sha256-job-key')

    // 用户在视口里切回示意通道应是允许的（权威产物仍留着，可再切回）
    success.useSchematic()
    expect(useModelStore.getState().channel).toBe('schematic')
    expect(useModelStore.getState().authoritativeKey).toBe('sha256-job-key')
  })

  it('构建参数取自母线草稿（唯一可写来源，§11.6 SSOT）', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ cache_hit: true, key: 'k' }))
    vi.stubGlobal('fetch', fetchMock)

    useParamsStore.getState().setBaseRadius(0.4)
    await useModelStore.getState().startBuild()

    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    const body = JSON.parse(String(call[1].body)) as { base_radius: number; segments: unknown[] }

    expect(call[0]).toBe('/api/geometry/build')
    expect(body).toEqual(useParamsStore.getState().profile)
    expect(body.base_radius).toBe(0.4)
  })
})
