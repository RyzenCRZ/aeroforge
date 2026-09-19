import { useEffect, useMemo, useRef } from 'react'
import * as THREE from 'three'

import { readColorToken } from './palette'
import { applySceneViewState, segmentNodeName, type SceneViewState } from './sceneState'

/** 回转网格的圆周分段数（§11.3 示意通道的固定细分）。 */
const LATHE_SEGMENTS = 64

/**
 * 由后端母线轮廓构造回转网格（ADR-011：**不做任何几何计算**）。
 *
 * 输入是后端下发的**一段**闭合轮廓（单位米，(r, z) 且首末点落在轴线上），
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

interface SchematicSegmentProps {
  index: number
  /** 该段的闭合轮廓（米），来自后端 `ValidationReport.segment_outline[index]` */
  outline: [number, number][]
  wireframe: boolean
  onSelect: (index: number) => void
}

/**
 * 单段示意网格。
 *
 * 节点名与权威通道的 GLB 节点**同名**（`seg-<i>`），两通道才可能按同一粒度显隐
 * （§11.4 契约表；否则"隐藏"只在一侧生效）。
 */
function SchematicSegment({ index, outline, wireframe, onSelect }: SchematicSegmentProps) {
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

  // 资源生命周期（NFR-03）：几何体与材质随依赖变化或卸载显式释放。
  // 高亮替换进来的材质属于视口层（`Viewport`）共享实例，不在此释放。
  useEffect(() => () => geometry.dispose(), [geometry])
  useEffect(() => () => material.dispose(), [material])

  return (
    <mesh
      name={segmentNodeName(index)}
      geometry={geometry}
      material={material}
      onClick={(event) => {
        // 双向联动：点视口里的段 = 选中该段（组件树随之高亮）
        event.stopPropagation()
        onSelect(index)
      }}
    />
  )
}

interface SchematicMeshProps {
  /** 后端 `ValidationReport.segment_outline`，单位米；下标即 `seg-<i>` 的 `<i>` */
  segments: [number, number][][]
  wireframe: boolean
  viewState: SceneViewState
  /** 选中回调（写回 `store/view` 的 `selectedSegment`） */
  onSelect: (index: number) => void
}

/**
 * 示意通道网格：后端采样轮廓的**逐段**程序化回转体（权威 GLB 到达即被替换）。
 *
 * 显隐 / 高亮 / 剖切**不在 JSX 里声明**，而是走与权威通道同一条
 * `applySceneViewState` 路径——两通道的视图语义必须由同一份代码决定（§11.4）。
 * 退化段（后端给空列表）不建网格也不建节点，与几何层一致（硬约束 3）。
 */
export function SchematicMesh({ segments, wireframe, viewState, onSelect }: SchematicMeshProps) {
  const group = useRef<THREE.Group>(null)

  useEffect(() => {
    if (group.current !== null) applySceneViewState(group.current, viewState)
  }, [segments, viewState])

  return (
    <group ref={group}>
      {segments.map((outline, index) =>
        outline.length < 2 ? null : (
          <SchematicSegment
            key={index}
            index={index}
            outline={outline}
            wireframe={wireframe}
            onSelect={onSelect}
          />
        ),
      )}
    </group>
  )
}
