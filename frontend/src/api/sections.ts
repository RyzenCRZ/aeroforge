import { isRecord, postJson } from './client'
import type { components } from './schema'
import type { Vehicle } from './params'

/**
 * 2D 视图组分区的 API（规格 §11.10 / OI-37，`POST /api/geometry/sections`）。
 *
 * **类型来源**：后端 `/api/geometry/sections` 契约已合入 OpenAPI（`schema.d.ts` 由
 * `npm run gen:api` 生成），类型一律取 `components['schemas']` 导入（§18.2 禁止手写
 * 重复类型）；**运行时守卫保持不变**——守卫本就不依赖生成物，是对传输边界的独立校验。
 *
 * 本模块只做传输与**边界**校验，不含任何几何 / 物理计算（ADR-011）。
 */

type Schemas = components['schemas']

/** band `section` 枚举（契约定死）：缺失分区不在数组里，前端按名寻址渲染。 */
export const BAND_SECTIONS = [
  'fairing',
  'adapter',
  'avionics',
  'forward_skirt',
  'ox_tank',
  'intertank',
  'common_bulkhead',
  'fuel_tank',
  'thrust_structure',
  'engine_bay',
  'interstage',
] as const

export type BandSection = (typeof BAND_SECTIONS)[number]

/**
 * 一段轴向分区条带。所有数值一律由后端下发，前端只排版（ADR-011）：
 *
 * - `length_m`：条带高（剖面图段高 ∝ 此值）；
 * - `liquid_level_m`：液面高度（自箱底向上，§6.1 h_liq 口径）——只出现在贮箱条带上；
 * - `color_key`：推进剂编码色 token 名（§11.2，映射到既有 CSS 变量）；
 * - `bulkhead_saving_m`：共底级长缩减量（共底条带右侧标注）；
 * - `insulation`：隔热层（斜线填充，§5.9 口径 2，LH₂ 侧）。
 */
export type SectionBand = Schemas['SectionBand']

/** 一级（或一组助推器）的分区形态：`level = 0` 为助推器（与 GCAT 记法对齐，§7.3）。 */
export type SectionsStage = Schemas['StageSections']

export type SectionDimensionLabel = Schemas['DimensionLabel']

export type SectionsDimensions = Schemas['SectionDimensions']

export type SectionsResponse = Schemas['SectionsResponse']

function isSectionBand(value: unknown): value is SectionBand {
  if (!isRecord(value)) return false
  return (
    typeof value.section === 'string' &&
    (BAND_SECTIONS as readonly string[]).includes(value.section) &&
    typeof value.label_zh === 'string' &&
    typeof value.length_m === 'number' &&
    (value.liquid_level_m === undefined || typeof value.liquid_level_m === 'number') &&
    (value.propellant_oxidizer === undefined ||
      value.propellant_oxidizer === null ||
      typeof value.propellant_oxidizer === 'string') &&
    (value.propellant_fuel === undefined ||
      value.propellant_fuel === null ||
      typeof value.propellant_fuel === 'string') &&
    (value.color_key === undefined || value.color_key === null || typeof value.color_key === 'string') &&
    (value.bulkhead_saving_m === undefined || typeof value.bulkhead_saving_m === 'number') &&
    (value.insulation === undefined || typeof value.insulation === 'boolean')
  )
}

/** 级 / 助推器组同形态（契约：boosters 为「同 stage 形态」），共用一个守卫。 */
function isStageGroup(value: unknown): value is SectionsStage {
  if (!isRecord(value)) return false
  return (
    typeof value.stage_index === 'number' &&
    typeof value.level === 'number' &&
    Array.isArray(value.bands) &&
    value.bands.every(isSectionBand) &&
    (value.delivery_pipe_routing === 'external' || value.delivery_pipe_routing === 'internal') &&
    (value.tank_order === 'oxidizer_first' || value.tank_order === 'fuel_first')
  )
}

function isDimensionLabel(value: unknown): value is SectionDimensionLabel {
  if (!isRecord(value)) return false
  return typeof value.key === 'string' && typeof value.text === 'string'
}

function isSectionsResponse(value: unknown): value is SectionsResponse {
  if (!isRecord(value)) return false
  const dimensions: unknown = value.dimensions
  return (
    Array.isArray(value.stages) &&
    value.stages.every(isStageGroup) &&
    (value.boosters === undefined ||
      (Array.isArray(value.boosters) && value.boosters.every(isStageGroup))) &&
    isRecord(dimensions) &&
    typeof dimensions.total_length_m === 'number' &&
    typeof dimensions.max_diameter_m === 'number' &&
    (dimensions.fairing_diameter_m === null || typeof dimensions.fairing_diameter_m === 'number') &&
    Array.isArray(dimensions.labels) &&
    dimensions.labels.every(isDimensionLabel) &&
    Array.isArray(value.warnings) &&
    value.warnings.every((item) => typeof item === 'string') &&
    isRecord(value.provenance)
  )
}

/**
 * 请求整箭分区数据（2D 视图组的唯一数据来源，两图共用）。
 *
 * 请求体 = 当前 `Vehicle`；响应契约不符时抛 `CONTRACT_MISMATCH`（经 `./client`）。
 */
export function fetchSections(vehicle: Vehicle, signal?: AbortSignal): Promise<SectionsResponse> {
  return postJson('/api/geometry/sections', vehicle, isSectionsResponse, signal)
}
