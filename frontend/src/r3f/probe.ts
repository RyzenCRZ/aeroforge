/**
 * M0.5 桌面壳探针的基础设施（规格 §16.2 / R-33）。
 *
 * 探针的唯一目的：证明 **WebView2 中 WebGL2 可用**，使 M1 的 3D 视口方案成立。
 * 判定结果写入 `window.__aeroforgeProbe`，由 `desktop/launcher.py` 轮询读取。
 *
 * M1 起由真实视口（`Viewport`）在**首帧渲染时**发布探针——与数据是否已加载无关，
 * 空场景也必须发布，否则启动器只能得到"探针超时"这种无诊断价值的结论。
 */

declare global {
  interface Window {
    __aeroforgeProbe?: {
      /** WebGL2 是否可用 */
      webgl2: boolean
      /** 图形渲染器（UNMASKED_RENDERER_WEBGL，若可取得） */
      renderer: string
      /** 从视口挂载到首帧绘制的毫秒数；WebGL2 不可用时为 -1 */
      rendered_ms: number
    }
  }
}

export interface GlSupport {
  webgl2: boolean
  renderer: string
}

/**
 * 探测 WebGL2 能力。
 *
 * 先探测、再决定是否挂 `<Canvas>`：若 WebGL2 缺失，R3F 建不出上下文，
 * 只会留下白屏与"探针超时"。
 */
export function detectWebGL2(): GlSupport {
  try {
    const canvas = document.createElement('canvas')
    const gl = canvas.getContext('webgl2')
    if (!gl) {
      return { webgl2: false, renderer: '不可用' }
    }
    // 该扩展能拿到真实 GPU 名称，便于排查"软件渲染回退"（性能会显著劣化）。
    const debugInfo = gl.getExtension('WEBGL_debug_renderer_info')
    const renderer = debugInfo
      ? String(gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL))
      : String(gl.getParameter(gl.RENDERER))
    return { webgl2: true, renderer }
  } catch {
    return { webgl2: false, renderer: '探测异常' }
  }
}

/** 发布探针结果（字段形状是 M0.5 门禁的冻结契约，不得增删）。 */
export function publishProbe(support: GlSupport, renderedMs: number): void {
  window.__aeroforgeProbe = {
    webgl2: support.webgl2,
    renderer: support.renderer,
    rendered_ms: renderedMs,
  }
}
