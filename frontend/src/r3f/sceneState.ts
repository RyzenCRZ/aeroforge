import * as THREE from 'three'

import type { ClipAxis } from '../store/view'
import { applyExplodeView, type ExplodeViewState } from './explode'

/**
 * 视口视图状态在 three 场景上的施加器（规格 §11.4 分级显隐 / 剖切 / 爆炸视图）。
 *
 * **两通道走同一条施加路径**（`applySceneViewState`）——这是"两通道同粒度"的落地方式：
 * 若示意通道按 React 声明式、权威通道按遍历改，两边的显隐/高亮/裁剪/爆炸就会各自演化，
 * 最终出现"隐藏只在一侧生效"这类分叉。
 *
 * 三条硬约束（OI-33，每条都对应一种静默失效）：
 *
 * 1. **节点名必须存在**——按名找不到节点即**不隐藏**（表现为"点了没反应"，全程零报错）；
 * 2. **根节点（`vehicle`）不持 mesh**——分段体已完全覆盖它，叠加渲染会把几何画厚一层；
 * 3. **退化段不产出节点**——对不存在的段做显隐与高亮都是**无操作**，且不得报错。
 *
 * 爆炸视图（OI-19 的 M5 部分）也走本路径：位移是**视图偏移**（只改节点 `position`，
 * 施加逻辑见 `explode.ts`），与显隐/剖切同受"纯视图状态"红线约束（§11.4）。
 *
 * 本文件只读写 three 对象：不含任何几何推导，不读时间，也不触发后端请求
 * （ADR-011 / §11.6「视图存储的任何变化不得触发后端请求」）。
 */

/** GLB 分段节点名前缀，与后端 `revolve.GLB_SEGMENT_PREFIX` 同字面量。 */
export const GLB_SEGMENT_PREFIX = 'seg-'

/** 第 `index` 段的 GLB 节点名（与后端 `revolve.segment_label` 是同一契约）。 */
export function segmentNodeName(index: number): string {
  return `${GLB_SEGMENT_PREFIX}${index}`
}

/**
 * 由节点名反解段下标；**非分段节点返回 `null`**（根节点 `vehicle`、未来的部件名）。
 *
 * `seg-` 后必须是纯十进制非负整数：`seg-1.5` / `seg--1` / `seg-x` / `seg-` 一律 `null`。
 * ⚠ 宁可不动，也不能把名字解析歪到别的段上——那会隐藏**相邻的另一段**。
 */
export function segmentIndexFromName(name: string): number | null {
  if (!name.startsWith(GLB_SEGMENT_PREFIX)) return null
  const tail = name.slice(GLB_SEGMENT_PREFIX.length)
  return /^\d+$/.test(tail) ? Number(tail) : null
}

/**
 * 求某个对象所属的段下标：自身命名优先，否则**向上**找最近的分段祖先。
 *
 * 需要向上找是因为 glTF 的 mesh 含多个 primitive 时，`GLTFLoader` 会把该节点建成
 * 一个**组**（名为 `seg-<i>`），其子网格另起名字（`seg-<i>_0`、`_1`…）——子网格自身
 * 解析不出下标，但它的材质与可见性仍属于该段。
 */
export function resolveSegmentIndex(object: THREE.Object3D | null): number | null {
  let node: THREE.Object3D | null = object
  while (node !== null) {
    const index = segmentIndexFromName(node.name)
    if (index !== null) return index
    node = node.parent
  }
  return null
}

/** `userData` 里存放网格**原始材质**的键（高亮是替换而非就地改色，见 `applySceneViewState`）。 */
const ORIGINAL_MATERIAL = 'aeroforgeOriginalMaterial'

/** 取网格的原始材质；首次调用时记下当前材质，之后恒返回它（高亮可反复开关而不丢原材质）。 */
function originalMaterialOf(mesh: THREE.Mesh): THREE.Material | THREE.Material[] {
  const stored: unknown = mesh.userData[ORIGINAL_MATERIAL]
  if (stored === undefined) {
    mesh.userData[ORIGINAL_MATERIAL] = mesh.material
    return mesh.material
  }
  return stored as THREE.Material | THREE.Material[]
}

/** 把一个（或一组）材质的裁剪面同步为 `planes`；**平面数量变化必须重编译着色器**。 */
function syncClippingPlanes(
  material: THREE.Material | THREE.Material[],
  planes: readonly THREE.Plane[],
): void {
  const list = Array.isArray(material) ? material : [material]
  for (const item of list) {
    const count = item.clippingPlanes?.length ?? 0
    item.clippingPlanes = planes.length === 0 ? null : [...planes]
    // 数量变化会改写着色器里的 `numClippingPlanes` 宏，须重编译；
    // 仅挪动位置（数量不变）只是 uniform 更新，拖动剖切滑杆不会卡。
    if (count !== planes.length) item.needsUpdate = true
  }
}

