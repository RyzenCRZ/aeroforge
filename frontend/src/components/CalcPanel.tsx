import { useEffect, useState, type ReactNode } from 'react'

import { fetchUnits } from '../api/params'
import { toDisplayValue, toUnitTable, unitSymbol, type UnitTable } from '../api/units'
import type { McResult, PerfEvaluateResponse } from '../api/perf'
import { usePerfStore } from '../store/perf'
import './CalcPanel.css'

/**
 * 底部计算评估面板（规格 §11.5 ③ 计算评估流）。
 *
 * 形态硬约束：
 *
 * 1. **底部弹出、不遮挡 3D 视口**：面板挂在布局流内（`app` 列 flex 的 main 与状态栏
 *    之间），弹出让工作区收缩而非覆盖——结构上不可能遮住中栏。
 * 2. **两阶段 UI（OI-25 核心验收）**：`interval_pending=true` 时区间列显示**占位态**
 *    （脉动样式，不显示旧数字）；MC 结果到达后**整体覆盖**为 P5–P95（含 P50）；
 *    未到达前不得新旧混排（store 在每次新评估发起时即清空旧区间）。
 * 3. **ΔV 瀑布逐项显示**（OI-23）：理想 → 四损失 → 自转加成 → 总，7 个数值逐项列出，
 *    并渲染 `assumptions` 文字（"允许简化但必须标明假设"）。
 * 4. **数值一律来自后端**（ADR-011）：条形宽度只是排版（|值| 按比例），不是计算。
 */

/** 四轨道点值运力表（OI-38）：LEO / SSO / GTO / GEO 直送四目标，固定显示顺序。 */
const ORBIT_ROWS = [
  { key: 'leo_kg', label: 'LEO' },
  { key: 'sso_kg', label: 'SSO' },
  { key: 'gto_kg', label: 'GTO' },
  { key: 'geo_kg', label: 'GEO' },
] as const

/** 区间 / 偏度提示条覆盖的量（键与 §8.7 `interval` / `moments` 同名）。 */
const MOMENT_LABELS: Record<string, string> = {
  leo_kg: 'LEO 运力',
  sso_kg: 'SSO 运力',
  gto_kg: 'GTO 运力',
  geo_kg: 'GEO 运力',
  glow_kg: '起飞质量 GLOW',
}

/** ΔV 瀑布（OI-23）：理想 → 四损失 → 自转加成（可为负，向西发射为罚项）→ 总。 */
const WATERFALL_ROWS = [
  { key: 'ideal_dv_km_s', label: '理想 ΔV', tone: 'base' },
  { key: 'gravity_loss_km_s', label: '重力损失', tone: 'loss' },
  { key: 'aero_loss_km_s', label: '气动损失', tone: 'loss' },
  { key: 'steering_loss_km_s', label: '转向损失', tone: 'loss' },
  { key: 'back_pressure_loss_km_s', label: '背压损失', tone: 'loss' },
  { key: 'rotation_assist_km_s', label: '自转加成', tone: 'gain' },
  { key: 'total_dv_km_s', label: '总 ΔV', tone: 'total' },
] as const

/** 单位表：载入一次并缓存（失败保持空表 = 按 SI 显示并说明，不静默换口径）。 */
function useUnitTable(): UnitTable {
  const [table, setTable] = useState<UnitTable>({})
  useEffect(() => {
    let alive = true
    fetchUnits()
      .then((response) => {
        if (alive) setTable(toUnitTable(response.units))
      })
      .catch(() => {
        // 单位表拿不到不影响计算结果展示：回退 SI 并在面板头部标出。
      })
    return () => {
      alive = false
    }
  }, [])
  return table
}

/** kg → 显示单位（§6.4：换算只按后端下发的 factor，前端零换算系数）。 */
function massText(unitTable: UnitTable, kg: number): string {
  return toDisplayValue(unitTable, 'mass', kg).toFixed(1)
}

/**
 * 区间单元格的两阶段状态机（OI-25）：
 * 错误 → 「区间不可用」；MC 到达 → P5–P95（含 P50）；在途 → 占位脉动；未投递 → —。
 */
