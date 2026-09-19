import { getJson, isRecord } from './client'
import type { components } from './schema'

/**
 * 模板域 API（规格 §11.5 ⑤ 规则 2/5 / OI-29 / OI-34）。
 *
 * - `GET /api/templates`         —— 内置模板清单（仅元数据）
 * - `GET /api/templates/{id}`    —— 单个模板的完整参数（含逐字段出处表）
 * - `GET /api/templates/match?name=` —— OI-34 名称匹配（别名表也在匹配宇宙内）
 *
 * 类型一律取自 OpenAPI 生成物（§18.2 禁止手写重复类型）；本模块只做传输与**边界**
 * 校验，不含任何物理或几何计算（ADR-011）。守卫写法与 `params.ts` 的
 * `isTemplateResponse` 同形态。
 */

export type TemplateSummary = components['schemas']['TemplateSummaryOut']
export type TemplateListResponse = components['schemas']['TemplateListResponse']
export type TemplateMatchResponse = components['schemas']['TemplateMatchResponse']
export type TemplateDetail = components['schemas']['TemplateDetailResponse']

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function isSourcedFields(value: unknown): value is Record<string, string> {
  return (
    isRecord(value) && Object.values(value).every((item) => typeof item === 'string')
  )
}

/**
 * `Vehicle` 的运行时收窄（与 `params.ts` 的 `isTemplateResponse` 对 vehicle 部分的
 * 校验同口径：结构存在即可，物理合法性由后端产品校验器负责）。
 */
function isVehicleShape(value: unknown): boolean {
  if (!isRecord(value)) return false
  return (
    typeof value.name === 'string' &&
    typeof value.payload_mass_kg === 'number' &&
    Array.isArray(value.stages) &&
    value.stages.every((stage) => isRecord(stage) && isRecord(stage.engine)) &&
    isRecord(value.mission)
  )
}

function isTemplateSummary(value: unknown): value is TemplateSummary {
  if (!isRecord(value)) return false
  return (
    typeof value.id === 'string' &&
    typeof value.name === 'string' &&
    isStringList(value.aliases) &&
    typeof value.stage_count === 'number' &&
    typeof value.note === 'string' &&
    typeof value.reference_payload_leo_kg === 'number'
  )
}

function isTemplateListResponse(value: unknown): value is TemplateListResponse {
  if (!isRecord(value)) return false
  return Array.isArray(value.templates) && value.templates.every(isTemplateSummary)
}

function isNullableString(value: unknown): boolean {
  return value === null || value === undefined || typeof value === 'string'
}

function isTemplateMatchResponse(value: unknown): value is TemplateMatchResponse {
  if (!isRecord(value)) return false
  if (typeof value.matched !== 'boolean') return false
  if (
    !isNullableString(value.template_id) ||
    !isNullableString(value.name) ||
    !isNullableString(value.note)
  ) {
    return false
  }
  // 契约：未命中时其余字段为 null（「还没打完」不是错误，不报错）；
  // 命中时 id 与名称必须给出，否则下游没法取详情。
  return !value.matched || (typeof value.template_id === 'string' && typeof value.name === 'string')
}

function isTemplateDetail(value: unknown): value is TemplateDetail {
  if (!isRecord(value)) return false
  return (
    typeof value.id === 'string' &&
    typeof value.name === 'string' &&
    typeof value.note === 'string' &&
    isStringList(value.aliases) &&
    typeof value.reference_payload_leo_kg === 'number' &&
    isSourcedFields(value.sourced_fields) &&
    isVehicleShape(value.vehicle)
  )
}

/** 内置模板清单（仅元数据；完整参数按 id 走 `fetchTemplateDetail`）。 */
export function fetchTemplates(): Promise<TemplateListResponse> {
  return getJson('/api/templates', isTemplateListResponse)
}

/** 单个模板的完整参数（§11.5 ⑤ 规则 2：含逐字段出处表与可直接提交诊断的 vehicle）。 */
export function fetchTemplateDetail(templateId: string): Promise<TemplateDetail> {
  return getJson(`/api/templates/${encodeURIComponent(templateId)}`, isTemplateDetail)
}

/** 未命中的响应体（契约：matched=false、其余字段 null——不报错）。 */
const TEMPLATE_MATCH_MISS: TemplateMatchResponse = {
  matched: false,
  template_id: null,
  name: null,
  note: null,
}

/**
 * OI-34 名称匹配（别名也在后端的匹配宇宙内，前端不做任何规范化）。
 *
 * 空名**不发起请求**直接返回未命中：输入过程中的频繁查询里，「还没打完」不是错误
 * （宁漏勿错）。调用方仍须自带防抖。
 */
export function matchTemplateName(name: string): Promise<TemplateMatchResponse> {
  if (name.trim() === '') return Promise.resolve({ ...TEMPLATE_MATCH_MISS })
  return getJson(`/api/templates/match?name=${encodeURIComponent(name)}`, isTemplateMatchResponse)
}
