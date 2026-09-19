import { Component, useEffect, type ReactNode } from 'react'
import * as THREE from 'three'

import { artifactUrl } from '../api/geometry'
import { AUTHORITATIVE_ROTATION } from './orientation'
import { applySceneViewState, resolveSegmentIndex, type SceneViewState } from './sceneState'
import { useGlbScene } from './useGlbScene'

/** 权威通道固定消费 LOD2（§11.3：交互中降级用 LOD2，M1 尚未接入距离驱动的 LOD 切换）。 */
const AUTHORITATIVE_FILE = 'model_lod2.glb'

interface AuthoritativeSceneProps {
  /** 已加载完成的 GLB 场景；同一份对象在多次视图交互间保持不变 */
  scene: THREE.Object3D
  viewState: SceneViewState
  onSelect: (index: number) => void
}

function AuthoritativeScene({ scene, viewState, onSelect }: AuthoritativeSceneProps) {
  useEffect(() => {
    // 按 GLB **节点名**（`seg-<i>`）施加显隐 / 高亮 / 剖切；节点不存在即无操作、不报错（§11.4）
    applySceneViewState(scene, viewState)
  }, [scene, viewState])

  return (
    // 朝向契约见 `orientation.ts`：GLB 场景图自带 Rx(-90°)，渲染器看到的已是 Y-up，
    // 与示意通道（Lathe 的轴即 Y）同向，故此处**必须保持恒等**（ADR-012 双通道可切换）。
    // 改动须同步 `orientation.test.ts` 与后端 `test_dual_channel_orientation.py`。
    <group
      rotation={AUTHORITATIVE_ROTATION}
      onClick={(event) => {
        // 双向联动：点视口里的段 = 选中该段。取不到段名（点到了非分段对象）即**不动**
        event.stopPropagation()
        const index = resolveSegmentIndex(event.object)
        if (index !== null) onSelect(index)
      }}
    >
      <primitive object={scene} />
    </group>
  )
}

interface BoundaryProps {
  fallback: ReactNode
  onFailure: (reason: string) => void
  children: ReactNode
}

interface BoundaryState {
  failed: boolean
}

/**
 * GLB 加载失败（网络/解析/404）时把错误拦在视口内：
 * 不能让一次加载失败掀掉整棵工作区，也不能留下空白视口（§11.4 规则 4）。
 */
class GlbErrorBoundary extends Component<BoundaryProps, BoundaryState> {
  override state: BoundaryState = { failed: false }

  static getDerivedStateFromError(): BoundaryState {
    return { failed: true }
  }

  override componentDidCatch(error: Error): void {
    this.props.onFailure(error.message)
  }

  override render(): ReactNode {
    if (this.state.failed) return this.props.fallback
    return this.props.children
  }
}

interface AuthoritativeModelProps {
  /** 权威产物的内容寻址键（来自作业 `result_key` 或缓存命中的 `key`） */
  artifactKey: string
  /** 加载中与失败时显示的示意网格，保证"无闪烁、无空洞"（§13.7 步骤 4） */
  fallback: ReactNode
  /** 失败回调：加载失败必须回退示意通道并给出原因 */
  onFailure: (reason: string) => void
  /** 视图状态（分级显隐 / 选中高亮 / 剖切），与示意通道**同一份**（§11.4） */
  viewState: SceneViewState
  /** 选中回调（写回 `store/view` 的 `selectedSegment`） */
  onSelect: (index: number) => void
}

/** 权威通道：加载后端导出的 GLB（唯一可作为几何依据的显示形态）。 */
export function AuthoritativeModel({
  artifactKey,
  fallback,
  onFailure,
  viewState,
  onSelect,
}: AuthoritativeModelProps) {
  const url = artifactUrl(artifactKey, AUTHORITATIVE_FILE)
  // 加载 / 释放只由 url 与挂载状态驱动；视图交互改的是 `viewState`，永不重新加载（§11.4）
  const scene = useGlbScene(url, onFailure)

  return (
    <GlbErrorBoundary fallback={fallback} onFailure={onFailure}>
      {/* 加载中与失败均显示示意网格（无闪烁、无空洞，§13.7 步骤 4 / §11.4 规则 4） */}
      {scene === null ? (
        fallback
      ) : (
        <AuthoritativeScene scene={scene} viewState={viewState} onSelect={onSelect} />
      )}
    </GlbErrorBoundary>
  )
}
