import { create } from 'zustand'

/** 视口亮度滑杆的取值范围（规格 §11.3 FR-08：0.2 ～ 2.0，默认 1.0）。 */
export const LIGHT_INTENSITY_MIN = 0.2
export const LIGHT_INTENSITY_MAX = 2.0

/** 剖切平面的法向（规格 §11.4）：沿箭体**轴向**，或**径向**（过轴的半平面）。 */
export type ClipAxis = 'axial' | 'radial'

/** 剖切状态（§11.4「剖切」：单个可移动平面，纯渲染裁剪，不重建几何、不可导出）。 */
export interface ClipState {
  enabled: boolean
  axis: ClipAxis
  /** 归一化位置 0–1：轴向 = 自底端到顶端；径向 = 自 −R 到 +R。纯视图量。 */
  position: number
}

interface ViewState {
  /** 视口亮度；**只影响视觉，不得进入任何计算或导出**（§11.3）。 */
  lightIntensity: number
  showGrid: boolean
  autoRotate: boolean
  wireframe: boolean
  /**
   * 被隐藏的**母线段**下标集合（§11.4 分级显隐）。
   *
   * 粒度 = `profile.segments` 的 0 基下标，即 GLB 节点名 `seg-<i>` 的 `<i>`，
   * 与参数层 `stages[i]`（"第几级"）**无关**，两者不得互相推演（§11.4 契约表）。
   * 对不存在的段（退化段不产出节点）隐藏即无操作——**不得报错**。
   */
  hiddenSegments: ReadonlySet<number>
  /** 组件树 ↔ 视口**共用**的选中段（双向联动：任一处的选择都写这里）。 */
  selectedSegment: number | null
  clip: ClipState
  /**
   * 爆炸因子（§11.4 / OI-19 M5）：0 = 常态。施加逻辑见 `r3f/explode.ts`
   * （位移是视图偏移，非几何变更；不可导出）。
   */
  explodeFactor: number
  setLightIntensity: (value: number) => void
  toggleGrid: () => void
  toggleAutoRotate: () => void
  toggleWireframe: () => void
  setSegmentHidden: (index: number, hidden: boolean) => void
  toggleSegmentHidden: (index: number) => void
  showAllSegments: () => void
  selectSegment: (index: number | null) => void
  setClipEnabled: (enabled: boolean) => void
  setClipAxis: (axis: ClipAxis) => void
  setClipPosition: (position: number) => void
  setExplodeFactor: (value: number) => void
  resetExplode: () => void
  reset: () => void
}

/** 默认剖切状态：关闭、轴向、居中。 */
export const DEFAULT_CLIP: ClipState = { enabled: false, axis: 'axial', position: 0.5 }

/** 空隐藏集：只读且在写入时总是换新实例，故可安全共享。 */
const NO_HIDDEN: ReadonlySet<number> = new Set<number>()

/**
 * 视图偏好（规格 §11.6 `store/view`）。
 *
 * 规则：本 store 的任何变化**不得触发后端请求**——改主题、拉亮度、切显隐、拖剖切都不重算。
 * 显隐与剖切是**纯视图状态**（§11.4）：不得写回 `store/params`，不得改变任何几何或计算结果。
 */
export const useViewStore = create<ViewState>((set) => ({
  lightIntensity: 1.0,
  showGrid: true,
  autoRotate: false,
  wireframe: false,
  hiddenSegments: NO_HIDDEN,
  selectedSegment: null,
  clip: DEFAULT_CLIP,
  explodeFactor: 0,

  setLightIntensity: (value) =>
    set({ lightIntensity: Math.min(LIGHT_INTENSITY_MAX, Math.max(LIGHT_INTENSITY_MIN, value)) }),

  toggleGrid: () => set((state) => ({ showGrid: !state.showGrid })),
  toggleAutoRotate: () => set((state) => ({ autoRotate: !state.autoRotate })),
  toggleWireframe: () => set((state) => ({ wireframe: !state.wireframe })),

  setSegmentHidden: (index, hidden) =>
    set((state) => {
      const already = state.hiddenSegments.has(index)
      if (already === hidden) return state
      const next = new Set(state.hiddenSegments)
      if (hidden) next.add(index)
      else next.delete(index)
      return { hiddenSegments: next }
    }),

  toggleSegmentHidden: (index) =>
    set((state) => {
      const next = new Set(state.hiddenSegments)
      if (!next.delete(index)) next.add(index)
      return { hiddenSegments: next }
    }),

  showAllSegments: () => set({ hiddenSegments: NO_HIDDEN }),

  selectSegment: (index) => set({ selectedSegment: index }),

  setClipEnabled: (enabled) =>
    set((state) => {
      // 互斥的另一侧（§11.4）：开剖切即复位爆炸——两个轴向视图状态不得并存
      if (enabled && state.explodeFactor > 0) {
        return { clip: { ...state.clip, enabled }, explodeFactor: 0 }
      }
      return { clip: { ...state.clip, enabled } }
    }),

  setClipAxis: (axis) => set((state) => ({ clip: { ...state.clip, axis } })),

  setClipPosition: (position) =>
    set((state) => ({ clip: { ...state.clip, position: Math.min(1, Math.max(0, position)) } })),

  setExplodeFactor: (value) =>
    set((state) => {
      const factor = Math.min(1, Math.max(0, value))
      // 互斥（§11.4）：爆炸激活即关剖切——UI 侧另行给出冲突提示（视口工具条）
      if (factor > 0 && state.clip.enabled) {
        return { explodeFactor: factor, clip: { ...state.clip, enabled: false } }
      }
      return { explodeFactor: factor }
    }),

  resetExplode: () => set({ explodeFactor: 0 }),

  reset: () =>
    set({
      lightIntensity: 1.0,
      showGrid: true,
      autoRotate: false,
      wireframe: false,
      hiddenSegments: NO_HIDDEN,
      selectedSegment: null,
      clip: DEFAULT_CLIP,
      explodeFactor: 0,
    }),
}))
