import { describe, expect, it } from 'vitest'
import * as THREE from 'three'

import {
  applySceneViewState,
  buildClippingPlane,
  resolveSegmentIndex,
  restoreOriginalMaterials,
  segmentIndexFromName,
  segmentNodeName,
  type SceneViewState,
} from './sceneState'

/**
 * 视口视图状态的施加器门禁（规格 §11.4 / §13.6）。
 *
 * 这里钉的是三条**硬约束**与两通道**同粒度**——它们全都是"不报错但不对"的类型：
 * 节点名对不上就是"点了没反应"，高亮就地改色就是"所有段一起亮"，剖面数量变化不重编译
 * 就是"剖切看起来生效了但着色器还是旧的"。三条都不会抛异常，只能靠断言钉住。
 */

function state(patch: Partial<SceneViewState> = {}): SceneViewState {
  return {
    hidden: new Set<number>(),
    selected: null,
    highlight: null,
    clipPlanes: [],
    explode: null,
    ...patch,
  }
}

function meshesOf(root: THREE.Object3D): THREE.Mesh[] {
  const found: THREE.Mesh[] = []
  root.traverse((object) => {
    if (object instanceof THREE.Mesh) found.push(object)
  })
  return found
}

/** 该对象在场景里是否真的可见（逐级上溯 `visible`——父节点隐藏时子节点也要算隐藏）。 */
function visibleInScene(object: THREE.Object3D): boolean {
  let node: THREE.Object3D | null = object
  while (node !== null) {
    if (!node.visible) return false
    node = node.parent
  }
  return true
}

/** 当前被隐藏的段下标（**只看具名节点**，与前端施加逻辑同一口径）。 */
function hiddenIndices(root: THREE.Object3D): number[] {
  const found: number[] = []
  root.traverse((object) => {
    const index = segmentIndexFromName(object.name)
    if (index !== null && !object.visible) found.push(index)
  })
  return found.sort((left, right) => left - right)
}

/**
 * **权威通道**的场景形态：`vehicle`（根，不持 mesh）→ 具名分段节点。
 *
 * 刻意混合两种形态，因为 `GLTFLoader` 对它们的产物不同：
 * - 单 primitive 的节点 → 直接是一个 `Mesh`（名 `seg-0`）；
 * - 多 primitive 的节点 → 一个 `Group`（名 `seg-1`）+ 子网格（名 `seg-1_0`…）。
 * 子网格自身解析不出段下标，只能靠向上找祖先——这正是最容易被漏掉的那条路径。
 */
function glbLikeScene(): THREE.Group {
  const root = new THREE.Group()
  root.name = 'vehicle'

  const shared = new THREE.MeshStandardMaterial()

  const plain = new THREE.Mesh(new THREE.BufferGeometry(), shared)
  plain.name = segmentNodeName(0)
  root.add(plain)

  const grouped = new THREE.Group()
  grouped.name = segmentNodeName(1)
  for (let primitive = 0; primitive < 2; primitive += 1) {
    const child = new THREE.Mesh(new THREE.BufferGeometry(), shared)
    child.name = `${segmentNodeName(1)}_${primitive}`
    grouped.add(child)
  }
  root.add(grouped)

  const tail = new THREE.Mesh(new THREE.BufferGeometry(), shared)
  tail.name = segmentNodeName(2)
  root.add(tail)

  return root
}

/** **示意通道**的场景形态：自建网格，节点名与权威通道**同名**（`seg-<i>`）。 */
function schematicLikeScene(indices: number[]): THREE.Group {
  const group = new THREE.Group()
  for (const index of indices) {
    const mesh = new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshStandardMaterial())
    mesh.name = segmentNodeName(index)
    group.add(mesh)
  }
  return group
}

