import { describe, expect, it } from 'vitest'

import type { DisplayUnitOut } from './params'
import { toDisplayValue, toSiValue, toUnitTable, unitSymbol } from './units'

/**
 * §6.4 显示单位的消费侧（规格 §1.7.3 OI-32 / P1）。
 *
 * 这里钉住的是**方向**：`SI = 显示值 × factor`。方向写反是本表最易犯的错，
 * 而且症状是"数值看着像对的、量级差 10 万倍"，不会报错——所以必须由用例钉死。
 *
 * 系数只来自后端（`GET /api/params/units`）；前端不得再抄一份表，也不得反推。
 */

function unit(overrides: Partial<DisplayUnitOut> & Pick<DisplayUnitOut, 'quantity'>): DisplayUnitOut {
  return {
    label: overrides.quantity,
    symbol: 'x',
    si_symbol: 'x',
    factor: 1,
    ...overrides,
  }
}

const UNITS: DisplayUnitOut[] = [
  unit({ quantity: 'pressure', label: '压强', symbol: 'bar', si_symbol: 'Pa', factor: 1e5 }),
  unit({ quantity: 'length', label: '长度', symbol: 'm', si_symbol: 'm', factor: 1 }),
  unit({ quantity: 'force', label: '推力', symbol: 'kN', si_symbol: 'N', factor: 1e3 }),
]

describe('toUnitTable', () => {
  it('按量索引，界面按 quantity 查系数（与字段声明同一口径）', () => {
    const table = toUnitTable(UNITS)

    expect(Object.keys(table).sort()).toEqual(['force', 'length', 'pressure'])
    expect(table.pressure?.factor).toBe(1e5)
  })
})

describe('换算方向：SI = 显示值 × factor', () => {
  const table = toUnitTable(UNITS)

  it('SI → 显示值用**除**（68.9 bar 的 SI 值是 6.89e6 Pa）', () => {
    expect(toDisplayValue(table, 'pressure', 68.9e5)).toBeCloseTo(68.9, 10)
  })

  it('显示值 → SI 用**乘**，且与上一条互为逆运算', () => {
    const si = toSiValue(table, 'pressure', 68.9)

    expect(si).toBeCloseTo(68.9e5, 6)
    expect(toDisplayValue(table, 'pressure', si)).toBeCloseTo(68.9, 10)
  })

  it('factor = 1 的量两个方向同值（长度不换算）', () => {
    expect(toDisplayValue(table, 'length', 20)).toBe(20)
    expect(toSiValue(table, 'length', 20)).toBe(20)
  })

  it('factor ≠ 1 时两个方向的数值**不相等**（反向即量级级错误）', () => {
    const display = toDisplayValue(table, 'force', 700_000)

    expect(display).toBeCloseTo(700, 10)
    expect(display).not.toBeCloseTo(700_000, 0)
  })
})

describe('表里没有该量时绝不用猜出来的系数', () => {
  it('SI → 显示值原值返回', () => {
    expect(toDisplayValue({}, 'pressure', 68.9e5)).toBe(68.9e5)
  })

  it('显示值 → SI 原值返回', () => {
    expect(toSiValue({}, 'pressure', 68.9)).toBe(68.9)
  })

  it('单位符号返回空串，由调用方退回 SI 文案（而不是编一个符号）', () => {
    expect(unitSymbol(toUnitTable(UNITS), 'pressure')).toBe('bar')
    expect(unitSymbol({}, 'pressure')).toBe('')
  })
})
