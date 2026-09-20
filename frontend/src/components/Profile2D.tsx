import { useEffect, useState } from 'react'

import { fetchSections, type SectionBand, type SectionsResponse } from '../api/sections'
import {
  buildSectionsLayout,
  clampZoom,
  liquidLayout,
  viewBoxText,
  zoomViewBox,
  ZOOM_STEP,
  type Frame,
  type LayoutBand,
  type LayoutGroup,
} from '../lib/profileGeometry'
import { useVehicleStore } from '../store/vehicle'
import { Outline2D } from './Outline2D'
import './Profile2D.css'

/**
 * 右栏 2D 视图组（规格 §11.10 / OI-37）：**外观图 + 工程剖面图**纵向排布，
 * 消费同一份后端分区数据（`POST /api/geometry/sections`）。
 *
 * - 数据获取：挂载 / vehicle 变更后**防抖 300 ms**发一次请求（与 §11.4 防抖口径一致，
 *   避免逐键请求风暴）；失败降级为一行「剖面数据不可用」，**不得白屏**。
 * - 纯渲染纪律（ADR-011）：条带高度、液面、共底缩减量、尺寸数值**全部来自后端**；
 *   本文件只做排版映射（映射函数见 `../lib/profileGeometry`），无任何物理公式。
 * - 两图各自持有缩放状态（[＋] / [－] / [复位]），交互互不影响（§11.10 视图组）。
 */

/** §11.4 同口径：vehicle 变更后的防抖时长（ms）。 */
export const SECTIONS_DEBOUNCE_MS = 300

/**
 * 工程剖面图的绘图区（SVG 视口）：左留白放尺寸线与标注，右留白放引线标注，
 * 右侧再留一条助推器车道（并联构型 OI-36）。纯排版常量，与物理量无关。
 */
const PROFILE_FRAME: Frame = {
  width: 340,
  height: 620,
  padLeft: 96,
  padRight: 104,
  padTop: 18,
  padBottom: 34,
  boosterLane: 58,
}

/** 推进剂编码色 → 既有语义 token（§11.2 / tokens.css）；组件不写色值字面量（NFR-05）。 */
const PROP_COLOR_VARS: Record<string, string> = {
  lox: 'var(--color-prop-lox)',
  ch4: 'var(--color-prop-ch4)',
  kero: 'var(--color-prop-kero)',
  lh2: 'var(--color-prop-lh2)',
}

/**
 * `color_key` 由后端下发（token 名）；未提供或未知名时退中性色——
 * 颜色不是唯一编码，段名文字始终可读（§11.10 可访问性）。
 */
function propColorOf(band: SectionBand): string {
  const key = band.color_key?.trim().toLowerCase() ?? ''
  return PROP_COLOR_VARS[key] ?? 'var(--color-text-secondary)'
}

interface BandViewProps {
  box: LayoutBand
  scale: number
  labelsX: number
}

