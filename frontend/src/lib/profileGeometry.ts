import type { SectionBand, SectionsResponse, SectionsStage } from '../api/sections'

/**
 * 2D 视图组（外观图 + 工程剖面图）的坐标映射与版面排版（规格 §11.10 / OI-37）。
 *
 * ⚠ **纯渲染纪律（ADR-011）**：本模块只做**线性排版映射**——把后端下发的米制数值
 * 换算成 SVG 坐标，不含任何物理公式（不推导长度 / 直径 / 液面 / 任何几何量）。
 * 坐标映射是**排版**不是物理，具体口径（§11.10）：
 *
 * - 纵向按整箭总长归一：z = 0 在头部顶点、向下为正（§4.3 坐标系）；
 * - 横向按最大直径归一；双向**等比**（两轴每米 SVG 单位数相同），绝不拉伸失真；
 * - 总长或直径变化 → `scale` 整体重算（映射随数据自适应，不存在"拉伸"路径）。
 */

/** 绘图区版面（SVG 视口内的固定留白约定；单位 = SVG 用户单位，与物理量无关）。 */
export interface Frame {
  width: number
  height: number
  /** 左留白：尺寸线与标注文字。 */
  padLeft: number
  /** 右留白：引线标注文字。 */
  padRight: number
  padTop: number
  padBottom: number
  /** 右侧助推器列车道宽（并联构型 OI-36）。 */
  boosterLane: number
}

/** 等比坐标映射结果。`scale` 对横纵两轴同值（等比），单位 = SVG 单位 / 米。 */
export interface AxisMapping {
  scale: number
  /** 芯级轴线的 SVG x。 */
  axisX: number
  /** 头部顶点（z = 0）的 SVG y。 */
  noseY: number
  bodyHalfW: number
  bodyLeft: number
  bodyRight: number
}

export function createAxisMapping(
  totalLengthM: number,
  maxDiameterM: number,
  frame: Frame,
): AxisMapping | null {
  if (!(totalLengthM > 0) || !(maxDiameterM > 0)) return null
  const availW = frame.width - frame.padLeft - frame.padRight - frame.boosterLane
  const availH = frame.height - frame.padTop - frame.padBottom
  // 双向等比：两轴共用同一个 scale（取先触边的那条约束）
  const scale = Math.min(availW / maxDiameterM, availH / totalLengthM)
  const bodyHalfW = (scale * maxDiameterM) / 2
  const axisX = frame.padLeft + availW / 2
  return {
    scale,
    axisX,
    noseY: frame.padTop,
    bodyHalfW,
    bodyLeft: axisX - bodyHalfW,
    bodyRight: axisX + bodyHalfW,
  }
}

/** 助推器列宽 = 芯级宽的固定绘制比例（契约未含助推器直径——呈现约定，非物理）。 */
export const BOOSTER_WIDTH_RATIO = 0.6

/** 助推器列之间的间距（SVG 单位）。 */
export const BOOSTER_GAP = 8

export function boosterHalfW(coreHalfW: number): number {
  return coreHalfW * BOOSTER_WIDTH_RATIO
}

/** 第 index 组助推器的轴线 x：紧贴芯级右侧自左向右排开（呈现约定）。 */
export function boosterAxisX(coreBodyRight: number, coreHalfW: number, index: number): number {
  const halfW = boosterHalfW(coreHalfW)
  return coreBodyRight + BOOSTER_GAP + halfW + index * (halfW * 2 + BOOSTER_GAP)
}

/**
 * 助推器列的纵向位置：底边与整箭底边对齐。
 *
 * 契约未含助推器的轴向站位，底对齐是**呈现约定**（并联助推器贴着芯级底部），
 * 输入全部是后端数值的线性映射。
 */
export function boosterTopY(
  totalLengthM: number,
  boosterLengthSumM: number,
  noseY: number,
  scale: number,
): number {
  return noseY + Math.max(0, totalLengthM - boosterLengthSumM) * scale
}

/** 条带米制长度求和（助推器列底对齐排版用）。 */
export function bandsLengthSum(bands: readonly SectionBand[]): number {
  return bands.reduce((sum, band) => sum + band.length_m, 0)
}

export interface LiquidLayout {
  /** 液体区高（SVG 单位）：占条带底部。 */
  liquid: number
  /** 气枕区高（SVG 单位）：条带顶部其余部分。 */
  ullage: number
}

/**
 * 液面排版：液体占条带**底部** `liquid_level_m`（液面高度口径 = 自箱底向上，
 * §6.1 派生式 `h_liq = 加注比例 × 可装高度`），其余为气枕。
 * 截到 [0, 条带长] 只是防溢出的排版截断，不是物理修正。
 */
