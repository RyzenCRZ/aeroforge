import { useEffect } from 'react'

import { act, fireEvent, render, screen } from '@testing-library/react'
import * as THREE from 'three'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useViewStore } from '../store/view'
import { applySceneViewState, type SceneViewState } from './sceneState'
import {
  applyExplodeView,
  collectExplodeGroups,
  explodeGroupKey,
  EXPLODE_GAP_RATIO,
} from './explode'
import { ViewportTools } from './Viewport'
import { useGlbScene } from './useGlbScene'

/**
 * 爆炸视图门禁（规格 §11.4 / OI-19 的 M5 部分 / §13.6）。
 *
 * 钉住的契约：
 *
 * 1. **取分区枚举，不硬编码件名**——分组从根 children 的实际节点名推导（级前缀
 *    `s1`/`b0` 分组、无级前缀名各自成组），组数随场景变化；
 * 2. **位移是视图偏移**——只改节点 `position`，`factor=0` 完全还原常态位置；
 * 3. **组序 × factor × 特征长度**——位移随组序单调、随 factor 线性、随长度等比；
 *    组内节点同偏移（相对位置不变）；
 * 4. **沿世界 Y 轴**——GLB 根自带 Rx(-90°)（局部 Z 才是世界 Y，见 orientation.ts），
 *    位移方向必须按父坐标系反解，两通道同一结果；
 * 5. **渲染层零后端请求**——爆炸状态变化不产生任何 GLB 重载与 fetch/WS（与显隐
 *    测试「0 次新增 GLB 请求」同款）。
 */

/** GLTFLoader 替身：不发真实请求，由用例按序兑现（useGlbScene.test 同款形态）。 */
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

function mesh(name: string, x = 0, y = 0, z = 0): THREE.Mesh {
  const mesh = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshStandardMaterial())
  mesh.name = name
  mesh.position.set(x, y, z)
  return mesh
}

/** 装配树形态（M5 契约）：根 `vehicle` → 级前缀部件节点 + 无级前缀部件节点。 */
function assemblyScene(): THREE.Group {
  const root = new THREE.Group()
  root.name = 'vehicle'
  root.add(mesh('s1-engine', 0, 0, 0))
  root.add(mesh('s1-ox-tank', 0, 4, 0))
  root.add(mesh('s2-fuel-tank', 0, 8, 0))
  root.add(mesh('fairing', 0, 12, 0))
  root.add(mesh('', 0, 20, 0)) // 空名节点：不可按名寻址，不参与（显隐硬约束 1 同源）
  return root
}

/** 示意通道形态：过渡期的 `seg-<i>` 节点（无级前缀 → 各自成组）。 */
function schematicScene(): THREE.Group {
  const root = new THREE.Group()
  for (const [index, y] of [0, 3, 6].entries()) {
    root.add(mesh(`seg-${index}`, 0, y, 0))
  }
  return root
}

beforeEach(() => {
  mockState.loads.length = 0
  useViewStore.getState().reset()
})

afterEach(() => {
  mockState.loads.length = 0
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  useViewStore.getState().reset()
})

describe('分组键：取分区枚举，不硬编码件名', () => {
  it('级（sN）/ 助推器（bN）前缀 = 组键；其余具名节点各自成组；空名不参与', () => {
    expect(explodeGroupKey('s1-ox-tank')).toBe('s1')
    expect(explodeGroupKey('s1')).toBe('s1')
    expect(explodeGroupKey('s10-engine-bay')).toBe('s10')
    expect(explodeGroupKey('b0-fin')).toBe('b0')
    // 过渡期分段名与未知部件名：各自成组（机制不动，后端换名即换粒度）
    expect(explodeGroupKey('seg-0')).toBe('seg-0')
    expect(explodeGroupKey('ox-tank')).toBe('ox-tank')
    expect(explodeGroupKey('')).toBeNull()
    // 前缀必须是 s/b+数字：普通词不误判成级
    expect(explodeGroupKey('solid-boost')).toBe('solid-boost')
  })

  it('collectExplodeGroups 按实际节点名分组（组数随场景变化，无数量假设）', () => {
    const root = assemblyScene()
    const groups = collectExplodeGroups(root)

    const byKey = new Map(groups.map((group) => [group.key, group.nodes.length]))
    expect(byKey.get('s1')).toBe(2) // s1-engine + s1-ox-tank 同组（组内相对位置保持）
    expect(byKey.get('s2')).toBe(1)
    expect(byKey.get('fairing')).toBe(1)
    expect(groups).toHaveLength(3) // 空名节点不计入
  })
})

