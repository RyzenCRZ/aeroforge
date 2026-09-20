import { describe, expect, it } from 'vitest'

import type { SectionBand, SectionsResponse } from '../api/sections'
import {
  bandsLengthSum,
  boosterAxisX,
  boosterHalfW,
  boosterTopY,
  buildSectionsLayout,
  clampZoom,
  createAxisMapping,
  liquidLayout,
  viewBoxText,
  zoomViewBox,
  type Frame,
} from './profileGeometry'

/**
 * `profileGeometry` 的**纯渲染守卫**（ADR-011 / §11.10）。
 *
 * 本模块只允许做**线性排版映射**（y = a + k·x）。下面全部用「输入 → 输出」对把线性
 * 性质钉死：任何物理公式（开方、平方比、比例反推等）混进来都会破坏这些等式——
 * 例如"由直径 / 长度反推条带高度"或"前端自算液面"都会在此现形。
 */

const FRAME: Frame = {
  width: 400,
  height: 600,
  padLeft: 90,
  padRight: 100,
  padTop: 20,
  padBottom: 30,
  boosterLane: 50,
}

const AVAIL_W = FRAME.width - FRAME.padLeft - FRAME.padRight - FRAME.boosterLane // = 160
const AVAIL_H = FRAME.height - FRAME.padTop - FRAME.padBottom // = 550

describe('坐标映射只做线性排版（ADR-011 纯渲染守卫）', () => {
  it('纵向受限时 scale = 可用高 / 总长，整箭恰好占满可用高（z=0 在头部顶点）', () => {
    // 总长 110 m、直径 2 m：若按高度铺满 scale = 550/110 = 5，体宽仅 10 < 160 → 高度受限
    const mapping = createAxisMapping(110, 2, FRAME)
    expect(mapping).not.toBeNull()
    if (mapping === null) return

    expect(mapping.noseY).toBe(FRAME.padTop)
    expect(mapping.scale).toBeCloseTo(5, 10)
    // 输入输出对：y(总长) − y(0) = 总长 × scale = 可用高（线性映射的端点校验）
    expect(mapping.noseY + 110 * mapping.scale).toBeCloseTo(FRAME.padTop + AVAIL_H, 10)
  })

  it('横向受限时体宽恰好占满可用宽，且两轴共用同一 scale（等比，不拉伸）', () => {
    // 总长 10 m、直径 4 m：高度需 10×40 = 400 < 550 → 宽度受限，scale = 160/4 = 40
    const mapping = createAxisMapping(10, 4, FRAME)
    expect(mapping).not.toBeNull()
    if (mapping === null) return

    expect(mapping.scale).toBeCloseTo(40, 10)
    expect(mapping.bodyRight - mapping.bodyLeft).toBeCloseTo(AVAIL_W, 10)
    // 轴线居中：左右半宽对称（线性映射的对称性校验）
    expect(mapping.axisX - mapping.bodyLeft).toBeCloseTo(mapping.bodyRight - mapping.axisX, 10)
    expect(mapping.bodyHalfW).toBeCloseTo(AVAIL_W / 2, 10)
  })

  it('非法输入（总长或直径 ≤ 0）返回 null，由调用方降级提示', () => {
    expect(createAxisMapping(0, 3, FRAME)).toBeNull()
    expect(createAxisMapping(10, 0, FRAME)).toBeNull()
    expect(createAxisMapping(-1, 3, FRAME)).toBeNull()
  })
})

