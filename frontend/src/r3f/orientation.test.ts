import { describe, expect, it } from 'vitest'
import * as THREE from 'three'

import { AUTHORITATIVE_ROTATION } from './orientation'
import { buildSchematicGeometry } from './SchematicMesh'

/**
 * 双通道朝向一致性门禁（ADR-012 / R-25 / §13.6）。
 *
 * M1 收官时两通道只对拍了**尺寸**：示意侧断言 Lathe 包围盒 = (2R, L, 2R)，权威侧断言 GLB
 * 文件包围盒 = (2R, 2R, L)——"哪根轴搬到世界的哪根轴"这一步此前只靠
 * `AuthoritativeModel.tsx` 里一句 `rotation={[-π/2, 0, 0]}` 的**静态推演**衔接。
 *
 * **M2 首项目视核对证明该推演是错的**：OCCT 导出器已给根节点写了 `Rx(-90°)`（glTF 的
 * Z-up → Y-up），`GLTFLoader` 会施加它，故权威通道的世界朝向**本来就是 Y-up**；再转一次
 * 就成了 -180°，模型躺倒，而**包围盒尺寸完全不变**——尺寸门禁对此完全失明。
 *
 * 因此本文件把"朝向"变成断言：两侧的**世界**包围盒都必须把回转轴放在世界 Y、自 y = 0 起。
 * 后端 `test_dual_channel_orientation.py` 用真实 GLB 顶点复核同一契约；本文件用真实的
 * three 对象复核。两处都改才算改对。
 */

/** 与后端柱 r=1 / L=3 同形的构型：L / 2R = 1.5，足以区分轴向与径向。 */
const RADIUS = 1.0
const LENGTH = 3.0

/** 修复前的错误常量：把访问器局部坐标当世界坐标后又转 -90°。自检专用，勿在别处引用。 */
const STALE_AUTHORITATIVE_ROTATION: [number, number, number] = [-Math.PI / 2, 0, 0]

/** 后端 `ValidationReport.outline` 同形（闭合轮廓，(r, z)，米）。 */
function cylinderOutline(): [number, number][] {
  return [
    [0, 0],
    [RADIUS, 0],
    [RADIUS, LENGTH],
    [0, LENGTH],
  ]
}

/** 把包围盒按给定欧拉角搬到世界坐标——即 `<group rotation={...}>` 的作用。 */
function toWorldBox(box: THREE.Box3, rotation: [number, number, number]): THREE.Box3 {
  const matrix = new THREE.Matrix4().makeRotationFromEuler(
    new THREE.Euler(rotation[0], rotation[1], rotation[2]),
  )
  const corners: THREE.Vector3[] = []
  for (const x of [box.min.x, box.max.x]) {
    for (const y of [box.min.y, box.max.y]) {
      for (const z of [box.min.z, box.max.z]) {
        corners.push(new THREE.Vector3(x, y, z).applyMatrix4(matrix))
      }
    }
  }
  return new THREE.Box3().setFromPoints(corners)
}

/**
 * 权威通道的**世界**包围盒：`GLTFLoader` 已把根节点的 `Rx(-90°)` 施加到顶点上，
 * 故 x/z 是径向（±R）、y 是轴向（0 → L）。数值取自实测（后端 `glb_bounding_box`）。
 */
function glbWorldBox(): THREE.Box3 {
  return new THREE.Box3(
    new THREE.Vector3(-RADIUS, 0, -RADIUS),
    new THREE.Vector3(RADIUS, LENGTH, RADIUS),
  )
}

/** 对照组：GLB **访问器**里的 min/max——Z-up 的**局部**坐标，不是渲染器看到的空间。 */
function glbAccessorBox(): THREE.Box3 {
  return new THREE.Box3(
    new THREE.Vector3(-RADIUS, -RADIUS, 0),
    new THREE.Vector3(RADIUS, RADIUS, LENGTH),
  )
}

