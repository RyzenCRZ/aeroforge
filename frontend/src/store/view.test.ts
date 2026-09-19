import { afterEach, describe, expect, it, vi } from 'vitest'

import { useParamsStore } from './params'
import { DEFAULT_CLIP, useViewStore } from './view'

/**
 * 显隐与剖切是**纯视图状态**（规格 §11.4 / §13.6 / §11.6）。
 *
 * 两条判据，缺一条就退化成"看着像对"：
 *
 * 1. 对 `store/view` 的任何操作都**零后端请求**——显隐与剖切不重算几何；
 * 2. 操作前后 `store/params` 的 `profile` **逐字段不变**——视图状态不得写回参数
 *    （P1 单一真相源：任何写回都会让"看到的"与"算的"分叉）。
 *
 * ⚠ 钉"没发请求"必须同时盯住 `fetch` 与 `WebSocket`：只盯前者，将来改用 WS 就静默失效。
 */

function spyOnTransport(): { requests: () => number } {
  const fetchSpy = vi.fn(() => Promise.reject(new Error('视图状态不得发起请求')))
  const socketSpy = vi.fn()
  vi.stubGlobal('fetch', fetchSpy)
  vi.stubGlobal('WebSocket', socketSpy)
  return {
    requests: () => fetchSpy.mock.calls.length + socketSpy.mock.calls.length,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  useViewStore.getState().reset()
  useParamsStore.getState().reset()
})

describe('显隐 / 剖切的取值', () => {
  it('默认不隐藏任何段、不剖切', () => {
    const state = useViewStore.getState()
    expect([...state.hiddenSegments]).toEqual([])
    expect(state.selectedSegment).toBeNull()
    expect(state.clip).toEqual(DEFAULT_CLIP)
    expect(state.clip.enabled).toBe(false)
  })

  it('显隐按段下标增删，且每次都换新集合（否则 zustand 不会通知订阅者）', () => {
    const before = useViewStore.getState().hiddenSegments
    useViewStore.getState().toggleSegmentHidden(1)
    const after = useViewStore.getState().hiddenSegments

    expect(after).not.toBe(before)
    expect([...after]).toEqual([1])

    useViewStore.getState().toggleSegmentHidden(1)
    expect([...useViewStore.getState().hiddenSegments]).toEqual([])
  })

  it('`setSegmentHidden` 幂等：重复设同一个值不产生新集合', () => {
    useViewStore.getState().setSegmentHidden(0, true)
    const once = useViewStore.getState().hiddenSegments
    useViewStore.getState().setSegmentHidden(0, true)
    expect(useViewStore.getState().hiddenSegments).toBe(once)
  })

  it('剖切位置被夹在 0–1，越界不会产生"整个模型消失"的无从解释画面', () => {
    useViewStore.getState().setClipPosition(-3)
    expect(useViewStore.getState().clip.position).toBe(0)
    useViewStore.getState().setClipPosition(9)
    expect(useViewStore.getState().clip.position).toBe(1)
  })

  it('reset 同时清空显隐、选中与剖切', () => {
    useViewStore.getState().toggleSegmentHidden(2)
    useViewStore.getState().selectSegment(2)
    useViewStore.getState().setClipEnabled(true)
    useViewStore.getState().setClipAxis('radial')

    useViewStore.getState().reset()

    const state = useViewStore.getState()
    expect([...state.hiddenSegments]).toEqual([])
    expect(state.selectedSegment).toBeNull()
    expect(state.clip).toEqual(DEFAULT_CLIP)
  })
})

describe('纯视图状态（§13.6）', () => {
  it('显隐 / 选中 / 剖切的全部操作零后端请求，且 store/params 逐字段不变', () => {
    const transport = spyOnTransport()
    const profileBefore = useParamsStore.getState().profile

    const view = useViewStore.getState()
    view.toggleSegmentHidden(0)
    view.toggleSegmentHidden(1)
    view.setSegmentHidden(1, false)
    view.showAllSegments()
    view.selectSegment(2)
    view.selectSegment(null)
    view.setClipEnabled(true)
    view.setClipAxis('radial')
    view.setClipPosition(0.73)
    view.setClipPosition(0.2)
    view.setLightIntensity(1.5)
    view.toggleWireframe()
    view.reset()

    expect(transport.requests()).toBe(0)
    // 同一对象引用：连"值相等但是新对象"都不允许（写回参数一定会换新对象）
    expect(useParamsStore.getState().profile).toBe(profileBefore)
    expect(useParamsStore.getState().profile).toEqual(profileBefore)
  })
})
