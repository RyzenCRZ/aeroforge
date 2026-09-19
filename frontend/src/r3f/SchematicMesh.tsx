import { useEffect, useMemo } from 'react'
import * as THREE from 'three'

import { readColorToken } from './palette'

/** 回转网格的圆周分段数（§11.3 示意通道的固定细分）。 */
const LATHE_SEGMENTS = 64

/**
 * 由后端母线轮廓构造回转网格（ADR-011：**不做任何几何计算**）。
 *
 * 输入是后端 `ValidationReport.outline`（闭合轮廓，单位米，(r, z) 且 r 落在轴线上），
 * 直接喂给 `LatheGeometry`：其轴为 three 的 **Y 轴**，即轮廓的 z 映射为世界高度。
 *
 * ⚠ 本函数只做数据搬运与去重，不推导任何半径或角度；示意网格**禁止参与**任何计算、
 * 导出或作为几何依据（ADR-012 / R-25）。
 *
 * 抽成纯函数是为了让「示意网格包围盒 vs 后端解析包络」的门禁（§13.6）在 jsdom 下可测
 * ——`LatheGeometry` 与 `Box3` 都是纯计算，不需要 WebGL 上下文。
 */
export function buildSchematicGeometry(outline: [number, number][]): THREE.BufferGeometry {
  const points: THREE.Vector2[] = []
  for (const [radius, z] of outline) {
    const previous = points[points.length - 1]
    // 后端轮廓首点可能重复（轴线起点），重复点只会产生退化三角形，去掉更干净
    if (previous !== undefined && previous.x === radius && previous.y === z) continue
    points.push(new THREE.Vector2(radius, z))
  }
  return new THREE.LatheGeometry(points, LATHE_SEGMENTS)
}

interface SchematicMeshProps {
  /** 后端 `ValidationReport.outline`，单位米 */
  outline: [number, number][]
  wireframe: boolean
}

/** 示意通道网格：后端采样轮廓的程序化回转体（首个可见形状，权威 GLB 到达即被替换）。 */
export function SchematicMesh({ outline, wireframe }: SchematicMeshProps) {
  const color = useMemo(() => readColorToken('--color-accent'), [])
  const geometry = useMemo(() => buildSchematicGeometry(outline), [outline])
  const material = useMemo(
    () =>
      new THREE.MeshStandardMaterial({
        // 未取得 token（如测试环境未加载 tokens.css）时交给材质默认色，避免写死字面色值
        color: color === '' ? undefined : color,
        metalness: 0.4,
        roughness: 0.6,
        wireframe,
      }),
    [color, wireframe],
  )

  // 资源生命周期（NFR-03）：几何体与材质随依赖变化或卸载显式释放
  useEffect(() => () => geometry.dispose(), [geometry])
  useEffect(() => () => material.dispose(), [material])

  return <mesh geometry={geometry} material={material} />
}
