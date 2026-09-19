import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { validateProfile, type MeridianProfile, type ValidationReport } from '../api/geometry'
import {
  DEFAULT_PROFILE,
  canAppendSegment,
  chainEndRadius,
  editorFromProfile,
  profileFromEditor,
  useParamsStore,
} from './params'

/**
 * 母线草稿的往返一致性与默认剖面合法性（§16.3 验收项 2 的前端侧）。
 *
 * 「往返一致」是母线保存/重载链路的同构表述：编辑器草稿与剖面之间不得丢失或
 * 篡改任何字段，否则用户每次保存都会静默改值（R-16 参数可复现）。
 */

/** 取段起点半径：段链首尾相接，故首段起点 = base_radius，其余段的起点 = 上一段终点。 */
function startRadiusOf(profile: MeridianProfile, index: number): number {
  return index === 0 ? profile.base_radius : profile.segments[index - 1].end_radius
}

function isDome(segment: MeridianProfile['segments'][number]): boolean {
  return segment.type === 'arc' || segment.type === 'ellipse'
}

/** 默认剖面的后端响应替身（只为让请求链路走通，数值本身由后端用例把关）。 */
const CONTRACT_OK_REPORT: ValidationReport = {
  ok: true,
  checks: [],
  joints: [],
  sample: [
    [0, 0],
    [1, 1],
  ],
  outline: [
    [0, 0],
    [1, 1],
    [0, 5],
  ],
  // 逐段闭合轮廓（下标对应 profile.segments）
  segment_outline: [
    [
      [0, 0],
      [1, 1],
      [0, 1],
    ],
    [
      [0, 1],
      [1, 1],
      [1, 4],
      [0, 4],
    ],
    [
      [0, 4],
      [1, 4],
      [0, 5],
    ],
  ],
  envelope: [2, 2, 5],
  volume: 1,
  surface_area: 1,
  centroid_z: 1,
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

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('母线编辑器草稿往返', () => {
  it('editorFromProfile → profileFromEditor 与原剖面深相等', () => {
    const source: MeridianProfile = {
      name: '往返用例',
      base_radius: 0.35,
      segments: [
        { type: 'line', length: 1.25, end_radius: 0.35 },
        { type: 'arc', length: 0.75, end_radius: 0 },
        { type: 'ellipse', length: 0.5, end_radius: 0.2 },
      ],
    }

    expect(profileFromEditor(editorFromProfile(source))).toEqual(source)
  })

  it('往返不共享可变对象（编辑草稿不会污染原剖面）', () => {
    const source: MeridianProfile = {
      name: '隔离用例',
      base_radius: 1,
      segments: [{ type: 'line', length: 2, end_radius: 1 }],
    }

    const draft = editorFromProfile(source)
    draft.segments[0].length = 99
    draft.baseRadius = 42

    expect(source.segments[0].length).toBe(2)
    expect(source.base_radius).toBe(1)
  })
})

describe('默认剖面', () => {
  it('段数、字段与穹顶判据均合法（后端 resolve 只接受恰有一端 r=0 的穹顶段）', () => {
    const profile = DEFAULT_PROFILE

    expect(profile.segments.length).toBeGreaterThanOrEqual(1)
    expect(profile.base_radius).toBeGreaterThanOrEqual(0)

    profile.segments.forEach((segment, index) => {
      expect(['line', 'arc', 'ellipse']).toContain(segment.type)
      expect(typeof segment.length).toBe('number')
      expect(typeof segment.end_radius).toBe('number')
      expect(segment.length).toBeGreaterThan(0)
      expect(segment.end_radius).toBeGreaterThanOrEqual(0)

      if (isDome(segment)) {
        const onAxis = [startRadiusOf(profile, index), segment.end_radius].filter(
          (radius) => radius === 0,
        )
        expect(onAxis).toHaveLength(1)
      }
    })

    // 轮廓必须闭合到轴线，否则尾段之后会凭空多出一个开口
    expect(chainEndRadius(profile)).toBe(0)
    expect(canAppendSegment(profile)).toBe(false)
  })

  it('能被 validateProfile 原样送出（JSON 可序列化、无字段丢失）', async () => {
    const fetchMock = vi.fn(async () => jsonResponse(CONTRACT_OK_REPORT))
    vi.stubGlobal('fetch', fetchMock)

    const report = await validateProfile(DEFAULT_PROFILE)
    expect(report.ok).toBe(true)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(call[0]).toBe('/api/geometry/validate')
    expect(JSON.parse(String(call[1].body))).toEqual(DEFAULT_PROFILE)
  })
})

describe('useParamsStore', () => {
  beforeEach(() => {
    useParamsStore.getState().reset()
  })

  afterEach(() => {
    useParamsStore.getState().reset()
  })

  it('追加段沿用末端半径；追加穹顶段时收拢到轴线，随后禁止继续追加', () => {
    // 开路剖面（柱段，末端半径 1）才允许追加；默认剖面已闭合到轴线，故此处先替换
    useParamsStore.getState().setProfile({
      name: '开路剖面',
      base_radius: 1,
      segments: [{ type: 'line', length: 2, end_radius: 1 }],
    })
    expect(canAppendSegment(useParamsStore.getState().profile)).toBe(true)

    useParamsStore.getState().addSegment('line')
    const opened = useParamsStore.getState().profile.segments
    expect(opened).toHaveLength(2)
    expect(opened[1]).toEqual({ type: 'line', length: 1, end_radius: 1 })

    useParamsStore.getState().addSegment('arc')
    const closed = useParamsStore.getState().profile.segments
    expect(closed).toHaveLength(3)
    expect(closed[closed.length - 1]).toEqual({ type: 'arc', length: 1, end_radius: 0 })
    expect(canAppendSegment(useParamsStore.getState().profile)).toBe(false)
  })

  it('默认剖面已闭合到轴线，故追加操作是空操作（不凭空造出非法剖面）', () => {
    useParamsStore.getState().addSegment('line')

    expect(useParamsStore.getState().profile.segments).toEqual(DEFAULT_PROFILE.segments)
  })

  it('段数下限为 1（后端 min_length=1，删空会立刻违约）', () => {
    useParamsStore.getState().removeSegment(0)
    useParamsStore.getState().removeSegment(1)

    expect(useParamsStore.getState().profile.segments).toHaveLength(1)
  })
})
