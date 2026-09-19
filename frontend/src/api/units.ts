import type { components } from './schema'

/**
 * §6.4 显示单位的**消费侧**（规格 §1.7.3 OI-32 / P1）。
 *
 * 规则：换算系数**只来自后端**（`GET /api/params/units` 的 `factor`），前端不得再抄一份表、
 * 也不得反推系数（该端点由 `api/params.ts` 的 `fetchUnits` 提供）。
 * 方向是固定的、也是最容易写反的一条：
 *
 * ```text
 * SI = 显示值 × factor      （factor ≥ 1 时显示值更小，例如压力 bar：1 bar = 1e5 Pa）
 * ```
 *
 * 本模块只做"数值 ↔ 显示"的换算与格式化，不参与任何物理计算（ADR-011）。
 */

export type UnitTable = Record<string, components['schemas']['DisplayUnitOut']>

/** 把单位表数组转成按量索引的映射；同量重复时后者覆盖前者（后端不应出现重复）。 */
export function toUnitTable(units: readonly components['schemas']['DisplayUnitOut'][]): UnitTable {
  const table: UnitTable = {}
  for (const unit of units) table[unit.quantity] = unit
  return table
}

/** SI → 显示值。量不在表内（或表里系数为 0）时返回原值——绝不用猜出来的系数换算。 */
export function toDisplayValue(
  table: UnitTable,
  quantity: components['schemas']['DisplayUnitOut']['quantity'],
  siValue: number,
): number {
  const unit = table[quantity]
  if (unit === undefined || unit.factor === 0) return siValue
  return siValue / unit.factor
}

/** 显示值 → SI（编辑时用）。 */
export function toSiValue(
  table: UnitTable,
  quantity: components['schemas']['DisplayUnitOut']['quantity'],
  displayValue: number,
): number {
  const unit = table[quantity]
  if (unit === undefined) return displayValue
  return displayValue * unit.factor
}

/** 显示单位符号；表里没有该量时返回空串（调用方据此退回 SI 文案，而不是编一个符号）。 */
export function unitSymbol(
  table: UnitTable,
  quantity: components['schemas']['DisplayUnitOut']['quantity'],
): string {
  return table[quantity]?.symbol ?? ''
}
