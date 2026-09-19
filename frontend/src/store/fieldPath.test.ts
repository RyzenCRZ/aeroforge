import { describe, expect, it } from 'vitest'

import { parseFieldPath, readFieldPath, writeFieldPath } from './fieldPath'

/**
 * `field_path` 的解析与读写（规格 §6.3 末注 / §1.7.3 OI-32）。
 *
 * 这条链是「后端标缺陷位置 → 界面定位控件」的**唯一**依据：后端用 `stages[0].…`
 * 标出问题字段，前端必须能用**同一个字符串**读到值、写回值、找到控件。
 * 解析一旦与后端口径分叉，诊断就指得出、界面却定位不到（静默失效）。
 */

describe('parseFieldPath：与后端 field_path 的口径一致', () => {
  it('对象键与数组下标混合', () => {
    expect(parseFieldPath('stages[0].engine.isp_vacuum_s')).toEqual([
      'stages',
      0,
      'engine',
      'isp_vacuum_s',
    ])
  })

  it('多级下标一律是数字（不是字符串 "0"）', () => {
    expect(parseFieldPath('stages[1].geometry.fuel_tank.wall_thickness_m')).toEqual([
      'stages',
      1,
      'geometry',
      'fuel_tank',
      'wall_thickness_m',
    ])
  })

  it('空串解析为空数组，由调用方决定是报错还是忽略', () => {
    expect(parseFieldPath('')).toEqual([])
  })
})

describe('readFieldPath：读取用于渲染，缺值不是错误', () => {
  const root = { stages: [{ engine: { mixture_ratio: 2.56 } }] }

  it('可达时取值', () => {
    expect(readFieldPath(root, 'stages[0].engine.mixture_ratio')).toBe(2.56)
  })

  it('不可达返回 undefined 而不抛错（缺字段是常态：模板刻意留白）', () => {
    expect(readFieldPath(root, 'aero')).toBeUndefined()
    expect(readFieldPath(root, 'stages[3].engine.mixture_ratio')).toBeUndefined()
    expect(readFieldPath(root, 'mission.orbit_type')).toBeUndefined()
  })
})

describe('writeFieldPath：写回必须是新对象（zustand 靠引用变化触发重渲染）', () => {
  const vehicle = { name: 'a', stages: [{ fill_fraction: 0.95 }] }

  it('返回新对象，原对象逐字段不变', () => {
    const next = writeFieldPath(vehicle, 'stages[0].fill_fraction', 0.9)

    expect(next).not.toBe(vehicle)
    expect(next.stages[0].fill_fraction).toBe(0.9)
    expect(vehicle.stages[0].fill_fraction).toBe(0.95)
  })

  it('深拷贝到最深一级：改一个分支不会与其它分支共享引用', () => {
    const next = writeFieldPath(vehicle, 'name', 'b')

    expect(next.stages).not.toBe(vehicle.stages)
    expect(next.stages[0]).not.toBe(vehicle.stages[0])
  })

  it('路径不可达时抛出，不静默返回原对象', () => {
    // 静默会让"改了但没生效"变成一个查不出的现象（同族教训：PyInstaller hook 静默不触发）。
    expect(() => writeFieldPath(vehicle, 'stages[5].fill_fraction', 1)).toThrow(/不可达|越界/)
    expect(() => writeFieldPath(vehicle, 'aero.fairing_diameter_m', 1)).toThrow(/不可达/)
  })

  it('空路径直接抛出（空路径不可能对应任何控件）', () => {
    expect(() => writeFieldPath(vehicle, '', 1)).toThrow(/为空|无法解析/)
  })
})
