import { create } from 'zustand'

import type { MeridianProfile, MeridianSegment, SegmentType } from '../api/geometry'

/**
 * 首次渲染的默认剖面：下球底 + 柱段 + 上球底（胶囊体），解析包络 (2, 2, 5) m。
 *
 * 合法性自检（后端 ``resolve`` 的穹顶判据：arc / ellipse 段两端中**恰有一端** r=0）：
 *   段 0 `arc(1.0 → 1.0)`：起点 r = base_radius = 0（在轴线上），终点 r = 1.0 ✓
 *   段 1 `line(3.0 → 1.0)`：自 r=1 到 r=1，是标准柱段 ✓
 *   段 2 `arc(1.0 → 0.0)`：起点 r = 1.0，终点 r = 0（在轴线上）✓
 * 与后端集成测试的 ``_CAPSULE`` 构型同形（`base_radius: 0` + 自轴线张开的下穹顶）。
 *
 * ⚠ 剖面数值一律以米为单位，回转轴 = Z 轴；前端不推导任何几何量（ADR-011）。
 */
export const DEFAULT_PROFILE: MeridianProfile = {
  name: '默认剖面（胶囊体）',
  base_radius: 0.0,
  segments: [
    { type: 'arc', length: 1.0, end_radius: 1.0 },
    { type: 'line', length: 3.0, end_radius: 1.0 },
    { type: 'arc', length: 1.0, end_radius: 0.0 },
  ],
}

/** 可编辑的段字段（`type` 参与判别式，故单独列出以避免 Partial 联合类型的赋值问题）。 */
export interface SegmentPatch {
  type?: SegmentType
  length?: number
  end_radius?: number
}

/**
 * 母线编辑器的草稿形态。
 *
 * 与 `MeridianProfile` 同构但字段命名不同，用于「编辑 → 剖面 → 编辑」的往返一致单测
 * （§16.3 验收项 2 的前端侧）。
 */
export interface EditorDraft {
  name: string
  baseRadius: number
  segments: MeridianSegment[]
}

/** 剖面 → 编辑器草稿（逐段浅拷贝，避免两处共享同一对象）。 */
export function editorFromProfile(profile: MeridianProfile): EditorDraft {
  return {
    name: profile.name,
    baseRadius: profile.base_radius,
    segments: profile.segments.map((segment) => ({ ...segment })),
  }
}

/** 编辑器草稿 → 剖面。与 `editorFromProfile` 互逆。 */
export function profileFromEditor(draft: EditorDraft): MeridianProfile {
  return {
    name: draft.name,
    base_radius: draft.baseRadius,
    segments: draft.segments.map((segment) => ({ ...segment })),
  }
}

/** 段链末端的半径；无段时即 `base_radius`（段与段首尾相接，故声明值就是实际值）。 */
export function chainEndRadius(profile: MeridianProfile): number {
  const last = profile.segments[profile.segments.length - 1]
  return last === undefined ? profile.base_radius : last.end_radius
}

/**
 * 是否允许在段链末尾追加新段。
 *
 * 末端半径已为 0（剖面已闭合到轴线）时禁止追加：任何后续段都会从 r=0 起步，
 * 要么立即违反穹顶判据，要么退化出零半径的柱段。
 */
export function canAppendSegment(profile: MeridianProfile): boolean {
  return chainEndRadius(profile) > 0
}

/** 新段的默认值：保证追加后整条剖面仍然合法（不凭空造出非法剖面）。 */
function defaultSegment(type: SegmentType, startRadius: number): MeridianSegment {
  switch (type) {
    case 'line':
      // 柱段：沿当前末端半径延伸，不改变轮廓半径，恒为合法
      return { type: 'line', length: 1, end_radius: startRadius }
    case 'arc':
    case 'ellipse':
      // 穹顶段：自当前末端半径收拢到轴线（穹顶要求恰有一端 r=0，此处即终点）
      return { type, length: 1, end_radius: 0 }
  }
}

function mergeSegment(segment: MeridianSegment, patch: SegmentPatch): MeridianSegment {
  switch (segment.type) {
    case 'line':
      return { ...segment, ...patch, type: 'line' }
    case 'arc':
      return { ...segment, ...patch, type: 'arc' }
    case 'ellipse':
      return { ...segment, ...patch, type: 'ellipse' }
  }
}

function cloneProfile(profile: MeridianProfile): MeridianProfile {
  return { ...profile, segments: profile.segments.map((segment) => ({ ...segment })) }
}

interface ParamsState {
  /** 母线草稿（用户参数的唯一可写来源，规格 §11.6 SSOT）。 */
  profile: MeridianProfile
  setBaseRadius: (radius: number) => void
  updateSegment: (index: number, patch: SegmentPatch) => void
  addSegment: (type: SegmentType) => void
  removeSegment: (index: number) => void
  setProfile: (profile: MeridianProfile) => void
  reset: () => void
}

/** 母线草稿状态（zustand）。 */
export const useParamsStore = create<ParamsState>((set) => ({
  profile: cloneProfile(DEFAULT_PROFILE),

  setBaseRadius: (radius) =>
    set((state) => ({ profile: { ...state.profile, base_radius: radius } })),

  updateSegment: (index, patch) =>
    set((state) => ({
      profile: {
        ...state.profile,
        segments: state.profile.segments.map((segment, position) =>
          position === index ? mergeSegment(segment, patch) : segment,
        ),
      },
    })),

  addSegment: (type) =>
    set((state) => {
      if (!canAppendSegment(state.profile)) return state
      const segment = defaultSegment(type, chainEndRadius(state.profile))
      return { profile: { ...state.profile, segments: [...state.profile.segments, segment] } }
    }),

  removeSegment: (index) =>
    set((state) => ({
      profile: {
        ...state.profile,
        // 段数下限为 1（后端 min_length=1），故不允许删空
        segments:
          state.profile.segments.length <= 1
            ? state.profile.segments
            : state.profile.segments.filter((_segment, position) => position !== index),
      },
    })),

  setProfile: (profile) => set({ profile: cloneProfile(profile) }),

  reset: () => set({ profile: cloneProfile(DEFAULT_PROFILE) }),
}))
