import { type ReactNode } from 'react'

import { act, renderHook } from '@testing-library/react'
import * as THREE from 'three'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useGlbScene } from './useGlbScene'

/**
 * GLTFLoader 的替身：不发任何真实请求，只记录每次 `load()` 的回调，由用例按序兑现。
 *
 * 必须钉死加载行为，因为本文件守的是 OI-19 实测暴露的回归：视图交互导致组件重渲染时，
 * 权威 GLB 被**反复重新请求**（根因是旧实现 `useLoader.clear` 与 StrictMode 双挂载互相打架）。
 */
const mockState = vi.hoisted(() => ({
  loads: [] as Array<{
    url: string
    onLoad: (gltf: { scene: THREE.Object3D }) => void
    onError: (error: unknown) => void
  }>,
}))

vi.mock('three/examples/jsm/loaders/GLTFLoader.js', () => ({
  GLTFLoader: class {
    load(
      url: string,
      onLoad: (gltf: { scene: THREE.Object3D }) => void,
      _onProgress: undefined,
      onError: (error: unknown) => void,
    ): void {
      mockState.loads.push({ url, onLoad, onError })
    }
  },
}))

/** 造一个结构与真实 GLB 相同的最小场景（`vehicle` → `seg-0` mesh），并监视释放调用。 */
function fakeGlb(label: string): {
  scene: THREE.Object3D
  geometry: THREE.BufferGeometry
  material: THREE.MeshBasicMaterial
} {
  const geometry = new THREE.BufferGeometry()
  vi.spyOn(geometry, 'dispose')
  const material = new THREE.MeshBasicMaterial()
  vi.spyOn(material, 'dispose')
  const mesh = new THREE.Mesh(geometry, material)
  mesh.name = `seg-${label}`
  const scene = new THREE.Object3D()
  scene.name = 'vehicle'
  scene.add(mesh)
  return { scene, geometry, material }
}

/** 兑现最早一次记录的加载（按调用顺序）。 */
async function resolveLoad(result: { scene: THREE.Object3D }): Promise<void> {
  const call = mockState.loads.shift()
  if (call === undefined) throw new Error('没有待兑现的加载记录')
  await act(async () => {
    call.onLoad(result)
  })
}

async function rejectLoad(error: unknown): Promise<void> {
  const call = mockState.loads.shift()
  if (call === undefined) throw new Error('没有待兑现的加载记录')
  await act(async () => {
    call.onError(error)
  })
}

const trivialWrapper = ({ children }: { children: ReactNode }) => children

describe('useGlbScene — 视图交互零请求（§11.4 / §13.6）', () => {
  beforeEach(() => {
    mockState.loads.length = 0
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('挂载时加载一次，兑现后持有场景，卸载时释放', async () => {
    const glb = fakeGlb('a')
    const onFailure = vi.fn()

    const hook = renderHook(() => useGlbScene('/api/artifacts/k1/model_lod2.glb', onFailure), {
      wrapper: trivialWrapper,
    })

    expect(mockState.loads).toHaveLength(1)
    expect(mockState.loads[0]?.url).toBe('/api/artifacts/k1/model_lod2.glb')
    expect(hook.result.current).toBeNull() // 加载期间保持 fallback

    await resolveLoad(glb)
    expect(hook.result.current).toBe(glb.scene)
    expect(glb.geometry.dispose).not.toHaveBeenCalled()
    expect(onFailure).not.toHaveBeenCalled()

    hook.unmount()
    expect(glb.geometry.dispose).toHaveBeenCalledTimes(1)
    expect(glb.material.dispose).toHaveBeenCalledTimes(1)
  })

  it('同一 url 的任意次重渲染都不再加载，场景引用保持不变（视图交互的门禁）', async () => {
    const glb = fakeGlb('stable')
    const onFailure = vi.fn()

    const hook = renderHook(() => useGlbScene('/api/artifacts/k2/model_lod2.glb', onFailure), {
      wrapper: trivialWrapper,
    })
    await resolveLoad(glb)
    mockState.loads.length = 0

    // 显隐 / 选中 / 剖切在真实组件里只改变视图状态：等价于这里的同 url 重渲染
    hook.rerender()
    hook.rerender()
    hook.rerender()
    hook.rerender()
    hook.rerender()

    expect(mockState.loads).toHaveLength(0)
    expect(hook.result.current).toBe(glb.scene)
  })

  it('url 变化时释放旧场景并加载新场景', async () => {
    const first = fakeGlb('old')
    const next = fakeGlb('new')
    const onFailure = vi.fn()

    const hook = renderHook(({ url }: { url: string }) => useGlbScene(url, onFailure), {
      initialProps: { url: '/api/artifacts/old/model_lod2.glb' },
      wrapper: trivialWrapper,
    })
    await resolveLoad(first)
    expect(hook.result.current).toBe(first.scene)

    mockState.loads.length = 0
    hook.rerender({ url: '/api/artifacts/new/model_lod2.glb' })
    expect(first.geometry.dispose).toHaveBeenCalledTimes(1) // 旧产物立即释放
    expect(mockState.loads).toHaveLength(1)
    expect(mockState.loads[0]?.url).toBe('/api/artifacts/new/model_lod2.glb')

    await resolveLoad(next)
    expect(hook.result.current).toBe(next.scene)

    hook.unmount()
    expect(next.geometry.dispose).toHaveBeenCalledTimes(1)
  })

  it('卸载后才兑现的请求：场景必须被释放，且不得写入已卸载的持有者', async () => {
    const late = fakeGlb('late')
    const onFailure = vi.fn()

    const hook = renderHook(() => useGlbScene('/api/artifacts/k3/model_lod2.glb', onFailure), {
      wrapper: trivialWrapper,
    })
    hook.unmount() // 模拟 StrictMode 首次挂载被立即清理（含通道快速切走的情形）

    await resolveLoad(late)
    expect(late.geometry.dispose).toHaveBeenCalledTimes(1)
    expect(onFailure).not.toHaveBeenCalled()
  })

  it('加载失败时把原因交给 onFailure，不抛出异常并保持 fallback', async () => {
    const onFailure = vi.fn()

    const hook = renderHook(() => useGlbScene('/api/artifacts/boom/model_lod2.glb', onFailure), {
      wrapper: trivialWrapper,
    })
    await rejectLoad(new Error('HTTP 404'))

    expect(onFailure).toHaveBeenCalledTimes(1)
    expect(onFailure).toHaveBeenCalledWith('HTTP 404')
    expect(hook.result.current).toBeNull()
  })
})
