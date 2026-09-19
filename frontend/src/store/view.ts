import { create } from 'zustand'

/** 视口亮度滑杆的取值范围（规格 §11.3 FR-08：0.2 ～ 2.0，默认 1.0）。 */
export const LIGHT_INTENSITY_MIN = 0.2
export const LIGHT_INTENSITY_MAX = 2.0

interface ViewState {
  /** 视口亮度；**只影响视觉，不得进入任何计算或导出**（§11.3）。 */
  lightIntensity: number
  showGrid: boolean
  autoRotate: boolean
  wireframe: boolean
  setLightIntensity: (value: number) => void
  toggleGrid: () => void
  toggleAutoRotate: () => void
  toggleWireframe: () => void
  reset: () => void
}

/**
 * 视图偏好（规格 §11.6 `store/view`）。
 *
 * 规则：本 store 的任何变化**不得触发后端请求**——改主题、拉亮度都不重算。
 */
export const useViewStore = create<ViewState>((set) => ({
  lightIntensity: 1.0,
  showGrid: true,
  autoRotate: false,
  wireframe: false,

  setLightIntensity: (value) =>
    set({ lightIntensity: Math.min(LIGHT_INTENSITY_MAX, Math.max(LIGHT_INTENSITY_MIN, value)) }),

  toggleGrid: () => set((state) => ({ showGrid: !state.showGrid })),
  toggleAutoRotate: () => set((state) => ({ autoRotate: !state.autoRotate })),
  toggleWireframe: () => set((state) => ({ wireframe: !state.wireframe })),

  reset: () =>
    set({ lightIntensity: 1.0, showGrid: true, autoRotate: false, wireframe: false }),
}))
