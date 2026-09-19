import { describe, expect, it } from 'vitest'
import * as THREE from 'three'

import { buildSchematicGeometry } from './SchematicMesh'

/**
 * 双通道一致性门禁（规格 §13.6 / §16.3）。
 *
 * 后端在权威侧比对「GLB accessor 包围盒 vs 解析包络」，前端在此比对
 * 「示意网格包围盒 vs 同一解析包络」——两半都用同一份解析包络，偏差 ≤ 1% 才算一致（R-25）。
 */

/** 下球底(r=1,h=1) + 柱段(r=1,h=3) + 上球底(r=1,h=1)：解析包络 (2R, 2R, L) = (2, 2, 5)。 */
const ANALYTIC_ENVELOPE: [number, number, number] = [2.0, 2.0, 5.0]

/**
 * 与后端 `ValidationReport.outline` 同形的采样折线（米）。
 *
 * 后端 `outline` 是闭合轮廓：自轴线 (r=0, z=0) 起、回到轴线 (r=0, z=5)。
 */
function capsuleOutline(): [number, number][] {
  const steps = 24
  const points: [number, number][] = []

  // 下球底：球心 (0, 1)，自 (0, 0) 张开到 (1, 1)
  for (let index = 0; index <= steps; index += 1) {
    const angle = (-Math.PI / 2) * (1 - index / steps)
    points.push([Math.cos(angle), 1 + Math.sin(angle)])
  }
  // 柱段：由 (1, 1) 推到 (1, 4)
  points.push([1, 4])
  // 上球底：球心 (0, 4)，自 (1, 4) 收拢到 (0, 5)
  for (let index = 0; index <= steps; index += 1) {
    const angle = (Math.PI / 2) * (index / steps)
    points.push([Math.cos(angle), 4 + Math.sin(angle)])
  }
  return points
}

function relativeError(measured: number, reference: number): number {
  return Math.abs(measured - reference) / reference
}

describe('buildSchematicGeometry', () => {
  it('示意网格包围盒与后端解析包络逐轴一致（相对误差 ≤ 1%）', () => {
    const geometry = buildSchematicGeometry(capsuleOutline())
    const position = geometry.getAttribute('position') as THREE.BufferAttribute

    const size = new THREE.Box3().setFromBufferAttribute(position).getSize(new THREE.Vector3())

    // LatheGeometry 绕 Y 轴回转：X/Z 是径向（直径 2R），Y 是轴向（总长 L）
    expect(relativeError(size.x, ANALYTIC_ENVELOPE[0])).toBeLessThanOrEqual(0.01)
    expect(relativeError(size.y, ANALYTIC_ENVELOPE[2])).toBeLessThanOrEqual(0.01)
    expect(relativeError(size.z, ANALYTIC_ENVELOPE[1])).toBeLessThanOrEqual(0.01)

    geometry.dispose()
  })

  it('轮廓落在轴线上时回转体两端封闭', () => {
    const geometry = buildSchematicGeometry(capsuleOutline())
    const position = geometry.getAttribute('position') as THREE.BufferAttribute
    const box = new THREE.Box3().setFromBufferAttribute(position)

    expect(box.min.y).toBeCloseTo(0, 6)
    expect(box.max.y).toBeCloseTo(ANALYTIC_ENVELOPE[2], 6)
    expect(position.count).toBeGreaterThan(0)

    geometry.dispose()
  })

  it('跳过重复点后仍能构造网格（后端轮廓首点可能重复）', () => {
    const outline = capsuleOutline()
    const withDuplicate: [number, number][] = [outline[0], ...outline]
    const geometry = buildSchematicGeometry(withDuplicate)

    expect(geometry.getAttribute('position').count).toBeGreaterThan(0)

    geometry.dispose()
  })
})
