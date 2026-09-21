import * as THREE from 'three'

/**
 * 爆炸视图（规格 §11.4 / OI-19 的 M5 部分）。
 *
 * 纪律与显隐 / 剖切完全同款（§11.4「三者的共同红线」）：
 *
 * 1. **纯视图状态**——位移只写节点的 `position`（视图偏移），不改任何几何
 *    （`BufferGeometry` 原封不动）、不写回 `store/params`、不触发任何后端请求；
 * 2. **不可导出**——爆炸位移不得进入送往计算或导出的几何（P1 / ADR-012）；
 * 3. **取分区枚举，不硬编码件名**——分组与顺序全部从场景根 children 的**实际节点名
 *    与实际轴向位置**推导；件名清单（`s1` / `ox-tank`…）换任何名字机制都不动。
 *
 * 分组契约（§11.4 契约表「具名节点是唯一的可分级机制」）：
 *
 * - 装配树命名 `s1-ox-tank` / `s2-fuel-tank` / `b0-fin`…——**级（sN）/ 助推器（bN）
 *   前缀 = 组键**，组内节点共享同一轴向偏移（相对位置不变）；
 * - 无级前缀的具名节点（如过渡期的 `seg-<i>`）**各自成组**——机制与显隐同构，
 *   后端把节点名换成分区名后无需改这里一行；
 * - 空名节点不参与（与显隐的硬约束 1 同源：按名寻址，无名即不可寻址）。
 */

/** 组间分离量占模型总高的比例：factor=1 时相邻组间隔 35% 箭高，足够看清内部构型。 */
export const EXPLODE_GAP_RATIO = 0.35

/** `userData` 里记录节点**常态位置**的键（爆炸是视图偏移，复位即还原，§11.4）。 */
const REST_POSITION = 'aeroforgeRestPosition'

/**
 * 级 / 助推器前缀：`s`/`b` + 十进制数字，后接分隔符（`-` / `/` / `.`）+ 部件名，
 * 或前缀即全名（装配树按级建组时）。其余具名节点一律各自成组。
 */
const STAGE_PREFIX_PATTERN = /^([sb]\d+)(?:[-/].+)?$/

/**
 * 求节点名对应的爆炸组键；**空名返回 `null`**（不参与爆炸，也不报错）。
 *
 * ⚠ 只认前缀模式，不认件名——`ox-tank` 不会单独成组，它跟着所属级 `s1` 走
 * （组内保持相对位置正是本契约的要求）。
 */
export function explodeGroupKey(name: string): string | null {
  if (name === '') return null
  const match = STAGE_PREFIX_PATTERN.exec(name)
  return match === null ? name : match[1]
}

/** 一个爆炸分组：组键 + 组内节点（均为场景根的直接 children）。 */
export interface ExplodeGroup {
  key: string
  nodes: THREE.Object3D[]
}

/**
 * 枚举场景根 children 的爆炸分组（按首次出现顺序；施加时会再按轴向位置重排）。
 *
 * 组数完全由**实际节点名**决定——本函数不含任何件名或数量假设。
 */
export function collectExplodeGroups(root: THREE.Object3D): ExplodeGroup[] {
  const byKey = new Map<string, ExplodeGroup>()
  for (const child of root.children) {
    const key = explodeGroupKey(child.name)
    if (key === null) continue
    const existing = byKey.get(key)
    if (existing === undefined) byKey.set(key, { key, nodes: [child] })
    else existing.nodes.push(child)
  }
  return [...byKey.values()]
}

/** 爆炸视图状态（挂在 `SceneViewState.explode` 上，两通道同一份）。 */
export interface ExplodeViewState {
  /** 0 = 常态，1 = 完全分离；纯视图量（不得写回 `store/params`，§11.4）。 */
  factor: number
  /**
   * 轴向特征长度（米）：取后端报告的 `total_length`——位移尺度不前端推导（ADR-011），
   * 与剖切的 `ClipExtent` 同一来源口径。
   */
  length: number
}

const WORLD_Y = new THREE.Vector3(0, 1, 0)

/**
 * 求世界 +Y 方向在某节点**父坐标系**里的方向向量。
 *
 * 不能想当然写 `position.y += offset`：GLB 场景图自带 `Rx(-90°)`（Z-up → Y-up，
 * 见 `orientation.ts`），根 children 的局部 **Z** 才对应世界 **Y**；示意通道的
 * 包装组则是恒等变换（局部 Y 即世界 Y）。按父矩阵反解方向，两种形态同一条代码。
 */
function worldYInParentSpace(node: THREE.Object3D): THREE.Vector3 {
  const parent = node.parent
  if (parent === null) return WORLD_Y.clone()
  parent.updateWorldMatrix(true, false)
  const inverse = parent.matrixWorld.clone().invert()
  return WORLD_Y.clone().transformDirection(inverse).normalize()
}

/**
 * 把爆炸视图施加到场景根（权威 GLB 与示意网格共用这一条路径）。
 *
 * - 组序 = 按**常态**轴向位置自尾向头排序（§5.9 分区枚举的轴向后序——由实际
 *   装配位置推导，不硬编码件名）；
 * - 组 `i` 的全部节点沿世界 +Y 平移 `i × factor × EXPLODE_GAP_RATIO × length`
 *   （位移量 ∝ 组序 × factor × 特征长度；组内同偏移 → 相对位置不变）；
 * - 幂等：每次先复位到记录的常态位置再重算——反复拖滑杆不会累积位移，
 *   `factor = 0` 即完全还原。
 *
 * @returns 实际命中的分组数（供门禁断言"节点名真的存在"，与显隐的返回值同语义）
 */
export function applyExplodeView(root: THREE.Object3D, view: ExplodeViewState): number {
  const groups = collectExplodeGroups(root)
  if (groups.length === 0) return 0

  // 1) 复位到常态位置：首施时记录，之后每次从常态重算（排序基准不受上一次位移影响）
  for (const group of groups) {
    for (const node of group.nodes) {
      const rest: unknown = node.userData[REST_POSITION]
      if (rest === undefined) {
        node.userData[REST_POSITION] = node.position.clone()
      } else {
        node.position.copy(rest as THREE.Vector3)
      }
    }
  }

  // 2) 轴向排序：按组内节点常态世界 Y 的均值升序（自尾向头）
  root.updateWorldMatrix(true, true)
  const meanWorldY = (group: ExplodeGroup): number => {
    let sum = 0
    for (const node of group.nodes) sum += node.getWorldPosition(new THREE.Vector3()).y
    return sum / group.nodes.length
  }
  groups.sort((left, right) => meanWorldY(left) - meanWorldY(right))

  // 3) 施加位移（factor 夹到 0–1：滑杆越界不该产生无从解释的画面，与剖切同口径）
  const factor = Math.min(1, Math.max(0, view.factor))
  const step = view.length * EXPLODE_GAP_RATIO
  groups.forEach((group, index) => {
    const offset = index * factor * step
    if (offset === 0) return
    for (const node of group.nodes) {
      node.position.addScaledVector(worldYInParentSpace(node), offset)
    }
  })
  return groups.length
}
