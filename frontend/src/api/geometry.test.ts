import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ApiError,
  artifactUrl,
  fetchJob,
  JOB_HANDSHAKE_TIMEOUT_MS,
  loadContour,
  openJobStream,
  validateProfile,
  type MeridianProfile,
  type ValidationReport,
} from './geometry'

/**
 * 几何 API 的传输层契约（规格 §10.1 / §10.2 / §10.3）。
 *
 * 这里只验「怎么发、怎么解析、怎么把错误还原成异常」——数值正确性属于后端用例，
 * 前端不得对几何数值做任何再推导（ADR-011）。
 */

const PROFILE: MeridianProfile = {
  name: 'capsule',
  base_radius: 0.0,
  segments: [
    { type: 'arc', length: 1, end_radius: 1 },
    { type: 'line', length: 3, end_radius: 1 },
    { type: 'arc', length: 1, end_radius: 0 },
  ],
}

const REPORT: ValidationReport = {
  ok: true,
  checks: [{ check: '闭合性', severity: 'pass', detail: '轮廓闭合' }],
  joints: [{ index: 0, z: 1, radius: 1, angle_deg: 0, kind: 'g1', severity: 'pass' }],
  sample: [
    [0, 0],
    [1, 1],
  ],
  outline: [
    [0, 0],
    [1, 1],
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

/** 非 JSON 错误体（如反向代理吐出的 HTML）。 */
function nonJsonResponse(status: number): Response {
  return {
    ok: false,
    status,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON')
    },
  } as unknown as Response
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('请求构造', () => {
  it('validateProfile 以同源相对路径 POST JSON', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(REPORT))
    vi.stubGlobal('fetch', fetchMock)

    const report = await validateProfile(PROFILE)

    expect(report).toEqual(REPORT)
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/geometry/validate')
    expect(path.startsWith('/')).toBe(true)
    expect(init.method).toBe('POST')
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(JSON.parse(String(init.body))).toEqual(PROFILE)
  })

  it('fetchJob / loadContour 的标识经 URL 编码（防路径穿越）', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ id: 'a b', canonical: '{}', profile: PROFILE }))
      .mockResolvedValueOnce(
        jsonResponse({
          job_id: 'job/1',
          status: 'running',
          stage: 'solid',
          progress: 0.4,
          created_at: '2026-09-19T00:00:00Z',
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    await loadContour('a b/c')
    await fetchJob('job/1')

    expect((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[0]).toBe(
      '/api/geometry/contour/a%20b%2Fc',
    )
    expect((fetchMock.mock.calls[1] as unknown as [string, RequestInit])[0]).toBe(
      '/api/jobs/job%2F1',
    )
  })

  it('artifactUrl 只做同源相对路径拼接，不探测主机或端口', () => {
    const url = artifactUrl('sha256-abc', 'model_lod2.glb')

    expect(url).toBe('/api/artifacts/sha256-abc/model_lod2.glb')
    expect(url.startsWith('/')).toBe(true)
    expect(url).not.toMatch(/https?:|localhost|127\.0\.0\.1|:\d+/)
  })
})

describe('错误契约（§10.3）', () => {
  it('把 {error:{...}} 还原为带 suggestion 的 ApiError', async () => {
    const errorBody = {
      error: {
        code: 'GEOMETRY_G1_DISCONTINUITY',
        stage: 'meridian',
        message: '接头 1 的切向夹角 3.2° 超过 0.5°',
        details: { index: 1, angle_deg: 3.2 },
        suggestion: '减小相邻两段在接头处的斜率差，或改用 arc 段平滑过渡',
      },
    }
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(errorBody, 422)))

    const error = await validateProfile(PROFILE).catch((reason: unknown) => reason)

    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.name).toBe('ApiError')
    expect(apiError.code).toBe('GEOMETRY_G1_DISCONTINUITY')
    expect(apiError.stage).toBe('meridian')
    expect(apiError.message).toBe(errorBody.error.message)
    expect(apiError.suggestion).toBe(errorBody.error.suggestion)
    expect(apiError.details).toEqual({ index: 1, angle_deg: 3.2 })
  })

  it('错误体不是 JSON 时退回 HTTP 状态码文案，不吞状态', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => nonJsonResponse(502)))

    const error = (await validateProfile(PROFILE).catch((reason: unknown) => reason)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('HTTP_502')
    expect(error.suggestion).not.toBe('')
  })

  it('错误体缺 suggestion 时按契约不符处理（不得把畸形错误体当成功）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ error: { code: 'X', stage: 'api', message: '无建议' } }, 500)),
    )

    const error = (await validateProfile(PROFILE).catch((reason: unknown) => reason)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('HTTP_500')
  })

  it('响应形状不符时不放行坏数据（CONTRACT_MISMATCH）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ ok: true })))

    const error = (await validateProfile(PROFILE).catch((reason: unknown) => reason)) as ApiError

    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('CONTRACT_MISMATCH')
    expect(error.message).toContain('/api/geometry/validate')
  })
})