/** 示意通道（Lathe，无旋转）的世界包围盒。 */
function schematicWorldBox(): THREE.Box3 {
  const geometry = buildSchematicGeometry(cylinderOutline())
  const box = new THREE.Box3().setFromBufferAttribute(
    geometry.getAttribute('position') as THREE.BufferAttribute,
  )
  geometry.dispose()
  return box
}

function sizeOf(box: THREE.Box3): number[] {
  return box.getSize(new THREE.Vector3()).toArray()
}

function relativeError(measured: number, reference: number): number {
  return Math.abs(measured - reference) / reference
}

function expectAxesAligned(size: number[], minY: number): void {
  expect(relativeError(size[1], LENGTH)).toBeLessThanOrEqual(0.01)
  expect(relativeError(size[0], 2 * RADIUS)).toBeLessThanOrEqual(0.01)
  expect(relativeError(size[2], 2 * RADIUS)).toBeLessThanOrEqual(0.01)
  // 起点仍落在原点：若变换里混入平移、或换了轴，这里会立刻暴露
  expect(minY).toBeCloseTo(0, 9)
}

describe('双通道朝向契约', () => {
  it('权威通道：GLB 世界朝向已是 Y-up，故对齐常量必须是恒等', () => {
    expect([...AUTHORITATIVE_ROTATION]).toEqual([0, 0, 0])

    const world = toWorldBox(glbWorldBox(), AUTHORITATIVE_ROTATION)
    expectAxesAligned(sizeOf(world), world.min.y)
  })

  it('示意通道：Lathe 包围盒同样把轴向放在世界 Y，且自 y = 0 起', () => {
    // 示意通道不得加旋转——加了就会与权威通道分叉（见 `SchematicMesh.tsx`）
    const box = schematicWorldBox()
    expectAxesAligned(sizeOf(box), box.min.y)
  })

  it('两通道世界包围盒逐轴一致（R-25 判据：偏差 ≤ 1%）', () => {
    const authoritative = sizeOf(toWorldBox(glbWorldBox(), AUTHORITATIVE_ROTATION))
    const schematic = sizeOf(schematicWorldBox())

    for (const axis of [0, 1, 2]) {
      expect(relativeError(authoritative[axis], schematic[axis])).toBeLessThanOrEqual(0.01)
    }
  })

  it('反向自检：沿用修复前的 -90° 常量时两通道必然分叉（证明本门禁能拦住该缺陷）', () => {
    const staleSize = sizeOf(toWorldBox(glbWorldBox(), STALE_AUTHORITATIVE_ROTATION))
    const schematic = sizeOf(schematicWorldBox())

    // 再转 -90° 会把轴向从世界 Y 挪到世界 Z——三边长度没变，但**逐轴的归属**变了。
    // 当初之所以没被发现：两侧的尺寸断言各自在自己的坐标系里自洽（后端拿的是访问器
    // 局部坐标、前端只测 Lathe 自身），没有任何一条断言跨到"渲染器实际看到的世界"。
    expect(relativeError(staleSize[2], LENGTH)).toBeLessThanOrEqual(0.01)
    expect(relativeError(staleSize[1], LENGTH)).toBeGreaterThan(0.01)

    const mismatchedAxes = [0, 1, 2].filter(
      (axis) => relativeError(staleSize[axis], schematic[axis]) > 0.01,
    )
    expect(mismatchedAxes).not.toHaveLength(0)
  })

  it('反向自检：把 GLB 访问器局部包围盒当作世界包围盒时同样分叉', () => {
    // 这正是当初的误判：访问器的 min/max 是 Z-up 局部坐标，比渲染器实际所见多缺一次 Rx(-90°)
    const misread = sizeOf(toWorldBox(glbAccessorBox(), AUTHORITATIVE_ROTATION))

    expect(relativeError(misread[2], LENGTH)).toBeLessThanOrEqual(0.01)
    expect(relativeError(misread[1], LENGTH)).toBeGreaterThan(0.01)
  })
})
