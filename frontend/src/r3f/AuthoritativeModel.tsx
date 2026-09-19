import { useLoader } from '@react-three/fiber'
import { Component, Suspense, useEffect, type ReactNode } from 'react'
import * as THREE from 'three'
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js'

import { artifactUrl } from '../api/geometry'
import { AUTHORITATIVE_ROTATION } from './orientation'

/** 权威通道固定消费 LOD2（§11.3：交互中降级用 LOD2，M1 尚未接入距离驱动的 LOD 切换）。 */
const AUTHORITATIVE_FILE = 'model_lod2.glb'

/** 释放 GLB 场景内的全部几何体与材质（NFR-03：重建时显式 dispose）。 */
function disposeScene(root: THREE.Object3D): void {
  root.traverse((object) => {
    if (!(object instanceof THREE.Mesh)) return
    object.geometry.dispose()
    const materials = Array.isArray(object.material) ? object.material : [object.material]
    for (const material of materials) material.dispose()
  })
}

function AuthoritativeScene({ url }: { url: string }) {
  const gltf = useLoader(GLTFLoader, url)

  useEffect(() => {
    const scene = gltf.scene
    return () => {
      disposeScene(scene)
      // `useLoader` 的结果由 suspend-react 全局缓存，不随卸载失效；
      // 若只 dispose 不清缓存，重新挂载会复用已释放的几何体与材质。
      useLoader.clear(GLTFLoader, url)
    }
  }, [gltf, url])

  return (
    // 朝向契约见 `orientation.ts`：GLB 场景图自带 Rx(-90°)，渲染器看到的已是 Y-up，
    // 与示意通道（Lathe 的轴即 Y）同向，故此处**必须保持恒等**（ADR-012 双通道可切换）。
    // 改动须同步 `orientation.test.ts` 与后端 `test_dual_channel_orientation.py`。
    <group rotation={AUTHORITATIVE_ROTATION}>
      <primitive object={gltf.scene} />
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
}

/** 权威通道：加载后端导出的 GLB（唯一可作为几何依据的显示形态）。 */
export function AuthoritativeModel({ artifactKey, fallback, onFailure }: AuthoritativeModelProps) {
  const url = artifactUrl(artifactKey, AUTHORITATIVE_FILE)
  return (
    <GlbErrorBoundary fallback={fallback} onFailure={onFailure}>
      <Suspense fallback={fallback}>
        <AuthoritativeScene url={url} />
      </Suspense>
    </GlbErrorBoundary>
  )
}
