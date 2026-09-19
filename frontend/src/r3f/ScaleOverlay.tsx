import type { Channel } from '../store/model'
import './ScaleOverlay.css'

/** 通道名（§11.11 的视口侧文案，与状态栏同源语义）。 */
const CHANNEL_TEXT: Record<Channel, string> = {
  schematic: '示意通道（后端采样）',
  authoritative: '权威通道（GLB）',
}

/**
 * 格式化长度：只做数值 → 字符串的转换，**不做任何换算或推导**（ADR-011）。
 */
function formatMeters(value: number): string {
  return `${value.toFixed(2)} m`
}

interface ScaleOverlayProps {
  /** 后端解析包络 (2R, 2R, L)，单位米；尚未取得校验报告时为 null */
  envelope: [number, number, number] | null
  channel: Channel
  /** 需要常驻提示的异常（如权威 GLB 加载失败已回退示意通道） */
  notice?: string | null
}

/**
 * 视口叠加层（§11.3 FR-07 / §11.11）：包络尺寸标注 + 当前通道名。
 *
 * 数值全部来自后端 `ValidationReport.envelope`，本组件只格式化。
 */
export function ScaleOverlay({ envelope, channel, notice }: ScaleOverlayProps) {
  return (
    <div className="scale-overlay">
      <span className="scale-overlay__channel label">{CHANNEL_TEXT[channel]}</span>
      {envelope !== null ? (
        <span className="scale-overlay__size num">
          {`直径 ${formatMeters(envelope[0])} · 长度 ${formatMeters(envelope[2])}`}
        </span>
      ) : (
        <span className="scale-overlay__size label">等待后端包络…</span>
      )}
      {notice !== null && notice !== undefined ? (
        <span className="scale-overlay__notice">{notice}</span>
      ) : null}
    </div>
  )
}
