/**
 * PNG 导出（OI-26，规格 §11.12 行 2038 的分辨率口径）。
 *
 * 纯前端渲染：SVG 序列化后按倍率栅格化；WebGL canvas 等一帧 rAF 后按**实际像素**
 * 尺寸 toBlob。三条红线（行 2038）：
 *
 * 1. 分辨率倍率 = `devicePixelRatio` 的**整数倍**（至少 2×）——`pngExportScale()` 是
 *    唯一口径，SVG 栅格化与 3D 视口重渲共用；
 * 2. **禁止 CSS 拉伸**：导出尺寸只取实际像素（canvas backing store / SVG 布局像素），
 *    绝不把 1× 位图放大到 2× 画布上——那是"先放大再截图"的伪高清；
 * 3. WebGL 截取前**等一帧 requestAnimationFrame**——避免截到未完成的一帧（中间态）。
 */

/** 导出分辨率倍率：devicePixelRatio 向上取整的整数倍，至少 2×（OI-26 行 2038）。 */
export function pngExportScale(): number {
  const ratio = typeof window === 'undefined' ? 1 : window.devicePixelRatio || 1
  return Math.max(2, Math.ceil(ratio))
}

/** 等待一帧 requestAnimationFrame（WebGL 截取前必须等一帧，OI-26 行 2038）。 */
export function nextAnimationFrame(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => resolve())
  })
}

/** 导出文件名：`{型号}-{YYYYMMDD-HHmmss}.png`——含型号与时间戳（OI-26）。 */
export function pngFileName(baseName: string, now: Date = new Date()): string {
  // 型号名清掉路径不安全字符与空白（用户可输入任意名字）
  const safe =
    baseName
      .trim()
      .replace(/[\\/:*?"<>|\s]+/g, '-')
      .replace(/^-+|-+$/g, '') || 'aeroforge'
  const pad = (value: number): string => String(value).padStart(2, '0')
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(
    now.getHours(),
  )}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  return `${safe}-${stamp}.png`
}

/** 触发浏览器下载（blob URL + 锚点点击；报告产物下载与 PNG 共用本函数）。 */
export function downloadBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = fileName
  anchor.click()
  // Chromium（桌面壳 WebView2）在 click 返回时已完成下载快照，可立即回收 blob URL
  URL.revokeObjectURL(url)
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob === null) reject(new Error('PNG 编码失败（canvas.toBlob 返回空）'))
      else resolve(blob)
    }, 'image/png')
  })
}

/**
 * 3D 视口快照：等一帧 rAF 后按 canvas **实际像素尺寸**导出（OI-26）。
 *
 * ⚠ 调用方须先让 canvas 以 `pngExportScale()` 倍率渲染（见 `Viewport` 的导出处理器：
 * 临时提升 renderer pixelRatio 并同步重渲一帧）——本函数不读布局尺寸、不做任何放大；
 * 从 1× 缓冲"先截后放"正是被禁止的伪高清。文件名含型号与时间戳。
 */
export async function exportCanvasPng(canvas: HTMLCanvasElement, baseName: string): Promise<void> {
  await nextAnimationFrame()
  const blob = await canvasToBlob(canvas)
  downloadBlob(blob, pngFileName(baseName))
}

/** SVG 栅格化时需要内联的样式属性——serialize 不携带外部 CSS，缺省 fill 会渲染成黑色。 */
const SVG_STYLE_PROPERTIES = [
  'fill',
  'fill-opacity',
  'fill-rule',
  'stroke',
  'stroke-width',
  'stroke-dasharray',
  'stroke-opacity',
  'stroke-linecap',
  'font-size',
  'font-family',
  'font-weight',
  'opacity',
] as const

/** 取导出基准尺寸：布局像素优先（「视图当前像素尺寸」，OI-26），退回 viewBox 逻辑尺寸。 */
function svgExportSize(svg: SVGSVGElement): { width: number; height: number } {
  const rect = svg.getBoundingClientRect()
  if (rect.width > 0 && rect.height > 0) {
    return { width: rect.width, height: rect.height }
  }
  const viewBox = svg.viewBox.baseVal
  if (viewBox.width > 0 && viewBox.height > 0) {
    return { width: viewBox.width, height: viewBox.height }
  }
  return { width: 300, height: 150 }
}

/** 序列化 SVG（克隆后把计算样式内联进去：外部样式表不随 serialize 走）。 */
function serializeSvg(svg: SVGSVGElement): string {
  const clone = svg.cloneNode(true) as SVGSVGElement
  const originals = [svg as Element, ...svg.querySelectorAll('*')]
  const copies = [clone as Element, ...clone.querySelectorAll('*')]
  // 克隆树与原树同构同序：按位配对，把原节点（在文档内）的计算样式抄到克隆上
  originals.forEach((element, index) => {
    // 本函数只处理 SVG 树（SVGElement 均持有 style）；断言写在这里比放宽入参类型诚实
    const copy = copies[index] as SVGSVGElement
    if (copy === undefined || copy.style === undefined) return
    const computed = window.getComputedStyle(element)
    for (const property of SVG_STYLE_PROPERTIES) {
      const value = computed.getPropertyValue(property)
      if (value !== '') copy.style.setProperty(property, value)
    }
  })
  return new XMLSerializer().serializeToString(clone)
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => resolve(image)
    image.onerror = () => reject(new Error('SVG 位图化失败（图像解码错误）'))
    image.src = url
  })
}

/**
 * 2D 视图（外观图 / 工程剖面图）快照：serialize → 按 `pngExportScale()` 倍率栅格化
 * 到离屏 canvas（矢量重栅格 = 真 2×，非位图拉伸）→ toBlob 下载（OI-26）。
 */
export async function exportSvgPng(svg: SVGSVGElement, baseName: string): Promise<void> {
  const scale = pngExportScale()
  const { width, height } = svgExportSize(svg)

  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(width * scale))
  canvas.height = Math.max(1, Math.round(height * scale))
  const context = canvas.getContext('2d')
  if (context === null) throw new Error('无法创建 2D 绘图上下文（canvas 不可用）')

  // 快照包含当前视图的底色（暗色主题下浅色线条在透明底上几乎不可见）
  const backdrop = window.getComputedStyle(document.body).backgroundColor
  if (backdrop !== '' && backdrop !== 'transparent' && backdrop !== 'rgba(0, 0, 0, 0)') {
    context.fillStyle = backdrop
    context.fillRect(0, 0, canvas.width, canvas.height)
  }

  const blob = new Blob([serializeSvg(svg)], { type: 'image/svg+xml;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  let image: HTMLImageElement
  try {
    image = await loadImage(url)
    context.drawImage(image, 0, 0, canvas.width, canvas.height)
  } finally {
    URL.revokeObjectURL(url)
  }

  const pngBlob = await canvasToBlob(canvas)
  downloadBlob(pngBlob, pngFileName(baseName))
}
