import { useEffect, useState } from 'react'

import { ApiError } from '../api/client'
import { fetchUnits } from '../api/params'
import { toDisplayValue, toUnitTable, unitSymbol, type UnitTable } from '../api/units'
import {
  exportArtifactUrl,
  fetchArtifactBlob,
  fetchVehicleSummary,
  REPORT_EXPORT_FORMATS,
  startReportExport,
  waitForExportJob,
  type VehicleSummaryResponse,
} from '../api/vehicleSummary'
import { downloadBlob } from '../lib/exportPng'
import { usePerfStore } from '../store/perf'
import { useVehicleStore } from '../store/vehicle'
import './VehicleSummaryPanel.css'

/**
 * 右栏「整箭数据 + 轨道运力」面板（规格 §11.5 / FR-10 / FR-11，挂在 2D 视图组下方）。
 *
 * 硬约束：
 *
 * 1. **只格式化不换算**（§11.5）：数值一律来自 `POST /api/vehicle/summary`；总质量的
 *    t 值用后端下发的 `glow_t`（§6.4 在 API 边界换算），其余质量按单位表 factor 显示
 *    （前端零换算系数，与 CalcPanel 同一 `GET /api/params/units` 来源）；
 * 2. **冻结语义复用 OI-04 既有 store**（`store/perf` 的 `autoUpdate`）：关闭 = 停发请求、
 *    保留最后一次结果、常驻「已冻结 / 可能过期」标记；冻结期间输入变更 → 标记升级为
 *    警示态「输入已变更」；重新开启立即按当前输入重算（store 的 `setAutoUpdate` 已含）；
 * 3. **导出报告**（FR-11）：`POST /api/export` 异步作业 → 既有作业通道 → 产物 blob 下载；
 *    缺 provenance 时按钮置灰并说明原因（§11.12 规则 2 / CON-04）。
 */

/** vehicle 变更后的防抖时长（与 2D 视图组 §11.4 同口径，避免逐键请求风暴）。 */
export const SUMMARY_DEBOUNCE_MS = 300

/** 四轨道小表的固定显示顺序（FR-11；行序是排版约定，与数据无关）。 */
const ORBIT_ROWS = ['LEO', 'SSO', 'GTO', 'GEO'] as const

/** 单位表：载入一次并缓存（失败保持空表 = 按 SI 显示，不静默换口径——CalcPanel 同款）。 */
function useUnitTable(): UnitTable {
  const [table, setTable] = useState<UnitTable>({})
  useEffect(() => {
    let alive = true
    fetchUnits()
      .then((response) => {
        if (alive) setTable(toUnitTable(response.units))
      })
      .catch(() => {
        // 单位表拿不到不影响数据展示：回退 SI（kg）
      })
    return () => {
      alive = false
    }
  }, [])
  return table
}

/** kg → 显示单位（§6.4：换算只按后端下发的 factor，前端零换算系数）。 */
function massText(table: UnitTable, kg: number): string {
  return toDisplayValue(table, 'mass', kg).toFixed(1)
}

/** 面板需要展示的错误（§10.3 的 `suggestion` 必填且必须可见）。 */
interface PanelError {
  code: string
  message: string
  suggestion: string
}

function toPanelError(error: unknown): PanelError {
  if (error instanceof ApiError) {
    return { code: error.code, message: error.message, suggestion: error.suggestion }
  }
  return {
    code: 'UNEXPECTED',
    message: error instanceof Error ? error.message : '未知错误',
    suggestion: '确认后端已启动（GET /api/health）后重试；若持续失败请查看后端日志',
  }
}

