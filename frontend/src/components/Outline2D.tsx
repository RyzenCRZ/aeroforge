import { useState } from 'react'

import type { SectionsResponse } from '../api/sections'
import {
  buildSectionsLayout,
  clampZoom,
  viewBoxText,
  zoomViewBox,
  ZOOM_STEP,
  type Frame,
} from '../lib/profileGeometry'
import './Profile2D.css'

/**
 * 外观图（规格 §11.10 / OI-37）：右栏 2D 视图组的**退化渲染**——只画外轮廓与级段
 * 分界横线，不展示内部条带 / 液面 / 引线标注 / 输送管。
 *
 * 与工程剖面图消费**同一份后端分区数据**（`SectionsResponse`，经同一 `buildSectionsLayout`），
 * 仅渲染层不同——本组件不为外观图另立任何几何来源（ADR-011）。缩放状态**独立**持有。
 */

/** 外观图的绘图区：无需标注留白，只留窄边与助推器车道。 */
const OUTLINE_FRAME: Frame = {
  width: 340,
  height: 620,
  padLeft: 24,
  padRight: 24,
  padTop: 16,
  padBottom: 24,
  boosterLane: 58,
}

interface Outline2DProps {
  data: SectionsResponse
}

export function Outline2D({ data }: Outline2DProps) {
  const [zoom, setZoom] = useState(1)
  const layout = buildSectionsLayout(data, OUTLINE_FRAME)

  if (layout === null) {
    return <p className="profile2d__hint label">后端下发的总长或最大直径为零，无法绘制外观图</p>
  }

  const { mapping, groups, coreGroups } = layout

  return (
    <div className="profile2d__view" data-testid="outline-view">
      <div className="profile2d__head">
        <h3 className="label">外观图</h3>
        <div className="profile2d__toolbar">
          <button
            type="button"
            className="profile2d__button"
            aria-label="放大"
            onClick={() => {
              setZoom((z) => clampZoom(z * ZOOM_STEP))
            }}
          >
            ＋
          </button>
          <button
            type="button"
            className="profile2d__button"
            aria-label="缩小"
            onClick={() => {
              setZoom((z) => clampZoom(z / ZOOM_STEP))
            }}
          >
            －
          </button>
          <button
            type="button"
            className="profile2d__button"
            aria-label="复位"
            onClick={() => {
              setZoom(1)
            }}
          >
            复位
          </button>
        </div>
      </div>

      <svg
        data-testid="outline-svg"
        className="profile2d__svg"
        viewBox={viewBoxText(zoomViewBox(OUTLINE_FRAME, zoom))}
        role="img"
        aria-label="外观图：外轮廓与级段分界"
      >
        {/* 外轮廓 = 逐条带的空心矩形（退化渲染：同 band 数据，不画液体 / 标注 / 管道） */}
        {groups.flatMap((group) =>
          group.bands.map((box) => (
            <rect
              key={`outline-band-${box.index}`}
              data-testid={`outline-band-${box.index}`}
              data-band-section={box.band.section}
              x={box.axisX - box.halfW}
              y={box.y}
              width={box.halfW * 2}
              height={box.h}
              className="profile2d__outline-band"
            />
          )),
        )}

        {/* 级段分界横线：n 个芯级 → n−1 条内部线（§11.10 外观图口径） */}
        {coreGroups.slice(0, -1).map((group, k) => {
          const next = coreGroups[k + 1]
          if (next === undefined) return null
          return (
            <line
              key={`outline-boundary-${group.key}`}
              data-testid={`outline-stage-boundary-${k}`}
              x1={mapping.bodyLeft}
              y1={next.topY}
              x2={mapping.bodyRight}
              y2={next.topY}
              className="profile2d__stage-boundary"
            />
          )
        })}
      </svg>
    </div>
  )
}
