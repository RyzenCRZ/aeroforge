import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  exportCanvasPng,
  exportSvgPng,
  pngExportScale,
  pngFileName,
} from './exportPng'

/**
 * PNG 导出门禁（OI-26，§11.12 行 2038 口径）。
 *
 * 钉住的契约：
 *
 * 1. **倍率**：`pngExportScale()` = devicePixelRatio 向上取整的整数倍，**至少 2×**；
 * 2. **等一帧**：canvas 截取必须先等 `requestAnimationFrame` 兑现一帧，再 toBlob——
 *    用「回调未兑现时 toBlob 不得被调」钉住顺序；
 * 3. **尺寸 = 2× 输入**：SVG 栅格化的 canvas 尺寸 = 布局尺寸 × 倍率（矢量重栅格，
 *    非位图拉伸）；drawImage 的目标矩形同尺寸；
 * 4. **文件名含型号与时间戳**；下载经 blob URL + 锚点 click 触发。
 */

/** jsdom 没有 URL.createObjectURL：手动挂替身（用完删除，不污染其它用例）。 */
function stubObjectUrl(): { createObjectURL: ReturnType<typeof vi.fn>; revokeObjectURL: ReturnType<typeof vi.fn> } {
  const createObjectURL = vi.fn(() => 'blob:mock-url')
  const revokeObjectURL = vi.fn()
  Object.defineProperty(URL, 'createObjectURL', {
    value: createObjectURL,
    configurable: true,
    writable: true,
  })
  Object.defineProperty(URL, 'revokeObjectURL', {
    value: revokeObjectURL,
    configurable: true,
    writable: true,
  })
  return { createObjectURL, revokeObjectURL }
}

/** rAF 替身：登记回调但不兑现（用例按步兑现，钉「等一帧」的顺序）。 */
function stubRaf(): { rafSpy: ReturnType<typeof vi.fn>; flush: () => void } {
  let callback: FrameRequestCallback | null = null
  const rafSpy = vi.fn((next: FrameRequestCallback) => {
    callback = next
    return 1
  })
  vi.stubGlobal('requestAnimationFrame', rafSpy)
  return {
    rafSpy,
    flush: () => {
      callback?.(0)
      callback = null
    },
  }
}

beforeEach(() => {
  stubObjectUrl()
  // 锚点 click 是下载动作本身：mock 掉导航副作用，只留调用记录
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  delete (URL as { createObjectURL?: unknown }).createObjectURL
  delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL
})

describe('导出倍率与文件名（OI-26 行 2038）', () => {
  it('pngExportScale = devicePixelRatio 向上取整的整数倍，至少 2×', () => {
    vi.stubGlobal('devicePixelRatio', 1)
    expect(pngExportScale()).toBe(2)
    vi.stubGlobal('devicePixelRatio', 1.5)
    expect(pngExportScale()).toBe(2)
    vi.stubGlobal('devicePixelRatio', 2)
    expect(pngExportScale()).toBe(2)
    vi.stubGlobal('devicePixelRatio', 2.5)
    expect(pngExportScale()).toBe(3)
    vi.stubGlobal('devicePixelRatio', 3)
    expect(pngExportScale()).toBe(3)
  })

  it('文件名含型号与时间戳；型号里的路径不安全字符被清理；空名回退 aeroforge', () => {
    const now = new Date(2026, 8, 21, 15, 30, 5)
    expect(pngFileName('示例箭', now)).toBe('示例箭-20260921-153005.png')
    expect(pngFileName('a/b:c d', now)).toBe('a-b-c-d-20260921-153005.png')
    expect(pngFileName('   ', now)).toBe('aeroforge-20260921-153005.png')
  })
})