/** 一条分区条带：矩形 + 细实线边界 + 右侧引线标注；贮箱条带另画液体 / 气枕。 */
function BandView({ box, scale, labelsX }: BandViewProps) {
  const { band, index, y, h, axisX, halfW } = box
  const x = axisX - halfW
  const midY = y + h / 2
  const isTank = band.section === 'ox_tank' || band.section === 'fuel_tank'
  const isBulkhead = band.section === 'common_bulkhead'
  const liquid =
    band.liquid_level_m === undefined || band.liquid_level_m === null
      ? null
      : liquidLayout(band.length_m, band.liquid_level_m, scale)
  // 右侧标注：共底标级长缩减量（§11.10），其余条带标段名
  const rightText =
    isBulkhead && band.bulkhead_saving_m != null
      ? `共底节省 ${band.bulkhead_saving_m.toFixed(2)} m`
      : band.label_zh

  return (
    <g data-testid={`profile-band-${index}`} data-band-section={band.section}>
      <rect
        x={x}
        y={y}
        width={halfW * 2}
        height={h}
        className={isTank ? 'profile2d__band' : 'profile2d__band profile2d__band--structure'}
      />

      {/* 液体占条带底部 liquid_level_m（§6.1 液面高度口径 = 自箱底向上），
          其上为同色 15% 透明度气枕（§11.10）——全部是后端米制量的线性映射 */}
      {liquid === null ? null : (
        <>
          <rect
            data-testid={`profile-ullage-${index}`}
            x={x}
            y={y}
            width={halfW * 2}
            height={liquid.ullage}
            style={{ fill: propColorOf(band) }}
            fillOpacity={0.15}
          />
          <rect
            data-testid={`profile-liquid-${index}`}
            x={x}
            y={y + liquid.ullage}
            width={halfW * 2}
            height={liquid.liquid}
            style={{ fill: propColorOf(band) }}
          />
        </>
      )}

      {/* 共底：两箱**共用**隔板画单线（§11.10；非共底的相邻边界由条带矩形描边自然成对） */}
      {isBulkhead ? (
        <line
          data-testid={`profile-bulkhead-line-${index}`}
          x1={x}
          y1={midY}
          x2={x + halfW * 2}
          y2={midY}
          className="profile2d__bulkhead"
        />
      ) : null}

      {/* 隔热层：斜线填充（§5.9 口径 2），按后端 insulation 字段寻址 */}
      {band.insulation === true ? (
        <rect
          data-testid={`profile-insulation-${index}`}
          x={x}
          y={y}
          width={halfW * 2}
          height={h}
          fill="url(#profile2d-hatch)"
          className="profile2d__insulation"
        />
      ) : null}

      {/* 发动机舱轮廓：契约未下发包络宽度，呈现层取体宽的固定绘制比例（排版约定，非物理） */}
      {band.section === 'engine_bay' ? (
        <rect
          data-testid={`profile-engine-envelope-${index}`}
          x={axisX - halfW * 0.66}
          y={y}
          width={halfW * 2 * 0.66}
          height={h}
          className="profile2d__engine"
        />
      ) : null}

      {/* 段名以引线标注在右侧（§11.10 绘制规范表） */}
      <line x1={x + halfW * 2} y1={midY} x2={labelsX - 4} y2={midY} className="profile2d__leader" />
      <text x={labelsX} y={midY + 3} className="profile2d__band-label">
        {rightText}
      </text>
    </g>
  )
}

/** 输送管道：只画几何走法，不画流阻 / 流量（§5.9 口径 3）。 */
function PipeLine({ group }: { group: LayoutGroup }) {
  if (group.routing === 'external') {
    // external：沿箭体外侧平行于轴线
    const x = group.axisX + group.halfW + 5
    return (
      <line
        data-testid={`profile-pipe-external-${group.key}`}
        x1={x}
        y1={group.topY}
        x2={x}
        y2={group.bottomY}
        className="profile2d__pipe"
      />
    )
  }
  // internal：上箱输送管穿下箱内部——自上箱顶贯到下箱底
  const tanks = group.bands.filter(
    (box) => box.band.section === 'ox_tank' || box.band.section === 'fuel_tank',
  )
  const upper = tanks[0]
  const lower = tanks[tanks.length - 1]
  const y1 = upper === undefined ? group.topY : upper.y
  const y2 = lower === undefined ? group.bottomY : lower.y + lower.h
  const x = group.axisX + group.halfW * 0.35
  return (
    <line
      data-testid={`profile-pipe-internal-${group.key}`}
      x1={x}
      y1={y1}
      x2={x}
      y2={y2}
      className="profile2d__pipe"
    />
  )
}