describe('作业进度流（§10.2，WS 不可用时的降级入口）', () => {
  /** WS 替身：只保留被测代码真正依赖的成员。 */
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
      /* 无副作用 */
    }
  }

  const RECORD = {
    job_id: 'job-1',
    status: 'running',
    stage: 'solid',
    progress: 0.4,
    created_at: '2026-09-19T00:00:00Z',
  }

  it('WS 地址由当前页面协议与主机推导（禁止硬编码主机名/端口）', () => {
    vi.stubGlobal('WebSocket', FakeSocket)

    const stream = openJobStream('job-1', { onRecord: vi.fn() })
    const expected = `${
      window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    }//${window.location.host}/ws/jobs/job-1`
    expect(FakeSocket.latest?.url).toBe(expected)

    stream.close()
  })

  it('投递合法快照给 onRecord；畸形消息被丢弃', () => {
    vi.stubGlobal('WebSocket', FakeSocket)
    const onRecord = vi.fn()

    const stream = openJobStream('job-1', { onRecord })
    const socket = FakeSocket.latest

    socket?.onmessage?.({ data: JSON.stringify(RECORD) } as MessageEvent)
    socket?.onmessage?.({ data: '{ 不是 JSON' } as MessageEvent)
    socket?.onmessage?.({ data: JSON.stringify({ job_id: 'job-1' }) } as MessageEvent)

    expect(onRecord).toHaveBeenCalledTimes(1)
    expect(onRecord).toHaveBeenCalledWith(RECORD)

    stream.close()
  })

  it('传输异常与关闭都会回调调用方（供 store 降级为轮询）', () => {
    vi.stubGlobal('WebSocket', FakeSocket)
    const onError = vi.fn()
    const onClose = vi.fn()

    openJobStream('job-1', { onRecord: vi.fn(), onError, onClose })
    const socket = FakeSocket.latest

    socket?.onerror?.({} as Event)
    socket?.onclose?.({} as CloseEvent)

    expect(onError).toHaveBeenCalledTimes(1)
    expect(onError.mock.calls[0][0]).toBeInstanceOf(ApiError)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('握手挂住（既不 open 也不 error/close）时按超时降级，不得静默卡住', () => {
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', FakeSocket)
    const onError = vi.fn()
    const onRecord = vi.fn()

    const stream = openJobStream('job-1', { onRecord, onError })

    // 反向代理未转发 WS 升级请求时，socket 会无限停在 CONNECTING：
    // 三个回调一个都不会来，只有握手超时能救。
    vi.advanceTimersByTime(JOB_HANDSHAKE_TIMEOUT_MS - 1)
    expect(onError).not.toHaveBeenCalled()

    vi.advanceTimersByTime(1)
    expect(onError).toHaveBeenCalledTimes(1)
    const error = onError.mock.calls[0][0] as ApiError
    expect(error).toBeInstanceOf(ApiError)
    expect(error.code).toBe('JOB_STREAM_HANDSHAKE_TIMEOUT')
    expect(error.suggestion).not.toBe('')

    // 放弃后迟到事件不得回流（回调已摘除），也不得重复降级
    const socket = FakeSocket.latest
    socket?.onopen?.({} as Event)
    socket?.onmessage?.({ data: JSON.stringify(RECORD) } as MessageEvent)
    socket?.onclose?.({} as CloseEvent)
    vi.advanceTimersByTime(JOB_HANDSHAKE_TIMEOUT_MS * 2)
    expect(onError).toHaveBeenCalledTimes(1)
    expect(onRecord).not.toHaveBeenCalled()

    stream.close()
  })

  it('握手成功（open）后超时不再触发降级', () => {
    vi.useFakeTimers()
    vi.stubGlobal('WebSocket', FakeSocket)
    const onError = vi.fn()

    const stream = openJobStream('job-1', { onRecord: vi.fn(), onError })
    FakeSocket.latest?.onopen?.({} as Event)

    vi.advanceTimersByTime(JOB_HANDSHAKE_TIMEOUT_MS * 3)
    expect(onError).not.toHaveBeenCalled()

    stream.close()
  })
})