describe('exportCanvasPng：等一帧 rAF 后按实际像素 toBlob 下载', () => {
  it('rAF 回调未兑现前不得截取；兑现后 toBlob(image/png) → blob URL 下载（文件名含型号与时间戳）', async () => {
    const { rafSpy, flush } = stubRaf()
    const blob = new Blob(['png-bytes'], { type: 'image/png' })
    const toBlob = vi.fn((callback: BlobCallback, _type?: string) => {
      callback(blob)
    })
    // ⚠ 只给 canvas 本体：函数不得读 clientWidth 之类的布局尺寸（CSS 拉伸来源）
    const canvas = { toBlob } as unknown as HTMLCanvasElement

    let settled: Promise<void> | null = null
    settled = exportCanvasPng(canvas, '示例箭')

    await Promise.resolve() // 微任务排空：rAF 已登记、回调未兑现
    expect(rafSpy).toHaveBeenCalledTimes(1)
    expect(toBlob).not.toHaveBeenCalled()

    flush() // 兑现一帧（OI-26：导出前等一帧，避免截到中间态）
    await settled

    expect(toBlob).toHaveBeenCalledTimes(1)
    expect(toBlob.mock.calls[0]?.[1]).toBe('image/png')
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)

    const click = vi.mocked(HTMLAnchorElement.prototype.click)
    expect(click).toHaveBeenCalledTimes(1)
    const anchor = click.mock.instances[0] as HTMLAnchorElement
    expect(anchor.download).toMatch(/^示例箭-\d{8}-\d{6}\.png$/)
    expect(anchor.href).toContain('blob:')
  })
})

describe('exportSvgPng：serialize → 2× canvas 栅格化 → 下载', () => {
  /** 替身 Image：src 赋值后微任务兑现 onload（与浏览器解码同语义）。 */
  class FakeImage {
    onload: (() => void) | null = null
    onerror: ((event: unknown) => void) | null = null
    private source = ''
    get src(): string {
      return this.source
    }
    set src(value: string) {
      this.source = value
      queueMicrotask(() => this.onload?.())
    }
  }

  function makeSvg(): SVGSVGElement {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    svg.setAttribute('viewBox', '0 0 340 620')
    svg.appendChild(document.createElementNS('http://www.w3.org/2000/svg', 'rect'))
    vi.spyOn(svg, 'getBoundingClientRect').mockReturnValue({
      width: 340,
      height: 620,
    } as DOMRect)
    return svg
  }

  /** canvas 替身：jsdom 无 2D 上下文，mock 掉 getContext / toBlob（尺寸留在实例上）。 */
  function stubCanvas(): {
    canvas: { width: number; height: number }
    drawImage: ReturnType<typeof vi.fn>
  } {
    const drawImage = vi.fn()
    const context = { drawImage, fillRect: vi.fn(), fillStyle: '' } as unknown as CanvasRenderingContext2D
    const pngBlob = new Blob(['png'], { type: 'image/png' })
    const canvas = {
      width: 0,
      height: 0,
      getContext: vi.fn(() => context),
      toBlob: vi.fn((callback: BlobCallback) => callback(pngBlob)),
    }
    const realCreateElement = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string) =>
      tag === 'canvas'
        ? (canvas as unknown as HTMLCanvasElement)
        : realCreateElement(tag)) as typeof document.createElement)
    return { canvas, drawImage }
  }

  it('输出 blob 且 canvas 尺寸 = 2× 输入（dpr=1 → 倍率 2）；drawImage 目标矩形同尺寸；下载被触发', async () => {
    vi.stubGlobal('devicePixelRatio', 1)
    vi.stubGlobal('Image', FakeImage)
    const { canvas, drawImage } = stubCanvas()
    const svg = makeSvg()

    await exportSvgPng(svg, '示例箭')

    // 2× 输入尺寸（OI-26：整数倍、至少 2×；矢量重栅格而非位图拉伸）
    expect(canvas.width).toBe(680)
    expect(canvas.height).toBe(1240)
    expect(drawImage).toHaveBeenCalledWith(expect.any(FakeImage), 0, 0, 680, 1240)

    // toBlob(image/png) → blob URL → 锚点下载；文件名含型号与时间戳
    expect(URL.createObjectURL).toHaveBeenCalled()
    const click = vi.mocked(HTMLAnchorElement.prototype.click)
    expect(click).toHaveBeenCalledTimes(1)
    const anchor = click.mock.instances[0] as HTMLAnchorElement
    expect(anchor.download).toMatch(/^示例箭-\d{8}-\d{6}\.png$/)
  })

  it('dpr=3（整数倍）时按 3× 栅格化', async () => {
    vi.stubGlobal('devicePixelRatio', 3)
    vi.stubGlobal('Image', FakeImage)
    const { canvas } = stubCanvas()

    await exportSvgPng(makeSvg(), '示例箭')

    expect(canvas.width).toBe(1020)
    expect(canvas.height).toBe(1860)
  })
})