describe('位移施加：组序 × factor × 特征长度（§11.4）', () => {
  it('factor 0→0.5：组序越高位移越大（单调），底部组不动；组内相对位置不变', () => {
    const root = assemblyScene()
    const length = 10
    const step = 0.5 * EXPLODE_GAP_RATIO * length // factor=0.5 的相邻组间距

    const groups = applyExplodeView(root, { factor: 0.5, length })
    expect(groups).toBe(3)

    const engine = root.getObjectByName('s1-engine')!
    const oxTank = root.getObjectByName('s1-ox-tank')!
    const fuelTank = root.getObjectByName('s2-fuel-tank')!
    const fairing = root.getObjectByName('fairing')!

    // 底部组（s1，组序 0）不动
    expect(engine.position.y).toBe(0)
    expect(oxTank.position.y).toBe(4)
    // 组序 1 / 2：位移 = 组序 × factor × 特征长度（沿世界 +Y）
    expect(fuelTank.position.y).toBeCloseTo(8 + step, 6)
    expect(fairing.position.y).toBeCloseTo(12 + 2 * step, 6)

    // 组内相对位置不变（同组两节点位移一致）
    expect(oxTank.position.y - engine.position.y).toBe(4)
  })

  it('位移 ∝ factor：0.25 的位移是 0.5 的一半；factor=0 完全还原常态位置', () => {
    const root = assemblyScene()
    const length = 10

    applyExplodeView(root, { factor: 0.5, length })
    const half = root.getObjectByName('s2-fuel-tank')!.position.y

    applyExplodeView(root, { factor: 0.25, length })
    const quarter = root.getObjectByName('s2-fuel-tank')!.position.y
    expect(quarter).toBeCloseTo(8 + (half - 8) / 2, 6)

    // factor=0：复位到原始位置（视图偏移不留残差）
    applyExplodeView(root, { factor: 0, length })
    expect(root.getObjectByName('s1-engine')!.position.y).toBe(0)
    expect(root.getObjectByName('s1-ox-tank')!.position.y).toBe(4)
    expect(root.getObjectByName('s2-fuel-tank')!.position.y).toBe(8)
    expect(root.getObjectByName('fairing')!.position.y).toBe(12)
  })

  it('幂等：同 factor 重复施加不累积位移；特征长度加倍位移等比放大', () => {
    const root = assemblyScene()
    applyExplodeView(root, { factor: 0.5, length: 10 })
    const once = root.getObjectByName('fairing')!.position.y
    applyExplodeView(root, { factor: 0.5, length: 10 })
    expect(root.getObjectByName('fairing')!.position.y).toBeCloseTo(once, 12)

    const scaled = new THREE.Group()
    scaled.add(mesh('s1-a', 0, 0, 0))
    scaled.add(mesh('s2-a', 0, 8, 0))
    applyExplodeView(scaled, { factor: 0.5, length: 20 })
    const withDouble = scaled.getObjectByName('s2-a')!.position.y
    expect(withDouble).toBeCloseTo(8 + 0.5 * EXPLODE_GAP_RATIO * 20, 6)
  })

  it('只改节点 position（视图偏移）：几何体实例与属性不被触碰', () => {
    const root = assemblyScene()
    const geometry = (root.getObjectByName('s1-engine') as THREE.Mesh).geometry
    applyExplodeView(root, { factor: 1, length: 10 })
    expect((root.getObjectByName('s1-engine') as THREE.Mesh).geometry).toBe(geometry)
  })

  it('factor 越界被夹到 0–1（滑杆越界不得产生无从解释的画面）', () => {
    const root = assemblyScene()
    applyExplodeView(root, { factor: -3, length: 10 })
    expect(root.getObjectByName('fairing')!.position.y).toBe(12)

    applyExplodeView(root, { factor: 9, length: 10 })
    expect(root.getObjectByName('fairing')!.position.y).toBeCloseTo(12 + 2 * EXPLODE_GAP_RATIO * 10, 6)
  })

  it('过渡期 seg-<i> 场景（示意通道）逐段分离——机制随节点名换粒度', () => {
    const root = schematicScene()
    const groups = applyExplodeView(root, { factor: 1, length: 6 })
    expect(groups).toBe(3)

    expect(root.getObjectByName('seg-0')!.position.y).toBe(0)
    expect(root.getObjectByName('seg-1')!.position.y).toBeCloseTo(3 + EXPLODE_GAP_RATIO * 6, 6)
    expect(root.getObjectByName('seg-2')!.position.y).toBeCloseTo(6 + 2 * EXPLODE_GAP_RATIO * 6, 6)
  })

  it('沿**世界** Y 轴：GLB 根自带 Rx(-90°) 时位移落在局部 Z 上（orientation.ts 契约）', () => {
    // 模拟 OCCT 导出器：根节点挂 Rx(-90°)（Z-up → Y-up），子节点用局部 Z 表达世界高度
    const root = new THREE.Group()
    root.name = 'vehicle'
    root.rotation.x = -Math.PI / 2
    root.add(mesh('s1-a', 0, 0, 0))
    root.add(mesh('s2-a', 0, 0, 10)) // 局部 z=10 → 世界 y=10

    applyExplodeView(root, { factor: 0.5, length: 20 })
    root.updateWorldMatrix(true, true)

    const offset = 0.5 * EXPLODE_GAP_RATIO * 20
    const world = root.getObjectByName('s2-a')!.getWorldPosition(new THREE.Vector3())
    expect(world.y).toBeCloseTo(10 + offset, 6)
    // 底部组世界位置不动
    expect(root.getObjectByName('s1-a')!.getWorldPosition(new THREE.Vector3()).y).toBeCloseTo(0, 6)
  })

  it('空场景（无可寻址节点）是安全无操作', () => {
    const root = new THREE.Group()
    expect(applyExplodeView(root, { factor: 1, length: 10 })).toBe(0)
  })
})

