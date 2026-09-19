import type { CheckResult, ValidationReport } from '../api/geometry'
import { JOB_STAGE_TEXT } from '../statusbar/StatusBar'
import { useModelStore, type ModelError } from '../store/model'
import './MeridianEditor.css'

/** 剖面图的绘图区尺寸（像素）。坐标映射只用于绘图，不参与任何几何计算。 */
const CHART = { width: 260, height: 200, pad: 30 }

const SEVERITY_TEXT: Record<CheckResult['severity'], string> = {
  pass: '通过',
  warn: '警告',
  fail: '不通过',
  skip: '跳过',
}

function scaleX(z: number, totalLength: number): number {
  return CHART.pad + (z / totalLength) * (CHART.width - 2 * CHART.pad)
}

function scaleY(radius: number, maxRadius: number): number {
  return CHART.height - CHART.pad - (radius / maxRadius) * (CHART.height - 2 * CHART.pad)
}

/** 后端轮廓 (r, z) → SVG 折线路径（横轴 = z，纵轴 = r）。只做线性映射。 */
function outlinePath(outline: [number, number][], totalLength: number, maxRadius: number): string {
  return outline
    .map(([radius, z], index) => {
      const command = index === 0 ? 'M' : 'L'
      return `${command} ${scaleX(z, totalLength).toFixed(2)} ${scaleY(radius, maxRadius).toFixed(2)}`
    })
    .join(' ')
}

function readNumber(source: Record<string, unknown> | null, key: string): number | null {
  if (source === null) return null
  const value = source[key]
  return typeof value === 'number' ? value : null
}

function readBoolean(source: Record<string, unknown> | null, key: string): boolean | null {
  if (source === null) return null
  const value = source[key]
  return typeof value === 'boolean' ? value : null
}

function readNumbers(source: Record<string, unknown> | null, key: string): number[] | null {
  if (source === null) return null
  const value = source[key]
  if (!Array.isArray(value)) return null
  return value.every((item) => typeof item === 'number') ? (value as number[]) : null
}

function formatNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toPrecision(6)
}

interface MetricRow {
  label: string
  value: string
}

/** 从后端 `metrics.json` 摘出关键数值；前端只格式化，不推导（ADR-011）。 */
function metricRows(metrics: Record<string, unknown> | null): MetricRow[] {
  const rows: MetricRow[] = []

  const volume = readNumber(metrics, 'volume')
  if (volume !== null) rows.push({ label: '体积 volume（m³）', value: formatNumber(volume) })

  const surfaceArea = readNumber(metrics, 'surface_area')
  if (surfaceArea !== null) {
    rows.push({ label: '表面积 surface_area（m²）', value: formatNumber(surfaceArea) })
  }

  const centroidZ = readNumber(metrics, 'centroid_z')
  if (centroidZ !== null) {
    rows.push({ label: '质心 z（m）', value: formatNumber(centroidZ) })
  }

  const envelope = readNumbers(metrics, 'envelope')
  if (envelope !== null && envelope.length === 3) {
    rows.push({
      label: '解析包络（m）',
      value: `直径 ${formatNumber(envelope[0])} · 长度 ${formatNumber(envelope[2])}`,
    })
  }

  const analyticVolume = readNumber(metrics, 'analytic_volume')
  if (analyticVolume !== null) {
    rows.push({ label: '解析体积（m³）', value: formatNumber(analyticVolume) })
  }

  const volumeError = readNumber(metrics, 'volume_relative_error')
  if (volumeError !== null) {
    // 门禁量：§16.3 要求 < 0.1%，故用科学计数法便于逐位比对
    rows.push({ label: '解析-内核体积相对误差', value: volumeError.toExponential(3) })
  }

  const isValid = readBoolean(metrics, 'is_valid')
  if (isValid !== null) rows.push({ label: 'OCCT 有效', value: isValid ? '是' : '否' })

  const solidCount = readNumber(metrics, 'solid_count')
  if (solidCount !== null) rows.push({ label: '实体数', value: String(solidCount) })

  const bbox = readNumbers(metrics, 'bbox_size')
  if (bbox !== null && bbox.length === 3) {
    rows.push({ label: '内核包围盒（m）', value: bbox.map(formatNumber).join(' × ') })
  }

  return rows
}

function ErrorBlock({ error, onClear }: { error: ModelError | null; onClear: () => void }) {
  if (error === null) return null
  return (
    <section className="panel surface meridian-editor__error">
      <h2 className="panel__title label">{error.origin === 'build' ? '构建错误' : '校验错误'}</h2>
      <p className="meridian-editor__error-message">{`${error.code}（${error.stage}）：${error.message}`}</p>
      <p className="meridian-editor__error-suggestion">{`处理建议：${error.suggestion}`}</p>
      <button type="button" className="meridian-editor__button" onClick={onClear}>
        清除错误
      </button>
    </section>
  )
}

interface ProfileChartProps {
  report: ValidationReport
}