describe('段节点名 —— 两通道同粒度的唯一凭据', () => {
  it('`segmentNodeName` 与 `segmentIndexFromName` 往返一致（两通道才可能在"同一段"上联动）', () => {
    for (const index of [0, 1, 7, 42]) {
      expect(segmentIndexFromName(segmentNodeName(index))).toBe(index)
    }
  })

  it('非分段名字一律返回 null —— 宁可不动，也不能解析歪到别的段上', () => {
    for (const name of ['vehicle', '', 'seg-', 'seg-x', 'seg-1.5', 'seg--1', 'seg-1_0', 'seg- 1']) {
      expect(segmentIndexFromName(name)).toBeNull()
    }
  })

  it('`resolveSegmentIndex` 向上找最近的分段祖先（多 primitive 时节点会被包成组）', () => {
    const root = glbLikeScene()
    const grouped = root.getObjectByName('seg-1')
    const child = grouped?.children[0] ?? null

    expect(grouped).not.toBeNull()
    expect(child).not.toBeNull()
    expect(resolveSegmentIndex(child)).toBe(1)
    expect(resolveSegmentIndex(root)).toBeNull()
    expect(resolveSegmentIndex(null)).toBeNull()
  })
})

describe('分级显隐（§11.4 硬约束 1 / 3）', () => {
  it('隐藏命中的段节点，根节点与未隐藏的段不受影响', () => {
    const root = glbLikeScene()

    const nodes = applySceneViewState(root, state({ hidden: new Set([1]) }))

    expect(nodes).toBe(3)
    expect(root.getObjectByName('seg-0')?.visible).toBe(true)
    expect(root.getObjectByName('seg-1')?.visible).toBe(false)
    expect(root.getObjectByName('seg-2')?.visible).toBe(true)
    // 根节点不参与显隐（它不持 mesh；若把根隐藏，整支箭都会消失）
    expect(root.visible).toBe(true)
    expect(hiddenIndices(root)).toEqual([1])
  })

  it('隐藏一个不存在的段下标不报错，也不隐藏任何东西（硬约束 1 与 3）', () => {
    const root = glbLikeScene()

    expect(() => applySceneViewState(root, state({ hidden: new Set([7]) }))).not.toThrow()
    expect(hiddenIndices(root)).toEqual([])
    expect(meshesOf(root).every(visibleInScene)).toBe(true)
  })

  it('示意通道缺了退化段的节点时，隐藏该下标同样无事发生（两通道节点集本就一致）', () => {
    // 后端对退化段返回空轮廓 → 示意通道不建网格；几何层也不产出该段节点
    const root = schematicLikeScene([0, 1])

    expect(() => applySceneViewState(root, state({ hidden: new Set([2]) }))).not.toThrow()
    expect(hiddenIndices(root)).toEqual([])
  })
})

describe('两通道同粒度（§11.4）', () => {
  it('同一份视图状态施加在两种场景形态上，隐藏的是**同一段**', () => {
    const authoritative = glbLikeScene()
    const schematic = schematicLikeScene([0, 1, 2])
    const viewState = state({ hidden: new Set([0, 2]) })

    applySceneViewState(authoritative, viewState)
    applySceneViewState(schematic, viewState)

    expect(hiddenIndices(authoritative)).toEqual([0, 2])
    expect(hiddenIndices(schematic)).toEqual([0, 2])
  })
})

describe('选中高亮 —— 材质替换，而非就地改色', () => {
  it('只有选中段换成高亮材质；共享的原材质**一个字段都不许改**', () => {
    const root = glbLikeScene()
    const highlight = new THREE.MeshStandardMaterial()
    const shared = (root.getObjectByName('seg-0') as THREE.Mesh).material as THREE.MeshStandardMaterial
    const emissiveBefore = shared.emissive.getHex()

    applySceneViewState(root, state({ selected: 1, highlight }))

    // seg-1 的两条子网格都换成高亮材质（段下标由祖先解析得出）
    const grouped = root.getObjectByName('seg-1')
    expect(meshesOf(grouped ?? root).every((mesh) => mesh.material === highlight)).toBe(true)
    expect((root.getObjectByName('seg-0') as THREE.Mesh).material).toBe(shared)
    expect((root.getObjectByName('seg-2') as THREE.Mesh).material).toBe(shared)
    // GLB 各段引用同一个材质实例：就地改 emissive 会把所有段一起点亮
    expect(shared.emissive.getHex()).toBe(emissiveBefore)
  })

  it('取消选中后还原为原材质（同一个实例，不是等价副本）', () => {
    const root = glbLikeScene()
    const highlight = new THREE.MeshStandardMaterial()
    const shared = (root.getObjectByName('seg-0') as THREE.Mesh).material

    applySceneViewState(root, state({ selected: 0, highlight }))
    expect((root.getObjectByName('seg-0') as THREE.Mesh).material).toBe(highlight)

    applySceneViewState(root, state({ selected: null, highlight }))
    expect((root.getObjectByName('seg-0') as THREE.Mesh).material).toBe(shared)
  })

  it('`restoreOriginalMaterials` 把被替换的材质还原——释放前必须先做，否则会连带释放视图层共享的那一个', () => {
    const root = glbLikeScene()
    const highlight = new THREE.MeshStandardMaterial()
    const shared = (root.getObjectByName('seg-0') as THREE.Mesh).material

    applySceneViewState(root, state({ selected: 0, highlight }))
    restoreOriginalMaterials(root)

    expect(meshesOf(root).every((mesh) => mesh.material === shared)).toBe(true)
  })

  it('非分段网格（段下标解析为 null）不参与高亮——不得误把根/装饰物点亮', () => {
    const group = new THREE.Group()
    const original = new THREE.MeshStandardMaterial()
    const decoration = new THREE.Mesh(new THREE.BufferGeometry(), original)
    decoration.name = 'grid-floor'
    group.add(decoration)

    applySceneViewState(group, state({ selected: 0, highlight: new THREE.MeshStandardMaterial() }))

    expect(decoration.material).toBe(original)
  })
})

