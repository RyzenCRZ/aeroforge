import { getJson, isRecord, postJson, putJson } from './client'
import type { components } from './schema'

/**
 * 参数域 API（规格 §6.3 / §6.4 / §6.5 / §10.1）。
 *
 * - `POST /api/params/diagnose` —— 方案诊断（含逐规则账目 `rules`）
 * - `GET  /api/params/units`     —— §6.4 显示单位全表（前端零换算系数，OI-32）
 * - `GET` / `PUT /api/params/thresholds` —— 阈值生效值 / 来源 / 写回（OI-31）
 * - `GET  /api/params/template`  —— 参数面板的起始箭（示例骨架，未经来源核对）
 *
 * 类型一律取自 OpenAPI 生成物（§18.2 禁止手写重复类型）；本模块只做传输与**边界**
 * 校验，不含任何物理或几何计算（ADR-011）。
 */

export type Vehicle = components['schemas']['Vehicle']
export type Stage = components['schemas']['Stage']
export type Booster = components['schemas']['Booster']
export type Mission = components['schemas']['Mission']
export type Diagnostic = components['schemas']['Diagnostic']
export type RuleOutcome = components['schemas']['RuleOutcome']
export type DiagnoseResponse = components['schemas']['DiagnoseResponse']
export type TemplateResponse = components['schemas']['TemplateResponse']
export type ThresholdEntry = components['schemas']['ThresholdEntry']
export type ThresholdsResponse = components['schemas']['ThresholdsResponse']
export type DisplayUnitOut = components['schemas']['DisplayUnitOut']
export type Quantity = DisplayUnitOut['quantity']

/** §6.3 的四类约束前缀（判定码前缀即类别，§11.5 据此分组）。 */
export const CONSTRAINT_PREFIXES = ['HARD_', 'ENGINEER_', 'COMPAT_', 'SAFETY_'] as const

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

/** 六字段裁定（§6.3 / §6.5 同形）。`field_path` 是「路径 → 控件」映射的键，必须校验。 */
function isDiagnostic(value: unknown): value is Diagnostic {
  if (!isRecord(value)) return false
  return (
    (value.level === 'hard' || value.level === 'warning') &&
    typeof value.code === 'string' &&
    typeof value.field_path === 'string' &&
    typeof value.message === 'string' &&
    typeof value.suggestion === 'string'
  )
}

function isRuleOutcome(value: unknown): value is RuleOutcome {
  if (!isRecord(value)) return false
  return (
    typeof value.code === 'string' &&
    typeof value.title === 'string' &&
    (value.level === 'hard' || value.level === 'warning') &&
    typeof value.threshold === 'string' &&
    typeof value.source === 'string' &&
    Array.isArray(value.diagnostics) &&
    value.diagnostics.every(isDiagnostic) &&
    (value.deferred_reason === undefined ||
      value.deferred_reason === null ||
      typeof value.deferred_reason === 'string') &&
    isStringList(value.uncovered)
  )
}

function isDiagnoseResponse(value: unknown): value is DiagnoseResponse {
  if (!isRecord(value)) return false
  return (
    Array.isArray(value.constraints) &&
    value.constraints.every(isDiagnostic) &&
    Array.isArray(value.diagnostics) &&
    value.diagnostics.every(isDiagnostic) &&
    Array.isArray(value.rules) &&
    value.rules.every(isRuleOutcome)
  )
}

function isTemplateResponse(value: unknown): value is TemplateResponse {
  if (!isRecord(value)) return false
  const vehicle = value.vehicle
  return (
    typeof value.template_id === 'string' &&
    typeof value.label === 'string' &&
    typeof value.note === 'string' &&
    isRecord(value.sourced_fields) &&
    Object.values(value.sourced_fields).every((item) => typeof item === 'string') &&
    isRecord(vehicle) &&
    typeof vehicle.name === 'string' &&
    typeof vehicle.payload_mass_kg === 'number' &&
    Array.isArray(vehicle.stages) &&
    vehicle.stages.every((stage) => isRecord(stage) && isRecord(stage.engine)) &&
    isRecord(vehicle.mission)
  )
}

function isDisplayUnit(value: unknown): value is DisplayUnitOut {
  if (!isRecord(value)) return false
  return (
    typeof value.quantity === 'string' &&
    typeof value.label === 'string' &&
    typeof value.symbol === 'string' &&
    typeof value.si_symbol === 'string' &&
    typeof value.factor === 'number' &&
    value.factor !== 0
  )
}

function isUnitsResponse(value: unknown): value is components['schemas']['UnitsResponse'] {
  if (!isRecord(value)) return false
  return Array.isArray(value.units) && value.units.every(isDisplayUnit)
}

function isThresholdEntry(value: unknown): value is ThresholdEntry {
  if (!isRecord(value)) return false
  return (
    typeof value.key === 'string' &&
    typeof value.label === 'string' &&
    (value.value === null || typeof value.value === 'number') &&
    typeof value.source === 'string' &&
    typeof value.origin === 'string' &&
    typeof value.note === 'string'
  )
}

function isThresholdsResponse(value: unknown): value is ThresholdsResponse {
  if (!isRecord(value)) return false
  return (
    typeof value.config_file === 'string' &&
    Array.isArray(value.thresholds) &&
    value.thresholds.every(isThresholdEntry)
  )
}

/** 取参数面板的起始箭（§11.5 ① 第 6 条：禁止前端硬编码型号数字）。 */
export function fetchTemplate(): Promise<TemplateResponse> {
  return getJson('/api/params/template', isTemplateResponse)
}

/**
 * 跑方案诊断。
 *
 * ⚠ 硬约束被违反 / 请求结构不合法时后端返回 **422**，字段级裁定在
 * `ApiError.details.diagnostics` 里（与 200 的 `constraints` 同形，§6.3 末注）。
 * 调用方必须把这条通路也渲染出来——它是「路径 → 控件」映射的另一半。
 */
export function diagnose(vehicle: Vehicle, signal?: AbortSignal): Promise<DiagnoseResponse> {
  return postJson('/api/params/diagnose', vehicle, isDiagnoseResponse, signal)
}

/** §6.4 显示单位全表（界面只按后端下发的 `factor` / `symbol` 格式化）。 */
export function fetchUnits(): Promise<components['schemas']['UnitsResponse']> {
  return getJson('/api/params/units', isUnitsResponse)
}

/** 阈值的**生效值**与来源（含「未配置」状态，OI-31）。 */
export function fetchThresholds(): Promise<ThresholdsResponse> {
  return getJson('/api/params/thresholds', isThresholdsResponse)
}

/** 写回 `config.toml`；`null` = 清空该键（回到「未配置」）。响应是重新读出的生效状态。 */
export function putThresholds(
  patch: Record<string, number | null>,
): Promise<ThresholdsResponse> {
  return putJson('/api/params/thresholds', patch, isThresholdsResponse)
}