/** 母线剖面图：后端 `outline`（米）→ SVG 折线，附轴线、刻度与接头标记（§11.3 / §16.3）。 */
function ProfileChart({ report }: ProfileChartProps) {
  const totalLength = report.envelope[2]
  const maxRadius = report.max_radius

  if (totalLength <= 0 || maxRadius <= 0) {
    return <p className="meridian-editor__hint label">后端返回的包络为零，无法绘制剖面</p>
  }

  const axisY = scaleY(0, maxRadius)

  return (
    <svg
      className="meridian-editor__chart"
      viewBox={`0 0 ${CHART.width} ${CHART.height}`}
      role="img"
      aria-label="母线剖面图（横轴为轴向位置，纵轴为半径）"
    >
      <line
        className="meridian-editor__axis"
        x1={CHART.pad}
        y1={axisY}
        x2={CHART.width - CHART.pad}
        y2={axisY}
      />
      <path className="meridian-editor__outline" d={outlinePath(report.outline, totalLength, maxRadius)} />

      {report.joints.map((joint) => (
        <circle
          key={`joint-${joint.index}-${joint.kind}`}
          className={`meridian-editor__joint meridian-editor__joint--${joint.severity}`}
          cx={scaleX(joint.z, totalLength)}
          cy={scaleY(joint.radius, maxRadius)}
          r={3}
        >
          <title>
            {`接头 #${joint.index}（${joint.kind}）：z=${formatNumber(joint.z)} m，r=${formatNumber(
              joint.radius,
            )} m，切向夹角 ${joint.angle_deg.toFixed(3)}°`}
          </title>
        </circle>
      ))}

      <text className="meridian-editor__tick" x={CHART.pad} y={CHART.height - 8}>
        {`z 0 m`}
      </text>
      <text className="meridian-editor__tick" x={CHART.width - CHART.pad} y={CHART.height - 8}>
        {`z ${formatNumber(totalLength)} m`}
      </text>
      <text className="meridian-editor__tick" x={CHART.pad} y={CHART.pad - 10}>
        {`r ${formatNumber(maxRadius)} m`}
      </text>
    </svg>
  )
}

/** 右栏：母线编辑器（2D 剖面 + 校验结果 + 构建区）——M1 的 2D→3D 入口（§5.2 / §16.3）。 */
export function MeridianEditor() {
  const report = useModelStore((state) => state.report)
  const validating = useModelStore((state) => state.validating)
  const error = useModelStore((state) => state.error)
  const building = useModelStore((state) => state.building)
  const job = useModelStore((state) => state.job)
  const metrics = useModelStore((state) => state.metrics)
  const startBuild = useModelStore((state) => state.startBuild)
  const clearError = useModelStore((state) => state.clearError)

  const errorBlock = <ErrorBlock error={error} onClear={clearError} />

  if (report === null) {
    return (
      <div className="meridian-editor">
        <section className="panel surface">
          <h2 className="panel__title label">母线编辑器</h2>
          <p className="meridian-editor__loading">
            {validating ? '正在校验母线…' : '正在请求后端采样数据…'}
          </p>
        </section>
        {errorBlock}
      </div>
    )
  }

  const rows = metricRows(metrics)
  const progressPercent = job === null ? 0 : Math.round(job.progress * 100)

  return (
    <div className="meridian-editor">
      <section className="panel surface">
        <h2 className="panel__title label">母线剖面（后端采样）</h2>
        <ProfileChart report={report} />
        <p className="meridian-editor__hint label">
          {`包络 Ø${formatNumber(report.envelope[0])} m × ${formatNumber(report.total_length)} m · ` +
            `解析体积 ${formatNumber(report.volume)} m³`}
        </p>
      </section>

      <section className="panel surface">
        <h2 className="panel__title label">校验结果</h2>
        <ul className="meridian-editor__checks">
          {report.checks.map((check) => (
            <li
              key={check.check}
              className={`meridian-editor__check meridian-editor__check--${check.severity}`}
            >
              <span className="meridian-editor__check-head">
                <span className="meridian-editor__check-name">{check.check}</span>
                <span className="meridian-editor__check-severity">{SEVERITY_TEXT[check.severity]}</span>
              </span>
              <span className="meridian-editor__check-detail">{check.detail}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="panel surface">
        <h2 className="panel__title label">构建</h2>
        <button
          type="button"
          className="meridian-editor__button meridian-editor__button--primary"
          disabled={building}
          onClick={() => {
            void startBuild()
          }}
        >
          {building ? '构建中…' : '构建几何'}
        </button>

        {building ? (
          <div className="meridian-editor__progress">
            <p className="meridian-editor__hint label">
              {`当前阶段：${job === null ? JOB_STAGE_TEXT.queued : JOB_STAGE_TEXT[job.stage]}（${progressPercent}%）`}
            </p>
            <div className="meridian-editor__progress-track">
              <div
                className="meridian-editor__progress-fill"
                style={{ width: `${progressPercent}%` }}
              />
            </div>
          </div>
        ) : null}

        {rows.length > 0 ? (
          <dl className="meridian-editor__metrics">
            {rows.map((row) => (
              <div key={row.label} className="meridian-editor__metric">
                <dt className="label">{row.label}</dt>
                <dd className="num">{row.value}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p className="meridian-editor__hint label">尚无构建产物指标；点击"构建几何"后显示。</p>
        )}
      </section>

      {errorBlock}
    </div>
  )
}