describe('版面排版（buildSectionsLayout，纯线性堆叠）', () => {
  function band(section: SectionBand['section'], lengthM: number): SectionBand {
    return { section, label_zh: section, length_m: lengthM }
  }

  function response(overrides: {
    stages?: SectionsResponse['stages']
    boosters?: SectionsResponse['boosters']
    totalLength: number
  }): SectionsResponse {
    return {
      stages: overrides.stages ?? [],
      boosters: overrides.boosters ?? [],
      dimensions: {
        total_length_m: overrides.totalLength,
        max_diameter_m: 3.35,
        fairing_diameter_m: 4.2,
        labels: [],
      },
      warnings: [],
      provenance: {},
    }
  }

  it('级内条带逐段堆叠：相邻条带的高度差与 length_m 成同一比例（线性 IO 对）', () => {
    const data = response({
      stages: [
        {
          stage_index: 1,
          level: 1,
          bands: [band('ox_tank', 1), band('intertank', 2), band('fuel_tank', 3)],
          delivery_pipe_routing: 'external',
          tank_order: 'oxidizer_first',
        },
      ],
      totalLength: 6,
    })
    const layout = buildSectionsLayout(data, FRAME)
    expect(layout).not.toBeNull()
    if (layout === null) return

    const { groups, mapping } = layout
    const bands = groups[0]?.bands ?? []
    expect(bands).toHaveLength(3)
    // 每条条带自身的排版高 ∝ 后端 length_m，比例恒为 scale（线性 IO 对）
    for (const box of bands) {
      expect(box.h / box.band.length_m).toBeCloseTo(mapping.scale, 10)
    }
    // 相邻条带的 y 间距 = 前一条带的 length_m × scale
    for (let i = 1; i < bands.length; i += 1) {
      const gap = bands[i]!.y - bands[i - 1]!.y
      expect(gap / bands[i - 1]!.band.length_m).toBeCloseTo(mapping.scale, 10)
    }
    // 顶到头（z=0 即头部顶点）
    expect(bands[0]!.y).toBeCloseTo(mapping.noseY, 10)
  })

  it('多级：level 降序自上而下（level 1 = 底级），两级相接处 y 连续', () => {
    const data = response({
      stages: [
        {
          stage_index: 1,
          level: 1,
          bands: [band('engine_bay', 3)],
          delivery_pipe_routing: 'external',
          tank_order: 'oxidizer_first',
        },
        {
          stage_index: 2,
          level: 2,
          bands: [band('fairing', 6), band('engine_bay', 1)],
          delivery_pipe_routing: 'external',
          tank_order: 'oxidizer_first',
        },
      ],
      totalLength: 10,
    })
    const layout = buildSectionsLayout(data, FRAME)
    expect(layout).not.toBeNull()
    if (layout === null) return

    const [upper, lower] = layout.coreGroups
    expect(upper?.bands[0]?.band.section).toBe('fairing') // level 2 在上
    expect(upper?.bottomY).toBeCloseTo(lower?.topY ?? Number.NaN, 10)
  })

  it('助推器（level 0）：右侧车道、底边与整箭底对齐（呈现约定 = 线性映射）', () => {
    const data = response({
      stages: [
        {
          stage_index: 1,
          level: 1,
          bands: [band('ox_tank', 24)],
          delivery_pipe_routing: 'external',
          tank_order: 'oxidizer_first',
        },
      ],
      boosters: [
        {
          stage_index: 0,
          level: 0,
          bands: [band('ox_tank', 6)],
          delivery_pipe_routing: 'external',
          tank_order: 'oxidizer_first',
        },
      ],
      totalLength: 30,
    })
    const layout = buildSectionsLayout(data, FRAME)
    expect(layout).not.toBeNull()
    if (layout === null) return

    expect(layout.coreGroups).toHaveLength(1)
    expect(layout.groups).toHaveLength(2)
    const booster = layout.groups[1]!
    expect(booster.isBooster).toBe(true)
    expect(booster.axisX).toBeGreaterThan(layout.mapping.bodyRight) // 在芯级右侧
    expect(booster.axisX).toBe(boosterAxisX(layout.mapping.bodyRight, layout.mapping.bodyHalfW, 0))
    expect(booster.halfW).toBe(boosterHalfW(layout.mapping.bodyHalfW))
    // 底对齐（线性 IO 对）：助推器顶 = 头部顶点 + (总长 − 助推器长) × scale，
    // 助推器底 = 头部顶点 + 总长 × scale（即整箭底边）
    expect(booster.topY).toBeCloseTo(layout.mapping.noseY + 24 * layout.mapping.scale, 10)
    expect(booster.bottomY).toBeCloseTo(layout.mapping.noseY + 30 * layout.mapping.scale, 10)
  })
})

describe('液面 / 助推器 / 求和的线性 IO 对', () => {
  it('liquidLayout：液体占条带底部 liquid_level_m，超出条带长截断（排版截断非物理）', () => {
    expect(liquidLayout(10, 8, 5)).toEqual({ liquid: 40, ullage: 10 })
    expect(liquidLayout(10, 12, 5)).toEqual({ liquid: 50, ullage: 0 })
    expect(liquidLayout(10, -1, 5)).toEqual({ liquid: 0, ullage: 50 })
    // 线性：液位翻倍 → 液体区翻倍
    expect(liquidLayout(10, 4, 5).liquid * 2).toBeCloseTo(liquidLayout(10, 8, 5).liquid, 10)
  })

  it('bandsLengthSum / boosterAxisX：纯求和与等差数列（线性）', () => {
    expect(bandsLengthSum([{ section: 'ox_tank', label_zh: '', length_m: 1.5 }, { section: 'fuel_tank', label_zh: '', length_m: 3 }])).toBeCloseTo(4.5, 10)
    const right = 200
    const halfW = 50
    expect(boosterHalfW(halfW)).toBeCloseTo(30, 10)
    // 第 0 组贴芯级，后续每组等距右移（公差 = 列宽 + 间距）
    expect(boosterAxisX(right, halfW, 0)).toBeCloseTo(238, 10)
    expect(boosterAxisX(right, halfW, 1) - boosterAxisX(right, halfW, 0)).toBeCloseTo(2 * 30 + 8, 10)
    expect(boosterTopY(30, 6, 20, 5)).toBeCloseTo(20 + 24 * 5, 10)
  })
})

describe('缩放排版（只改视口，不改映射基准）', () => {
  it('clampZoom：档位钳在 0.5×～4×', () => {
    expect(clampZoom(10)).toBe(4)
    expect(clampZoom(0.1)).toBe(0.5)
    expect(clampZoom(1.5)).toBe(1.5)
    expect(clampZoom(4)).toBe(4)
  })

  it('zoomViewBox：1× = 全幅；2× = 以绘图区中心收缩一半（输入输出对）', () => {
    expect(zoomViewBox(FRAME, 1)).toEqual({ x: 0, y: 0, w: 400, h: 600 })
    expect(zoomViewBox(FRAME, 2)).toEqual({ x: 100, y: 150, w: 200, h: 300 })
    const zoomed = zoomViewBox(FRAME, 2)
    expect(zoomed.x + zoomed.w / 2).toBeCloseTo(FRAME.width / 2, 10)
    expect(zoomed.y + zoomed.h / 2).toBeCloseTo(FRAME.height / 2, 10)
    // 超界档位同样被钳住后线性缩放
    expect(zoomViewBox(FRAME, 99).w).toBeCloseTo(FRAME.width / 4, 10)
    expect(zoomViewBox(FRAME, 0.01).w).toBeCloseTo(FRAME.width / 0.5, 10)
  })

  it('viewBoxText：固定 "x y w h" 顺序', () => {
    expect(viewBoxText({ x: 0, y: 0, w: 400, h: 600 })).toBe('0 0 400 600')
    expect(viewBoxText({ x: 34, y: 62, w: 272, h: 496 })).toBe('34 62 272 496')
  })
})