/** 与真实组件同构的施加链：`useGlbScene` 持有场景 + `applySceneViewState` 施加视图状态。 */
// ⚠ 失败回调必须是**稳定引用**：useGlbScene 的 effect 依赖 [url, onFailure]，
//   内联箭头函数每次渲染都是新引用，会导致场景被反复释放重载（真实组件用 useCallback）。
const noopFailure = (): void => undefined

function ExplodeHarness({ factor, length }: { factor: number; length: number }) {
  const scene = useGlbScene('/api/artifacts/explode-key/model_lod2.glb', noopFailure)
  const viewState: SceneViewState = {
    hidden: new Set<number>(),
    selected: null,
    highlight: null,
    clipPlanes: [],
    explode: { factor, length },
  }
  useEffect(() => {
    if (scene === null) return
    applySceneViewState(scene, viewState)
  }, [scene, viewState])
  return scene === null ? (
    <div data-testid="explode-loading" />
  ) : (
    <div data-testid="explode-ready" />
  )
}

describe('渲染层零后端请求（§11.4 / §13.6，与显隐门禁同款）', () => {
  it('爆炸因子变化：0 次新增 GLB 请求、零 fetch / WS，位移真实生效并随 factor=0 复位', async () => {
    const fetchSpy = vi.fn(() => Promise.reject(new Error('爆炸视图不得发起请求')))
    const socketSpy = vi.fn()
    vi.stubGlobal('fetch', fetchSpy)
    vi.stubGlobal('WebSocket', socketSpy)

    const scene = assemblyScene()
    const { rerender } = render(<ExplodeHarness factor={0} length={10} />)
    expect(mockState.loads).toHaveLength(1)

    await act(async () => {
      mockState.loads[0]!.onLoad({ scene })
    })
    expect(screen.getByTestId('explode-ready')).toBeInTheDocument()

    mockState.loads.length = 0

    // 拖动滑杆等价于 viewState 变化（factor 与长度都变一轮，含复位）
    rerender(<ExplodeHarness factor={0.5} length={10} />)
    expect(scene.getObjectByName('s2-fuel-tank')!.position.y).toBeCloseTo(
      8 + 0.5 * EXPLODE_GAP_RATIO * 10,
      6,
    )

    rerender(<ExplodeHarness factor={1} length={12} />)
    rerender(<ExplodeHarness factor={0} length={10} />)
    expect(scene.getObjectByName('s2-fuel-tank')!.position.y).toBe(8) // 复位即还原

    // 「0 次新增 GLB 请求」+ 零后端请求（fetch 与 WS 都盯住，只盯一个会静默失效）
    expect(mockState.loads).toHaveLength(0)
    expect(fetchSpy.mock.calls.length).toBe(0)
    expect(socketSpy.mock.calls.length).toBe(0)
  })
})

describe('视口工具条：滑杆 / 复位 / 爆炸态导出禁用（§11.4 / OI-26）', () => {
  it('滑杆交互写入 explodeFactor（视图状态）；爆炸态导出按钮禁用并提示「爆炸视图为视图状态」', () => {
    const onExportPng = vi.fn()
    render(<ViewportTools onExportPng={onExportPng} />)

    const exportButton = screen.getByRole('button', { name: '导出 PNG' })
    expect(exportButton).toBeEnabled()

    fireEvent.change(screen.getByRole('slider', { name: '爆炸视图' }), {
      target: { value: '0.5' },
    })
    expect(useViewStore.getState().explodeFactor).toBe(0.5)
    expect(screen.getByText('0.50')).toBeInTheDocument()

    // 爆炸态：导出禁用（视图状态不可导出）+ 冲突提示可见；点击不再触达导出
    expect(exportButton).toBeDisabled()
    expect(screen.getByText(/爆炸视图为视图状态/)).toBeInTheDocument()
    fireEvent.click(exportButton)
    expect(onExportPng).not.toHaveBeenCalled()

    // 复位：因子归零，导出恢复可用，提示消失
    fireEvent.click(screen.getByRole('button', { name: '复位' }))
    expect(useViewStore.getState().explodeFactor).toBe(0)
    expect(screen.getByRole('button', { name: '导出 PNG' })).toBeEnabled()
    expect(screen.queryByText(/爆炸视图为视图状态/)).not.toBeInTheDocument()
  })

  it('滑杆拖到 0 与 1 两个边界值都被接受（夹取在 store 完成）', () => {
    render(<ViewportTools onExportPng={() => undefined} />)
    fireEvent.change(screen.getByRole('slider', { name: '爆炸视图' }), {
      target: { value: '1' },
    })
    expect(useViewStore.getState().explodeFactor).toBe(1)
    fireEvent.change(screen.getByRole('slider', { name: '爆炸视图' }), {
      target: { value: '0' },
    })
    expect(useViewStore.getState().explodeFactor).toBe(0)
  })
})