/** 工程剖面图（视图组之一）：分区条带 + 液面 + 共底 + 管道 + 尺寸标注。 */
function EngineeringProfile({ data }: { data: SectionsResponse }) {
  const [zoom, setZoom] = useState(1)
  const layout = buildSectionsLayout(data, PROFILE_FRAME)

  if (layout === null) {
    return <p className="profile2d__hint label">后端下发的总长或最大直径为零，无法绘制工程剖面图</p>
  }

  const { mapping, groups } = layout
  const bodyTop = mapping.noseY
  const bodyBottom = mapping.noseY + data.dimensions.total_length_m * mapping.scale
  const bodyLeft = mapping.axisX - mapping.bodyHalfW
  const bodyRight = mapping.axisX + mapping.bodyHalfW
  const labelsX = Math.max(...groups.map((group) => group.axisX + group.halfW)) + 12
  const dimX = PROFILE_FRAME.padLeft - 34
  const dimY = bodyBottom + 16

  return (
    <div className="profile2d__view" data-testid="profile-view">
      <div className="profile2d__head">
        <h3 className="label">工程剖面图</h3>
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
        data-testid="profile-svg"
        className="profile2d__svg"
        viewBox={viewBoxText(zoomViewBox(PROFILE_FRAME, zoom))}
        role="img"
        aria-label="工程剖面图：分区条带、液面、共底、输送管与尺寸标注"
      >
        <defs>
          <pattern
            id="profile2d-hatch"
            width={6}
            height={6}
            patternUnits="userSpaceOnUse"
            patternTransform="rotate(45)"
          >
            <line x1={0} y1={0} x2={0} y2={6} className="profile2d__hatch-line" />
          </pattern>
        </defs>

        {/* 尺寸线（固定成分，不因缩放或空间不足省略，§5.9 共性 7）：左侧 = 总长，底部 = 最大直径 */}
        <g>
          <line x1={dimX} y1={bodyTop} x2={dimX} y2={bodyBottom} className="profile2d__dim-line" />
          <line x1={dimX - 4} y1={bodyTop} x2={dimX + 4} y2={bodyTop} className="profile2d__dim-line" />
          <line
            x1={dimX - 4}
            y1={bodyBottom}
            x2={dimX + 4}
            y2={bodyBottom}
            className="profile2d__dim-line"
          />
          <line x1={bodyLeft} y1={dimY} x2={bodyRight} y2={dimY} className="profile2d__dim-line" />
          <line x1={bodyLeft} y1={dimY - 4} x2={bodyLeft} y2={dimY + 4} className="profile2d__dim-line" />
          <line
            x1={bodyRight}
            y1={dimY - 4}
            x2={bodyRight}
            y2={dimY + 4}
            className="profile2d__dim-line"
          />
        </g>

        {/* 尺寸标注：labels 全部渲染、不省略（§5.9 共性 7）；数值与措辞都来自后端（ADR-011）。
            labels 未带锚点，沿左留白自上而下排开（排版约定）。 */}
        {data.dimensions.labels.map((label, i) => (
          <text
            key={label.key}
            data-testid={`profile-dim-${label.key}`}
            x={4}
            y={bodyTop + 10 + i * 12}
            className="profile2d__dim-text"
          >
            {label.text}
          </text>
        ))}

        {/* 输送管走法：每级一条，读 delivery_pipe_routing 字段（external / internal） */}
        {groups.map((group) => (
          <PipeLine key={`pipe-${group.key}`} group={group} />
        ))}

        {/* 分区条带：自上而下逐段，段高 ∝ 后端 length_m（读数据，不硬编码） */}
        {groups.flatMap((group) =>
          group.bands.map((box) => (
            <BandView
              key={`band-${box.index}`}
              box={box}
              scale={mapping.scale}
              labelsX={labelsX}
            />
          )),
        )}
      </svg>
    </div>
  )
}

/**
 * 右栏 2D 视图组容器：负责数据获取与降级，向下分发同一份 `SectionsResponse`。
 * 挂载点 = 右栏（`views/Workspace.tsx` 第三栏）。
 */
export function Profile2D() {
  const vehicle = useVehicleStore((state) => state.vehicle)
  const [data, setData] = useState<SectionsResponse | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    if (vehicle === null) {
      setData(null)
      setFailed(false)
      return
    }
    const controller = new AbortController()
    // 防抖 300 ms：vehicle 连续变更只发最后一次（与 §11.4 防抖口径一致）
    const timer = window.setTimeout(() => {
      fetchSections(vehicle, controller.signal)
        .then((response) => {
          if (controller.signal.aborted) return
          setData(response)
          setFailed(false)
        })
        .catch(() => {
          // 降级不白屏：保留旧数据（若有），提示行可见（材料下拉同款纪律）
          if (controller.signal.aborted) return
          setFailed(true)
        })
    }, SECTIONS_DEBOUNCE_MS)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [vehicle])

  if (vehicle === null) {
    return (
      <section className="panel surface profile2d">
        <h2 className="panel__title label">2D 视图组</h2>
        <p className="profile2d__hint label">尚未载入参数，无法绘制外观图与工程剖面图</p>
      </section>
    )
  }

  return (
    <section className="panel surface profile2d" data-testid="profile2d-panel">
      <h2 className="panel__title label">2D 视图组（外观 + 剖面）</h2>

      {failed ? (
        <p className="profile2d__fallback label" data-testid="sections-unavailable">
          剖面数据不可用（POST /api/geometry/sections 未返回）——两张 2D 视图暂缺，其余面板不受影响。
        </p>
      ) : null}

      {data !== null && data.warnings.length > 0 ? (
        <ul className="profile2d__warnings">
          {data.warnings.map((warning, index) => (
            <li key={`${index}-${warning}`} className="profile2d__warning label">
              {warning}
            </li>
          ))}
        </ul>
      ) : null}

      {data === null && !failed ? <p className="profile2d__hint label">正在请求分区数据…</p> : null}

      {data === null ? null : (
        <>
          <Outline2D data={data} />
          <EngineeringProfile data={data} />
        </>
      )}
    </section>
  )
}
