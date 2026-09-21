import { OrbitControls } from '@react-three/drei'
import { Canvas, useFrame } from '@react-three/fiber'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as THREE from 'three'

import type { ValidationReport } from '../api/geometry'
import { exportCanvasPng, pngExportScale } from '../lib/exportPng'
import { useModelStore } from '../store/model'
import { useVehicleStore } from '../store/vehicle'
import { useViewStore } from '../store/view'
import { AuthoritativeModel } from './AuthoritativeModel'
import { detectWebGL2, publishProbe, type GlSupport } from './probe'
import { readColorToken } from './palette'
import { ScaleOverlay } from './ScaleOverlay'
import { SchematicMesh } from './SchematicMesh'
import { buildClippingPlane, type SceneViewState } from './sceneState'
import type { ExplodeViewState } from './explode'
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

/**
 * 视口角落工具条（§11.4 / OI-19 M5 + OI-26）：爆炸视图滑杆（0–1）+ 复位 + PNG 导出。
 *
 * 导出在**爆炸态禁用**（§11.4：爆炸位移是视图偏移，不得进入导出的几何——导出按钮
 * 置灰并给出「爆炸视图为视图状态」提示）；剖切的互斥由 `store/view` 收口
 * （激活爆炸即关剖切），此处只显示冲突提示。
 */
export function ViewportTools({ onExportPng }: { onExportPng: () => void }) {
  const explodeFactor = useViewStore((state) => state.explodeFactor)
  const setExplodeFactor = useViewStore((state) => state.setExplodeFactor)
  const resetExplode = useViewStore((state) => state.resetExplode)
  const exploded = explodeFactor > 0

  return (
    <div className="viewport__tools" data-testid="viewport-tools">
      <label className="viewport__tool">
        <span className="label">爆炸</span>
        <input
          type="range"
          min={0}
          max={1}
          step={0.01}
          value={explodeFactor}
          aria-label="爆炸视图"
          onChange={(event) => setExplodeFactor(Number(event.target.value))}
        />
        <span className="num">{explodeFactor.toFixed(2)}</span>
      </label>
      <button type="button" className="viewport__tool-button" onClick={resetExplode}>
        复位
      </button>
      <button
        type="button"
        className="viewport__tool-button"
        onClick={onExportPng}
        disabled={exploded}
        title={exploded ? '爆炸视图为视图状态，不可导出' : undefined}
      >
        导出 PNG
      </button>
      {exploded ? (
        <span className="viewport__tools-hint label">
          爆炸视图为视图状态（不可导出；剖切已互斥关闭）
        </span>
      ) : null}
    </div>
  )
}

