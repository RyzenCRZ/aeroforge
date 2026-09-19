import { getJson, isRecord } from './client'
import type { components } from './schema'

/**
 * 材料目录域 API（规格 §7.4 / QA-3 材料引用化）。
 *
 * - `GET /api/catalog/materials` —— 材料库全量（含派生比强度 / 比刚度计算值）
 *
 * 类型一律取自 OpenAPI 生成物（§18.2 禁止手写重复类型）；本模块只做传输与**边界**
 * 校验，不含任何物理计算（ADR-011）。守卫写法与 `templates.ts` / `params.ts` 同形态。
 */

export type MaterialEntry = components['schemas']['MaterialOut']
export type MaterialsResponse = components['schemas']['MaterialsResponse']

/**
 * 单条材料记录的运行时收窄。结构存在即可——物理合法性（强度为正等）由后端
 * 产品校验器负责；`quality` 逐字校验是因为 UI 据它渲染「[典型值]」标注（§1.4-4）。
 */
function isMaterialEntry(value: unknown): value is MaterialEntry {
  if (!isRecord(value)) return false
  return (
    typeof value.id === 'string' &&
    typeof value.name === 'string' &&
    typeof value.category === 'string' &&
    typeof value.density_kg_m3 === 'number' &&
    typeof value.elastic_modulus_pa === 'number' &&
    typeof value.yield_strength_pa === 'number' &&
    typeof value.service_temp_min_c === 'number' &&
    typeof value.service_temp_max_c === 'number' &&
    typeof value.typical_min_wall_thickness_m === 'number' &&
    typeof value.heat_treatment === 'string' &&
    (value.source === null || typeof value.source === 'string') &&
    (value.quality === 'literature' || value.quality === 'typical') &&
    typeof value.specific_strength_m2_s2 === 'number' &&
    typeof value.specific_stiffness_m2_s2 === 'number'
  )
}

function isMaterialsResponse(value: unknown): value is MaterialsResponse {
  if (!isRecord(value)) return false
  return Array.isArray(value.materials) && value.materials.every(isMaterialEntry)
}

/** 材料库全量（QA-3：`material` 字段的值域 = 库内材料 id）。 */
export function fetchMaterials(): Promise<MaterialsResponse> {
  return getJson('/api/catalog/materials', isMaterialsResponse)
}
