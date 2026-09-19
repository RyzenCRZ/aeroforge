import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { GLTFLoader, type GLTF } from 'three/examples/jsm/loaders/GLTFLoader.js'

import { restoreOriginalMaterials } from './sceneState'

/** 释放 GLB 场景内的全部几何体与材质（NFR-03：重建时显式 dispose）。 */
export function disposeScene(root: THREE.Object3D): void {
  // ⚠ 先还原原始材质：选中高亮是**材质替换**，被换上去的是视口层共享的实例，
  //   直接释放会把别人的材质一起 dispose（重建后复用即要重新编译，且无任何报错）
  restoreOriginalMaterials(root)
  root.traverse((object) => {
    if (!(object instanceof THREE.Mesh)) return
    object.geometry.dispose()
    const materials = Array.isArray(object.material) ? object.material : [object.material]
    for (const material of materials) material.dispose()
  })
}

/**
 * 按 url 加载 GLB 场景并持有其生命周期。
 *
 * 刻意**不用** `@react-three/fiber` 的 `useLoader`：后者依赖 suspend-react 全局缓存，
 * 而我们过去在卸载清理里调 `useLoader.clear` 防"复用已释放资源"——StrictMode 首挂载的
 * setup→cleanup→setup 会在刚加载完时把缓存清掉，此后**每次** `viewState` 变化触发的
 * 重渲染都会缓存未命中、重新发起请求并在 Suspense 边界回退（实测每次交互一次 XHR）。
 *
 * 本 hook 的释放时机只有三种：url 变化（新产物）、切回示意通道、Canvas 卸载。
 * 显隐 / 选中 / 剖切只改视图状态，不进本 hook、**不产生任何后端请求**（§11.4 / §13.6）。
 *
 * @returns 已加载完成的场景；加载中为 null（调用方负责渲染 fallback）
 */
export function useGlbScene(
  url: string,
  onFailure: (reason: string) => void,
): THREE.Object3D | null {
  const [scene, setScene] = useState<THREE.Object3D | null>(null)
  // ref 让清理闭包拿到最新场景，同时不因此重订阅 effect
  const sceneRef = useRef<THREE.Object3D | null>(null)

  useEffect(() => {
    let cancelled = false
    const loader = new GLTFLoader()

    loader.load(
      url,
      (gltf: GLTF) => {
        // StrictMode 首挂载的 setup→cleanup→setup 会作废第一次请求：
        // 场景绝不能塞进一个已经被清理的持有者
        if (cancelled) {
          disposeScene(gltf.scene)
          return
        }
        sceneRef.current = gltf.scene
        setScene(gltf.scene)
      },
      undefined,
      (error: unknown) => {
        if (cancelled) return
        onFailure(error instanceof Error ? error.message : String(error))
      },
    )

    return () => {
      cancelled = true
      if (sceneRef.current !== null) {
        disposeScene(sceneRef.current)
        sceneRef.current = null
        // 清空已释放的场景：否则新 url 加载失败时视口会继续引用已 dispose 的旧场景
        setScene(null)
      }
    }
  }, [url, onFailure])

  return scene
}
