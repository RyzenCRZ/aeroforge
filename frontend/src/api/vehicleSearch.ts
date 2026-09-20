import { getJson, isRecord } from './client'
import type { components } from './schema'

/**
 * 型号检索域 API（OI-39，§7.7 / §10.1）。
 *
 * - `GET /api/catalog/vehicles/search?name=` —— GCAT lv 全库规范化子串检索
 *   （内置精校模板的置顶与「精校」标注由**前端**做，后端不掺合模板逻辑）。
 * - `GET /api/catalog/vehicles/record?name=&variant=` —— 型号已知参数集
 *   （逐字段带出处；GCAT 缺失的字段值为 null 且列入 `missing`，禁止编造）。
 *
 * 类型一律取自 OpenAPI 生成物（§18.2 禁止手写重复类型）；本模块只做传输与
 * **边界**校验，不含任何物理或几何计算（ADR-011）。守卫写法与 `templates.ts`
 * 的 `isTemplateResponse` 同形态。
 */

export type VehicleSearchHit = components['schemas']['VehicleSearchHitOut']
export type VehicleSearchResponse = components['schemas']['VehicleSearchResponse']
export type SourcedField = components['schemas']['SourcedField']
export type VehicleRecordResponse = components['schemas']['VehicleRecordResponse']
export type StageAssemblyOut = components['schemas']['StageAssemblyOut']
export type EngineFieldsOut = components['schemas']['EngineFieldsOut']

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === 'string'
}

function isVehicleSearchHit(value: unknown): value is VehicleSearchHit {
  if (!isRecord(value)) return false
  const availability: unknown = value.availability
  return (
    typeof value.record_id === 'number' &&
    typeof value.name === 'string' &&
    isNullableString(value.variant) &&
    isNullableString(value.family) &&
    isNullableString(value.country) &&
    (value.stage_count === null || typeof value.stage_count === 'number') &&
    isRecord(availability) &&
    typeof availability.glow === 'boolean' &&
    typeof availability.length_m === 'boolean' &&
    typeof availability.diameter_m === 'boolean' &&
    typeof availability.payload_leo_kg === 'boolean'
  )
}

function isVehicleSearchResponse(value: unknown): value is VehicleSearchResponse {
  if (!isRecord(value)) return false
  return (
    typeof value.query === 'string' &&
    Array.isArray(value.hits) &&
    value.hits.every(isVehicleSearchHit)
  )
}

/** `SourcedField` 的运行时收窄：值可 null（缺失是状态不是 0，§7.5）。 */
function isSourcedField(value: unknown): value is SourcedField {
  if (!isRecord(value)) return false
  return (
    (value.value === null ||
      typeof value.value === 'number' ||
      typeof value.value === 'string') &&
    typeof value.source === 'string' &&
    typeof value.unit_uncertain === 'boolean'
  )
}

function isSourcedFieldMap(value: unknown): boolean {
  return isRecord(value) && Object.values(value).every(isSourcedField)
}

function isStageAssembly(value: unknown): value is StageAssemblyOut {
  if (!isRecord(value)) return false
  return (
    isNullableString(value.stage_no) &&
    isNullableString(value.qualifier) &&
    (value.record === null || isSourcedFieldMap(value.record)) &&
    typeof value.missing_reference === 'boolean'
  )
}

function isEngineFields(value: unknown): value is EngineFieldsOut {
  return isSourcedFieldMap(value)
}

function isVehicleRecordResponse(value: unknown): value is VehicleRecordResponse {
  if (!isRecord(value)) return false
  return (
    typeof value.name === 'string' &&
    isNullableString(value.variant) &&
    typeof value.record_id === 'number' &&
    typeof value.quality === 'string' &&
    isSourcedFieldMap(value.vehicle) &&
    Array.isArray(value.stages) &&
    value.stages.every(isStageAssembly) &&
    Array.isArray(value.engines) &&
    value.engines.every(isEngineFields) &&
    Array.isArray(value.missing) &&
    value.missing.every((item) => typeof item === 'string') &&
    Array.isArray(value.warnings) &&
    value.warnings.every((item) => typeof item === 'string') &&
    typeof value.reference_only === 'boolean'
  )
}

/**
 * 型号检索（OI-39 ②）：300 ms 防抖由调用方负责；空名**不发起请求**直接返回空表
 * （与 `matchTemplateName` 同口径：输入过程中的「还没打完」不是错误，宁漏勿错）。
 */
export function searchVehicleRecords(name: string): Promise<VehicleSearchResponse> {
  if (name.trim() === '') return Promise.resolve({ query: name, hits: [] })
  return getJson(
    `/api/catalog/vehicles/search?name=${encodeURIComponent(name)}`,
    isVehicleSearchResponse,
  )
}

/** 型号已知参数集（OI-39 ③）：按名称 + 变体精确取数；查无此型号时后端 404（ApiError）。 */
export function fetchVehicleRecord(name: string, variant?: string): Promise<VehicleRecordResponse> {
  const params = new URLSearchParams({ name })
  if (variant !== undefined && variant !== '') params.set('variant', variant)
  return getJson(`/api/catalog/vehicles/record?${params.toString()}`, isVehicleRecordResponse)
}