function intervalCell(
  unitTable: UnitTable,
  key: string,
  response: PerfEvaluateResponse,
  mc: McResult | null,
  mcError: boolean,
): ReactNode {
  if (mcError) return <span className="calc-panel__interval-na">区间不可用</span>
  if (mc !== null) {
    const row = mc.interval[key]
    if (row === undefined) return '—'
    return (
      <span className="calc-panel__interval">
        {`P5 ${massText(unitTable, row.p5)} – P95 ${massText(unitTable, row.p95)}（P50 ${massText(unitTable, row.p50)}）`}
      </span>
    )
  }
  // 未到达：占位态——绝不显示旧数字（store 已在新评估发起时清空，此处兜底同语义）
  if (response.interval_pending) {
    return <span className="calc-panel__interval-pending">区间计算中…</span>
  }
  return '—'
}

/** 底部计算评估面板本体。 */
export function CalcPanel() {
  const evaluating = usePerfStore((state) => state.evaluating)
  const response = usePerfStore((state) => state.response)
  const evaluateError = usePerfStore((state) => state.evaluateError)
  const mc = usePerfStore((state) => state.mc)
  const mcError = usePerfStore((state) => state.mcError)
  const runEvaluate = usePerfStore((state) => state.runEvaluate)
  const retryMc = usePerfStore((state) => state.retryMc)
  const closePanel = usePerfStore((state) => state.closePanel)
  const unitTable = useUnitTable()

  const unitsReady = Object.keys(unitTable).length > 0
  const massUnit = unitSymbol(unitTable, 'mass') || 'kg'

  return (
    <section className="calc-panel surface" aria-label="计算评估面板">
      <header className="calc-panel__head">
        <h2 className="calc-panel__title label">计算评估</h2>
        {evaluating ? <span className="calc-panel__head-state">点值计算中…</span> : null}
        <span className="calc-panel__unit-hint label">
          {unitsReady
            ? `质量按显示单位（${massUnit}，§6.4）；ΔV 单位 km/s`
            : '单位表未载入，质量按 SI（kg）显示；ΔV 单位 km/s'}
        </span>
        <button type="button" className="calc-panel__button" onClick={runEvaluate}>
          重新计算
        </button>
        <button type="button" className="calc-panel__button" onClick={closePanel}>
          收起面板
        </button>
      </header>

      {evaluateError !== null ? (
        <p className="calc-panel__error">
          {`点值评估失败（${evaluateError.code}）：${evaluateError.message}——${evaluateError.suggestion}`}
        </p>
      ) : null}

      {response === null ? (
        evaluateError === null ? (
          <p className="calc-panel__hint">尚未计算——点击「计算」开始阶段①点值评估。</p>
        ) : null
      ) : (
        <>
          {response.warnings.length > 0 ? (
            <ul className="calc-panel__warnings">
              {response.warnings.map((warning, index) => (
                <li key={`warning-${index}`}>{warning}</li>
              ))}
            </ul>
          ) : null}

          {/*
           * 四轨道点值运力表（OI-38）+ GLOW：点值列 = 阶段①（立即渲染）；区间列 =
           * 两阶段状态机（占位 → 覆盖 / 错误 → 重试）。
           */}
          <table className="calc-panel__table">
            <thead>
              <tr>
                <th className="label">目标</th>
                <th className="label">{`点值（${massUnit}）`}</th>
                <th className="label">{`区间 P5 – P95（P50，${massUnit}）`}</th>
                <th className="label">ΔV 需求（km/s）</th>
                <th className="label">需求来源</th>
              </tr>
            </thead>
            <tbody>
              {ORBIT_ROWS.map(({ key, label }) => {
                const row = response.point.payload_by_orbit[key]
                if (row === undefined) return null
                return (
                  <tr key={key}>
                    <th scope="row" className="label">{label}</th>
                    <td className="num">
                      {massText(unitTable, row.payload_kg)}
                      {row.attainable === false ? (
                        <span className="calc-panel__unattainable">（不可达）</span>
                      ) : null}
                    </td>
                    <td>{intervalCell(unitTable, key, response, mc, mcError !== null)}</td>
                    <td className="num">{row.dv_used_km_s.toFixed(2)}</td>
                    <td className="calc-panel__source">{row.dv_source}</td>
                  </tr>
                )
              })}
              <tr>
                <th scope="row" className="label">起飞质量 GLOW</th>
                <td className="num">{massText(unitTable, response.point.glow_kg)}</td>
                <td>{intervalCell(unitTable, 'glow_kg', response, mc, mcError !== null)}</td>
                <td>—</td>
                <td>—</td>
              </tr>
              <tr>
                <th scope="row" className="label">输入载荷（对照）</th>
                <td className="num">{massText(unitTable, response.point.payload_mass_kg)}</td>
                <td>—</td>
                <td>—</td>
                <td>—</td>
              </tr>
            </tbody>
          </table>

          {mcError !== null ? (
            <p className="calc-panel__error">
              {`MC 区间失败（${mcError.code}）：${mcError.message}——${mcError.suggestion}`}
              <button type="button" className="calc-panel__button" onClick={retryMc}>
                重试区间
              </button>
            </p>
          ) : null}

          {/*
           * 偏度提示条（§8.7 / OI-01）：|G₁|>0.5 时后端给出 mean_vs_p50_note——
           * 此时必须提示「均值 ≠ P50」，不得把 P50 当期望值使用。
           */}
          {mc !== null
            ? Object.entries(mc.moments)
                .filter(([, moments]) => moments.mean_vs_p50_note !== null)
                .map(([key, moments]) => (
                  <p key={`skew-${key}`} className="calc-panel__skew-note">
                    {`⚠ ${MOMENT_LABELS[key] ?? key}：均值 ≠ P50——${moments.mean_vs_p50_note}`}
                  </p>
                ))
            : null}

          {/*
           * ΔV 瀑布（OI-23）：逐项显示 7 个数值（不是只给总和），条宽只是按 |值| 的
           * 排版比例；assumptions 必须渲染（允许简化但必须标明假设）。
           */}
          <div className="calc-panel__waterfall">
            <h3 className="label">{`ΔV 瀑布（${response.delta_v_budget.target_orbit}，km/s）`}</h3>
            {(() => {
              const max = Math.max(
                ...WATERFALL_ROWS.map(({ key }) => Math.abs(response.delta_v_budget[key])),
              )
              return WATERFALL_ROWS.map(({ key, label, tone }) => {
                const value = response.delta_v_budget[key]
                return (
                  <div key={key} className="calc-panel__waterfall-row">
                    <span className="calc-panel__waterfall-label label">{label}</span>
                    <span className="calc-panel__bar-track">
                      <span className={`calc-panel__bar calc-panel__bar--${tone}`} style={{ width: `${(Math.abs(value) / max) * 100}%` }} />
                    </span>
                    <span className="num calc-panel__waterfall-value">{value.toFixed(2)}</span>
                  </div>
                )
              })
            })()}
            <ul className="calc-panel__assumptions">
              {response.delta_v_budget.assumptions.map((assumption, index) => (
                <li key={`assumption-${index}`}>{assumption}</li>
              ))}
            </ul>
          </div>

          {/*
           * 敏感度 Top-8（§8.7，一阶差分非 Sobol）：条宽 = impact（后端已按最大值
           * 归一 ≤ 1），数值与排序照抄后端，前端不重算。
           */}
          {mc !== null && mc.sensitivity.length > 0 ? (
            <div className="calc-panel__sensitivity">
              <h3 className="label">参数敏感度 Top-8（±1σ 一阶差分，按最大归一）</h3>
              {mc.sensitivity.map((item) => (
                <div key={item.param} className="calc-panel__sensitivity-row">
                  <span className="calc-panel__sensitivity-param label">{item.param}</span>
                  <span className="calc-panel__bar-track">
                    <span
                      className="calc-panel__bar calc-panel__bar--gain"
                      style={{ width: `${Math.max(0, Math.min(1, item.impact)) * 100}%` }}
                    />
                  </span>
                  <span className="num calc-panel__waterfall-value">{item.impact.toFixed(2)}</span>
                </div>
              ))}
            </div>
          ) : null}
        </>
      )}
    </section>
  )
}