/** 整箭数据 + 轨道运力面板本体（挂载点 = `views/Workspace` 右栏第三块）。 */
export function VehicleSummaryPanel() {
  const vehicle = useVehicleStore((state) => state.vehicle)
  const autoUpdate = usePerfStore((state) => state.autoUpdate)
  const setAutoUpdate = usePerfStore((state) => state.setAutoUpdate)

  const [summary, setSummary] = useState<VehicleSummaryResponse | null>(null)
  const [failure, setFailure] = useState<PanelError | null>(null)
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState<PanelError | null>(null)
  /** 关闭自动更新瞬间的输入快照（冻结期间「输入已变更」的比对基准，OI-04 规则 3）。 */
  const [frozenInputs, setFrozenInputs] = useState<string | null>(null)
  const unitTable = useUnitTable()

  // 数据获取：挂载 / vehicle 变更后防抖 300 ms 发一次请求；冻结（OI-04）时不发——
  // 参数变更不再触发后端请求，面板保留最后一次结果（§11.5 OI-04 裁决 1/2）。
  // 重新开启自动更新时本 effect 因 autoUpdate 依赖变化而重跑 = 立即按当前输入重算（裁决 4）。
  useEffect(() => {
    if (vehicle === null) {
      setSummary(null)
      setFailure(null)
      return
    }
    if (!autoUpdate) return
    const controller = new AbortController()
    const timer = window.setTimeout(() => {
      fetchVehicleSummary(vehicle, controller.signal)
        .then((response) => {
          if (controller.signal.aborted) return
          setSummary(response)
          setFailure(null)
        })
        .catch((error: unknown) => {
          // 降级不白屏：保留旧数据（若有），提示行可见（2D 视图组同款纪律）
          if (controller.signal.aborted) return
          setFailure(toPanelError(error))
        })
    }, SUMMARY_DEBOUNCE_MS)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [vehicle, autoUpdate])

  // 冻结标记的比对基准：关闭瞬间定格输入快照（OI-04 规则 2/3——重开时清空）
  useEffect(() => {
    if (autoUpdate) {
      setFrozenInputs(null)
      return
    }
    setFrozenInputs(JSON.stringify(useVehicleStore.getState().vehicle))
  }, [autoUpdate])

  /** 冻结期间输入被改过（标记升级为警示态并注明「输入已变更」）。 */
  const inputsChanged =
    !autoUpdate && frozenInputs !== null && JSON.stringify(vehicle) !== frozenInputs

  const massUnit = unitSymbol(unitTable, 'mass') || 'kg'

  /** 导出报告（FR-11）：异步作业 + 既有作业通道 + 产物 blob 下载。 */
  const handleExportReport = (): void => {
    const current = useVehicleStore.getState().vehicle
    if (current === null || exporting) return
    const name = current.name.trim() || 'aeroforge'
    setExporting(true)
    setExportError(null)
    startReportExport(current, REPORT_EXPORT_FORMATS)
      .then((start) =>
        waitForExportJob(start.job_id).then((resultKey) => ({ start, resultKey })),
      )
      .then(({ start, resultKey }) =>
        // 契约：产物文件名由响应 files 映射下发（命名单一事实源在后端），
        // 前端禁止自行拼接（ADR-011 同族纪律）；映射缺失 = 契约破坏，显式报错
        Promise.all(
          REPORT_EXPORT_FORMATS.map(async (format) => {
            const file = start.files[format]
            if (typeof file !== 'string' || file === '') {
              throw new Error(`导出响应缺少 ${format} 的产物文件名映射（契约破坏）`)
            }
            const blob = await fetchArtifactBlob(exportArtifactUrl(resultKey, file))
            downloadBlob(blob, `${name}-${file}`)
          }),
        ),
      )
      .then(() => setExporting(false))
      .catch((error: unknown) => {
        setExporting(false)
        setExportError(toPanelError(error))
      })
  }

  // §11.12 规则 2：报告必须附 provenance（CON-04）；数据未就绪同样不可导出
  const provenanceReady = summary !== null && Object.keys(summary.provenance).length > 0
  const exportDisabled = vehicle === null || exporting || !provenanceReady
  const exportHint = exporting
    ? '导出中…'
    : summary === null
      ? '数据未就绪，暂不可导出'
      : !provenanceReady
        ? '缺少 provenance（CON-04），暂不可导出'
        : null

  if (vehicle === null) {
    return (
      <section className="panel surface vehicle-summary" data-testid="vehicle-summary-panel">
        <h2 className="panel__title label">整箭数据</h2>
        <p className="vehicle-summary__hint label">尚未载入参数，无法获取整箭数据</p>
      </section>
    )
  }

  return (
    <section className="panel surface vehicle-summary" data-testid="vehicle-summary-panel">
      <h2 className="panel__title label">整箭数据</h2>

      {failure !== null ? (
        <p className="vehicle-summary__fallback" data-testid="summary-unavailable">
          {`整箭数据不可用（${failure.code}）：${failure.message}——${failure.suggestion}`}
        </p>
      ) : null}

      {summary === null && failure === null ? (
        <p className="vehicle-summary__hint label">正在请求整箭数据…</p>
      ) : null}

      {summary === null ? null : (
        <>
          {summary.warnings.length > 0 ? (
            <ul className="vehicle-summary__warnings">
              {summary.warnings.map((warning, index) => (
                <li key={`${index}-${warning}`} className="vehicle-summary__warning label">
                  {warning}
                </li>
              ))}
            </ul>
          ) : null}

          {/*
           * FR-10 七字段：总质量 kg+t 双显示（t 值用后端 glow_t，前端不换算）；
           * 其余质量按单位表 factor 显示、长度只格式化（m）。
           */}
          <dl className="vehicle-summary__fields" data-testid="summary-fields">
            <div className="vehicle-summary__field">
              <dt className="label">总质量</dt>
              <dd className="num">{`${summary.glow_kg.toFixed(1)} kg · ${summary.glow_t.toFixed(1)} t`}</dd>
            </div>
            <div className="vehicle-summary__field">
              <dt className="label">推进剂总质量</dt>
              <dd className="num">{`${massText(unitTable, summary.propellant_total_kg)} ${massUnit}`}</dd>
            </div>
            <div className="vehicle-summary__field">
              <dt className="label">干重</dt>
              <dd className="num">{`${massText(unitTable, summary.dry_mass_kg)} ${massUnit}`}</dd>
            </div>
            <div className="vehicle-summary__field">
              <dt className="label">总高度</dt>
              <dd className="num">{`${summary.total_length_m.toFixed(2)} m`}</dd>
            </div>
            <div className="vehicle-summary__field">
              <dt className="label">箭体最大直径</dt>
              <dd className="num">{`${summary.max_diameter_m.toFixed(2)} m`}</dd>
            </div>
            <div className="vehicle-summary__field">
              <dt className="label">整流罩直径</dt>
              <dd className="num">
                {summary.fairing_diameter_m === null
                  ? '—'
                  : `${summary.fairing_diameter_m.toFixed(2)} m`}
              </dd>
            </div>
          </dl>
        </>
      )}

      {/* ---- 轨道运力（FR-11）：当前目标轨道点值 + 四轨道小表 + 自动更新开关 ---- */}
      <div className="vehicle-summary__orbit">
        <div className="vehicle-summary__orbit-head">
          <h3 className="label">轨道运力</h3>
          <label className="vehicle-summary__auto">
            <input
              type="checkbox"
              checked={autoUpdate}
              onChange={(event) => setAutoUpdate(event.target.checked)}
            />
            <span className="label">自动更新</span>
          </label>
        </div>

        {!autoUpdate ? (
          <p
            className={
              inputsChanged
                ? 'vehicle-summary__frozen vehicle-summary__frozen--warn'
                : 'vehicle-summary__frozen'
            }
            data-testid="frozen-marker"
          >
            {inputsChanged ? '已冻结 / 可能过期——输入已变更' : '已冻结 / 可能过期'}
          </p>
        ) : null}

        {summary === null ? (
          <p className="vehicle-summary__hint label">{failure === null ? '等待轨道运力数据…' : '轨道运力暂缺'}</p>
        ) : (
          <>
            <p className="vehicle-summary__target">
              <span className="label">当前目标轨道</span>{' '}
              <span className="num">{summary.orbit.target}</span>
              <span className="vehicle-summary__target-value num">
                {`${massText(unitTable, summary.orbit.payload_kg)} ${massUnit}`}
              </span>
            </p>
            <table className="vehicle-summary__table">
              <tbody>
                {ORBIT_ROWS.map((orbit) => {
                  const row = summary.orbit.payload_by_orbit[orbit]
                  return (
                    <tr key={orbit}>
                      <th scope="row" className="label">
                        {orbit}
                      </th>
                      <td className="num">
                        {row === undefined ? '—' : `${massText(unitTable, row.payload_kg)} ${massUnit}`}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </>
        )}

        <div className="vehicle-summary__export">
          <button
            type="button"
            className="vehicle-summary__button"
            onClick={handleExportReport}
            disabled={exportDisabled}
            data-testid="export-report-button"
          >
            导出报告
          </button>
          {exportHint !== null ? (
            <span className="vehicle-summary__export-hint label">{exportHint}</span>
          ) : null}
          {exportError !== null ? (
            <p className="vehicle-summary__error">
              {`导出失败（${exportError.code}）：${exportError.message}——${exportError.suggestion}`}
            </p>
          ) : null}
        </div>
      </div>
    </section>
  )
}