/** 3D 视口：双通道（示意 / 权威）+ 光照滑杆 + 包络叠加层（ADR-012 / §11.3）。 */
export function Viewport() {
  const support = useMemo(detectWebGL2, [])
  const mountedAt = useMemo(() => performance.now(), [])

  const lightIntensity = useViewStore((state) => state.lightIntensity)
  const showGrid = useViewStore((state) => state.showGrid)
  const autoRotate = useViewStore((state) => state.autoRotate)
  const wireframe = useViewStore((state) => state.wireframe)
  const hiddenSegments = useViewStore((state) => state.hiddenSegments)
  const selectedSegment = useViewStore((state) => state.selectedSegment)
  const clip = useViewStore((state) => state.clip)
  const explodeFactor = useViewStore((state) => state.explodeFactor)
  const selectSegment = useViewStore((state) => state.selectSegment)

  const report = useModelStore((state) => state.report)
  const channel = useModelStore((state) => state.channel)
  const authoritativeKey = useModelStore((state) => state.authoritativeKey)

  const [glbNotice, setGlbNotice] = useState<string | null>(null)

  /**
   * PNG 导出所需的渲染上下文（`onCreated` 时定格——gl / scene / camera 在 r3f 里
   * 均为长生命周期实例）。导出时临时提升 pixelRatio 重渲一帧，见 `handleExportPng`。
   */
  const renderContextRef = useRef<{
    gl: THREE.WebGLRenderer
    scene: THREE.Scene
    camera: THREE.Camera
  } | null>(null)

  /**
   * PNG 导出（OI-26，行 2038 口径）：先按 `pngExportScale()`（devicePixelRatio 整数倍、
   * 至少 2×）提升渲染分辨率并同步重渲一帧，再等一帧 rAF 后按 canvas **实际像素**
   * 截取——绝不把 1× 位图放大（伪高清）或读 CSS 布局尺寸（拉伸模糊）。
   */
  const handleExportPng = useCallback(async () => {
    const context = renderContextRef.current
    if (context === null) return
    const { gl, scene, camera } = context
    const name = useVehicleStore.getState().vehicle?.name ?? ''
    const previousRatio = gl.getPixelRatio()
    try {
      gl.setPixelRatio(pngExportScale())
      gl.render(scene, camera)
      await exportCanvasPng(gl.domElement, name)
    } finally {
      gl.setPixelRatio(previousRatio)
    }
  }, [])

  /**
   * 选中段的高亮材质（两通道共用同一个实例）。
   *
   * 用**替换材质**而不是就地改 emissive：GLB 各段引用的是同一个材质实例，就地改色会把
   * 所有段一起点亮。释放由本组件负责，故 `applySceneViewState` 只替换、不持有。
   */
  const highlightMaterial = useMemo(() => {
    const accent = readColorToken('--color-accent')
    return new THREE.MeshStandardMaterial({
      color: accent === '' ? undefined : accent,
      emissive: accent === '' ? undefined : accent,
      emissiveIntensity: 0.45,
      metalness: 0.3,
      roughness: 0.5,
      wireframe,
    })
  }, [wireframe])

  useEffect(() => () => highlightMaterial.dispose(), [highlightMaterial])

  /**
   * 剖切平面（§11.4：单个可移动平面，**纯渲染裁剪**，不重建几何、不可导出）。
   *
   * 尺度取自后端报告的 `total_length` / `max_radius`，前端不推导任何几何量（ADR-011）。
   */
  const clipPlanes = useMemo(() => {
    if (!clip.enabled || report === null) return [] as THREE.Plane[]
    return [
      buildClippingPlane(clip.axis, clip.position, {
        length: report.total_length,
        radius: report.max_radius,
      }),
    ]
  }, [clip.enabled, clip.axis, clip.position, report])

  /**
   * 爆炸视图状态（§11.4 / OI-19 M5）：特征长度取后端报告的 `total_length`（前端不推导，
   * 与剖切的 `ClipExtent` 同一来源口径）；`factor = 0` 也要施加——复位即还原常态位置。
   */
  const explode = useMemo<ExplodeViewState | null>(() => {
    if (report === null) return null
    return { factor: explodeFactor, length: report.total_length }
  }, [explodeFactor, report])

  /** 视图状态是**同一份对象**喂给两条通道——两通道的显隐语义必须同源（§11.4）。 */
  const viewState = useMemo<SceneViewState>(
    () => ({
      hidden: hiddenSegments,
      selected: selectedSegment,
      highlight: highlightMaterial,
      clipPlanes,
      explode,
    }),
    [hiddenSegments, selectedSegment, highlightMaterial, clipPlanes, explode],
  )

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

  const segments = report === null ? null : report.segment_outline
  const gridColor = readColorToken('--color-border')
  const schematic =
    segments === null ? null : (
      <SchematicMesh
        segments={segments}
        wireframe={wireframe}
        viewState={viewState}
        onSelect={selectSegment}
      />
    )

  return (
    <div className="viewport">
      <Canvas
        className="viewport__canvas"
        camera={{ position: CAMERA_POSITION, fov: CAMERA_FOV }}
        onCreated={(state) => {
          // 逐材质裁剪（`material.clippingPlanes`）必须先打开渲染器的全局开关，
          // 否则剖切**静默不生效**（画面完全正常，只是没被切）
          state.gl.localClippingEnabled = true
          renderContextRef.current = {
            gl: state.gl,
            scene: state.scene,
            camera: state.camera,
          }
        }}
      >
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
            viewState={viewState}
            onSelect={selectSegment}
          />
        ) : (
          schematic
        )}
        <OrbitControls autoRotate={autoRotate} target={[0, cameraTargetY(report), 0]} />
      </Canvas>
      <ViewportTools onExportPng={handleExportPng} />
      <ScaleOverlay envelope={report?.envelope ?? null} channel={channel} notice={glbNotice} />
    </div>
  )
}