export function liquidLayout(bandLengthM: number, liquidLevelM: number, scale: number): LiquidLayout {
  const bandH = bandLengthM * scale
  const liquidM = Math.min(Math.max(liquidLevelM, 0), bandLengthM)
  const liquid = liquidM * scale
  return { liquid, ullage: bandH - liquid }
}

export type PipeRouting = SectionsStage['delivery_pipe_routing']

/** 一条条带在 SVG 里的排版盒（y = 条带顶，h = 条带高，均为线性映射结果）。 */
export interface LayoutBand {
  band: SectionBand
  /** 全局自上而下序号（跨级 / 跨助推器连续），供 data-testid 与顺序断言。 */
  index: number
  y: number
  h: number
  axisX: number
  halfW: number
}

/** 一级（或一组助推器）的排版列。 */
export interface LayoutGroup {
  key: string
  isBooster: boolean
  routing: PipeRouting
  axisX: number
  halfW: number
  topY: number
  bottomY: number
  bands: LayoutBand[]
}

export interface SectionsLayout {
  mapping: AxisMapping
  groups: LayoutGroup[]
  /** 芯级列（自上而下）；助推器列不参与「级段分界线」计数。 */
  coreGroups: LayoutGroup[]
}

/**
 * 把后端分区数据排进绘图区（纯排版，无物理）。
 *
 * - 芯级自上而下 = `level` 降序（§6.1 记法：level 1 = 底级）；
 * - 级内条带按后端数组顺序自上而下渲染——储箱排列（tank_order）由后端排好，前端
 *   只读数组顺序，**不硬编码**「氧箱在上」（§5.9 非铁律）；
 * - 缺失分区本就不在数组里，消费端按名寻址渲染（section 枚举）；
 * - 助推器（level 0）排在芯级右侧车道，底边与整箭底对齐（呈现约定，见 `boosterTopY`）。
 */
export function buildSectionsLayout(data: SectionsResponse, frame: Frame): SectionsLayout | null {
  const mapping = createAxisMapping(data.dimensions.total_length_m, data.dimensions.max_diameter_m, frame)
  if (mapping === null) return null

  const groups: LayoutGroup[] = []
  let bandIndex = 0

  const stages = [...data.stages].sort((a, b) => b.level - a.level)
  let z = 0
  for (const stage of stages) {
    const topY = mapping.noseY + z * mapping.scale
    const bands = stage.bands.map((band): LayoutBand => {
      const box: LayoutBand = {
        band,
        index: bandIndex++,
        y: mapping.noseY + z * mapping.scale,
        h: band.length_m * mapping.scale,
        axisX: mapping.axisX,
        halfW: mapping.bodyHalfW,
      }
      z += band.length_m
      return box
    })
    groups.push({
      key: `stage-${stage.stage_index}`,
      isBooster: false,
      routing: stage.delivery_pipe_routing,
      axisX: mapping.axisX,
      halfW: mapping.bodyHalfW,
      topY,
      bottomY: mapping.noseY + z * mapping.scale,
      bands,
    })
  }

  const boosters = data.boosters ?? []
  for (const [j, booster] of boosters.entries()) {
    const axisX = boosterAxisX(mapping.bodyRight, mapping.bodyHalfW, j)
    const halfW = boosterHalfW(mapping.bodyHalfW)
    const topY = boosterTopY(
      data.dimensions.total_length_m,
      bandsLengthSum(booster.bands),
      mapping.noseY,
      mapping.scale,
    )
    let zBooster = 0
    const bands = booster.bands.map((band): LayoutBand => {
      const box: LayoutBand = {
        band,
        index: bandIndex++,
        y: topY + zBooster * mapping.scale,
        h: band.length_m * mapping.scale,
        axisX,
        halfW,
      }
      zBooster += band.length_m
      return box
    })
    groups.push({
      key: `booster-${j}`,
      isBooster: true,
      routing: booster.delivery_pipe_routing,
      axisX,
      halfW,
      topY,
      bottomY: topY + zBooster * mapping.scale,
      bands,
    })
  }

  return { mapping, groups, coreGroups: groups.filter((group) => !group.isBooster) }
}

/** 缩放档位边界（§11.10：0.5×～4×，复位回 1×）。 */
export const ZOOM_MIN = 0.5
export const ZOOM_MAX = 4

/** 每按一次 [＋] / [－] 的等比步长。 */
export const ZOOM_STEP = 1.25

export interface ViewBox {
  x: number
  y: number
  w: number
  h: number
}

export function clampZoom(zoom: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom))
}

/** 缩放只改 SVG 视口（以绘图区中心为中心收缩 / 扩张），不改映射基准（§11.10）。 */
export function zoomViewBox(frame: Frame, zoom: number): ViewBox {
  const z = clampZoom(zoom)
  const w = frame.width / z
  const h = frame.height / z
  return { x: (frame.width - w) / 2, y: (frame.height - h) / 2, w, h }
}

export function viewBoxText(viewBox: ViewBox): string {
  return `${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`
}