/** 视口中与"看得见什么"有关的全部视图状态（全部来自 `store/view`，均为纯视图量）。 */
export interface SceneViewState {
  /** 被隐藏的母线段下标（§11.4 分级显隐） */
  hidden: ReadonlySet<number>
  /** 选中的段下标（组件树 ↔ 视口双向联动）；`null` = 无选中 */
  selected: number | null
  /** 选中段使用的高亮材质；`null` = 不做高亮（只做显隐与剖切） */
  highlight: THREE.Material | null
  /** 剖切平面（§11.4 剖切）；空数组 = 不剖切 */
  clipPlanes: readonly THREE.Plane[]
  /**
   * 爆炸视图（§11.4 / OI-19 M5）；`null` = 不施加（如尚无后端报告）。
   * 位移是视图偏移、不可导出（§11.4 共同红线）。
   */
  explode: ExplodeViewState | null
}

/**
 * 把视图状态施加到场景（权威 GLB 与示意网格共用这一条路径）。
 *
 * 返回**实际命中的段节点数**——供门禁断言"节点名真的存在"，从而把
 * "找不到就静默不隐藏"这种失效变成可见的数字（§11.4 硬约束 1）。
 */
export function applySceneViewState(root: THREE.Object3D, state: SceneViewState): number {
  let nodes = 0
  root.traverse((object) => {
    const own = segmentIndexFromName(object.name)
    if (own !== null) {
      nodes += 1
      // 显隐施加在**具名节点**上：glTF 的节点旋转/平移就在这一级，改子网格会漏掉变换
      object.visible = !state.hidden.has(own)
    }
    if (!(object instanceof THREE.Mesh)) return

    const index = own ?? resolveSegmentIndex(object.parent)
    // ⚠ 段下标解析失败（`null`）时**不动材质**：非分段网格不参与显隐与高亮，但裁剪照做
    const original = originalMaterialOf(object)
    // 高亮用**替换材质**而非就地改 emissive：GLB 各段很可能引用**同一个**材质实例，
    // 就地改色会把所有段一起点亮（本工程实测导出为单一材质）
    object.material =
      index !== null && index === state.selected && state.highlight !== null
        ? state.highlight
        : original
    syncClippingPlanes(object.material, state.clipPlanes)
  })
  // 爆炸视图（§11.4 / OI-19 M5）：按装配树分区枚举沿世界 Y 轴分离——视图偏移，
  // 不改几何、不写回参数（`factor = 0` 即还原常态位置）
  if (state.explode !== null) applyExplodeView(root, state.explode)
  return nodes
}

/**
 * 还原被高亮**替换**过的材质（释放场景前必须先做这一步）。
 *
 * `applySceneViewState` 的高亮是材质替换；若不先还原就释放，会把视图层**共享**的那一个
 * 高亮材质当作本场景的材质释放掉，重建后复用它又要重新编译（且日志里看不出异常）。
 */
export function restoreOriginalMaterials(root: THREE.Object3D): void {
  root.traverse((object) => {
    if (!(object instanceof THREE.Mesh)) return
    const stored: unknown = object.userData[ORIGINAL_MATERIAL]
    if (stored === undefined) return
    object.material = stored as THREE.Material | THREE.Material[]
  })
}

/** 剖切所需的箭体外廓尺度——**全部取自后端报告**，前端不推导（ADR-011）。 */
export interface ClipExtent {
  /** 轴向总长（米），来自 `ValidationReport.total_length` */
  length: number
  /** 最大半径（米），来自 `ValidationReport.max_radius` */
  radius: number
}

/**
 * 构造单个剖切平面（§11.4 剖切）。
 *
 * three 只保留 `dot(normal, p) + constant ≥ 0` 的一侧，故法向取**负**方向表示"保留下侧"：
 * - 轴向：法向 −Y，`constant = t·L`，即保留 `y ≤ t·L`（两通道都是 Y-up，见 `orientation.ts`）；
 * - 径向：法向 −X，`constant = (2t−1)·R`，即平面自 −R 扫到 +R 的过轴平面。
 *
 * `position` 会被夹到 0–1：滑杆越界不该产生"整个模型消失"这种无从解释的画面。
 */
export function buildClippingPlane(
  axis: ClipAxis,
  position: number,
  extent: ClipExtent,
): THREE.Plane {
  const t = Math.min(1, Math.max(0, position))
  if (axis === 'axial') {
    return new THREE.Plane(new THREE.Vector3(0, -1, 0), t * extent.length)
  }
  return new THREE.Plane(new THREE.Vector3(-1, 0, 0), (2 * t - 1) * extent.radius)
}