describe('剖切（§11.4）', () => {
  it('轴向：法向 −Y、constant = t·L，保留 y ≤ t·L 的一侧', () => {
    const plane = buildClippingPlane('axial', 0.5, { length: 5, radius: 1 })

    expect(plane.normal.toArray()).toEqual([0, -1, 0])
    expect(plane.constant).toBeCloseTo(2.5, 12)
    expect(plane.distanceToPoint(new THREE.Vector3(0, 1, 0))).toBeGreaterThan(0)
    expect(plane.distanceToPoint(new THREE.Vector3(0, 4, 0))).toBeLessThan(0)
  })

  it('径向：法向 −X，位置自 −R 扫到 +R（0.5 即过轴平面）', () => {
    expect(buildClippingPlane('radial', 0, { length: 5, radius: 2 }).constant).toBeCloseTo(-2, 12)
    expect(buildClippingPlane('radial', 0.5, { length: 5, radius: 2 }).constant).toBeCloseTo(0, 12)
    expect(buildClippingPlane('radial', 1, { length: 5, radius: 2 }).constant).toBeCloseTo(2, 12)
    expect(buildClippingPlane('radial', 0.5, { length: 5, radius: 2 }).normal.toArray()).toEqual([
      -1, 0, 0,
    ])
  })

  it('位置越界被夹到 0–1（越界会让模型整体消失，画面无从解释）', () => {
    expect(buildClippingPlane('axial', -4, { length: 5, radius: 1 }).constant).toBeCloseTo(0, 12)
    expect(buildClippingPlane('axial', 4, { length: 5, radius: 1 }).constant).toBeCloseTo(5, 12)
  })

  it('平面**数量**变化必须重编译着色器；仅挪位置不重编译（拖滑杆才不会卡）', () => {
    const root = glbLikeScene()
    const material = (root.getObjectByName('seg-0') as THREE.Mesh).material as THREE.Material

    applySceneViewState(root, state({ clipPlanes: [new THREE.Plane()] }))
    expect(material.clippingPlanes).toHaveLength(1)
    const versionWithOnePlane = material.version

    applySceneViewState(root, state({ clipPlanes: [new THREE.Plane(new THREE.Vector3(0, -1, 0), 3)] }))
    expect(material.clippingPlanes?.[0]?.constant).toBeCloseTo(3, 12)
    expect(material.version).toBe(versionWithOnePlane)

    applySceneViewState(root, state({ clipPlanes: [] }))
    expect(material.clippingPlanes).toBeNull()
    expect(material.version).toBeGreaterThan(versionWithOnePlane)
  })

  it('剖切施加到全部网格（含未具名的子网格），且不改动可见性', () => {
    const root = glbLikeScene()
    const plane = new THREE.Plane(new THREE.Vector3(0, -1, 0), 1)

    applySceneViewState(root, state({ clipPlanes: [plane] }))

    const materials = meshesOf(root).map((mesh) =>
      Array.isArray(mesh.material) ? mesh.material[0] : mesh.material,
    )
    expect(materials.every((material) => material?.clippingPlanes?.[0] === plane)).toBe(true)
    expect(hiddenIndices(root)).toEqual([])
  })
})
