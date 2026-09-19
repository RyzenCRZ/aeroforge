import { OrbitControls } from '@react-three/drei'
import { Canvas, useFrame } from '@react-three/fiber'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import type { ValidationReport } from '../api/geometry'
import { useModelStore } from '../store/model'
import { useViewStore } from '../store/view'
import { AuthoritativeModel } from './AuthoritativeModel'
import { detectWebGL2, publishProbe, type GlSupport } from './probe'
import { readColorToken } from './palette'
import { ScaleOverlay } from './ScaleOverlay'
import { SchematicMesh } from './SchematicMesh'
import './Viewport.css'

const CAMERA_POSITION: [number, number, number] = [5, 6, 5]
const CAMERA_FOV = 45
/** 参考网格的尺寸与格数：纯场景装饰，不参与任何计算 */
const GRID_SIZE = 20
const GRID_DIVISIONS = 20

/**
 * 取景中心 = 回转体轴向中点。
 *
 * 这只是相机取景（§11.6 `store/view` 的范畴），数值取自后端 `total_length`，
 * 既不做几何推导，也不进入任何计算或导出（ADR-011）。
 */
function cameraTargetY(report: ValidationReport | null): number {
  return report === null ? 0 : report.total_length / 2
}

/**
 * 首帧发布 M0.5 探针（§16.2）。
 *
 * 必须在**真实渲染循环**里发布——与数据是否已加载无关，空场景也要发布，
 * 否则启动器只会得到"探针超时"这种无诊断价值的结论。
 */
function ProbePublisher({ support, mountedAt }: { support: GlSupport; mountedAt: number }) {
  const published = useRef(false)

  useFrame(() => {
    if (published.current) return
    published.current = true
    publishProbe(support, performance.now() - mountedAt)
  })

  return null
}

/** 3D 视口：双通道（示意 / 权威）+ 光照滑杆 + 包络叠加层（ADR-012 / §11.3）。 */
export function Viewport() {
  const support = useMemo(detectWebGL2, [])
  const mountedAt = useMemo(() => performance.now(), [])

  const lightIntensity = useViewStore((state) => state.lightIntensity)
  const showGrid = useViewStore((state) => state.showGrid)
  const autoRotate = useViewStore((state) => state.autoRotate)
  const wireframe = useViewStore((state) => state.wireframe)

  const report = useModelStore((state) => state.report)
  const channel = useModelStore((state) => state.channel)
  const authoritativeKey = useModelStore((state) => state.authoritativeKey)

  const [glbNotice, setGlbNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!support.webgl2) {
      // 兜底上报：让启动器能给出"WebGL2 不可用"的明确结论，而不是超时
      publishProbe(support, -1)
    }
  }, [support])

  const handleGlbFailure = useCallback((reason: string) => {
    setGlbNotice(`权威 GLB 加载失败，已回退示意通道：${reason}`)
    useModelStore.getState().useSchematic()
  }, [])

  if (!support.webgl2) {
    // 禁止静默白屏：必须显式报错（规格 R-33）
    return (
      <div className="viewport viewport--failed">
        <p className="label">WebGL2 不可用</p>
        <p className="viewport__message">
          WebGL2 不可用，3D 视口方案不成立（规格 R-33）。当前桌面壳（WebView2）无法创建 WebGL2
          上下文，需先修订规格再继续；后备路径为服务端渲染图像 + 交互重放。
        </p>
      </div>
    )
  }

  const outline = report === null ? null : report.outline
  const gridColor = readColorToken('--color-border')
  const schematic = outline === null ? null : <SchematicMesh outline={outline} wireframe={wireframe} />

  return (
    <div className="viewport">
      <Canvas className="viewport__canvas" camera={{ position: CAMERA_POSITION, fov: CAMERA_FOV }}>
        <ambientLight intensity={0.6 * lightIntensity} />
        <directionalLight position={[4, 6, 3]} intensity={1.6 * lightIntensity} />
        <ProbePublisher support={support} mountedAt={mountedAt} />
        {showGrid && gridColor !== '' ? (
          <gridHelper args={[GRID_SIZE, GRID_DIVISIONS, gridColor, gridColor]} />
        ) : null}
        {channel === 'authoritative' && authoritativeKey !== null ? (
          // 权威 GLB 到达即强制替换示意网格；加载期间仍显示示意网格（无闪烁、无空洞）
          <AuthoritativeModel
            artifactKey={authoritativeKey}
            fallback={schematic}
            onFailure={handleGlbFailure}
          />
        ) : (
          schematic
        )}
        <OrbitControls autoRotate={autoRotate} target={[0, cameraTargetY(report), 0]} />
      </Canvas>
      <ScaleOverlay envelope={report?.envelope ?? null} channel={channel} notice={glbNotice} />
    </div>
  )
}
