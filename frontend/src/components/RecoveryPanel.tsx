import { useEffect, useRef, useState } from 'react'

import { ApiError } from '../api/client'
import { solveSequence, type SequenceReport } from '../api/sequence'
import { useVehicleStore } from '../store/vehicle'
import './RecoveryPanel.css'

/**
 * 右栏「回收质量代价」面板（§8.9 / M5 验收「回收质量代价可解释」）。
 *
 * 硬约束：
 *
 * 1. **数值一律后端下发**（ADR-011）：三项代价与合计取自 `POST /api/sizing/sequence`
 *    响应的 `recovery`（RecoveryCosts），前端只格式化，绝不自行加总或换算；
 * 2. **三项分解、不得合并**（§8.9 规则 3）：每项 kg + 一句可解释文案；合计行只呈现
 *    后端的 `inert_cost_kg`（惯性代价 = 系统 + 结构加强），预留推进剂是推进剂预算、
 *    后端口径明确**不计入**——前端不得另造一个三项之和；
 * 3. **未启用不隐藏**：回收未启用时面板显示「未启用回收，无代价」（可发现性）；
 *    请求失败降级为提示行（§10.3：code / message / suggestion 可见），不白屏。
 */

/** vehicle / ΔV 变更后的防抖时长（与右栏 2D 视图组、整箭数据面板同口径）。 */
export const SEQUENCE_DEBOUNCE_MS = 300

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

function kgText(kg: number): string {
  return `${kg.toFixed(1)} kg`
}

/** 回收质量代价面板本体（挂载点 = `views/Workspace` 右栏，整箭数据面板之下）。 */
export function RecoveryPanel() {
  const vehicle = useVehicleStore((state) => state.vehicle)

  // 目标总 ΔV 是 sequence 请求体的必填项（§8.9 / sizing 同口径）：面板内草稿字符串，
  // 提交时解析——「0.」之类的中间态不得退化成 0 发出去（NumberField 同一教训）。
  const [deltaVDraft, setDeltaVDraft] = useState('')
  const deltaV = Number(deltaVDraft)
  const deltaVValid = deltaVDraft.trim() !== '' && Number.isFinite(deltaV) && deltaV > 0

  const recoveryEnabled = vehicle?.recovery?.enabled === true

  const [report, setReport] = useState<SequenceReport | null>(null)
  const [failure, setFailure] = useState<PanelError | null>(null)
  /** 请求代数：新请求发起即递增，迟到响应按它作废（不覆盖新结果）。 */
  const requestToken = useRef(0)

  useEffect(() => {
    if (vehicle === null || !recoveryEnabled || !deltaVValid) {
      requestToken.current += 1
      setReport(null)
      setFailure(null)
      return
    }
    const token = ++requestToken.current
    const timer = window.setTimeout(() => {
      solveSequence(vehicle, deltaV)
        .then((response) => {
          if (token !== requestToken.current) return
          setReport(response)
          setFailure(null)
        })
        .catch((error: unknown) => {
          if (token !== requestToken.current) return
          setFailure(toPanelError(error))
        })
    }, SEQUENCE_DEBOUNCE_MS)
    return () => {
      window.clearTimeout(timer)
    }
  }, [vehicle, recoveryEnabled, deltaVValid, deltaV])

  const costs = report?.recovery ?? null

  return (
    <section className="panel surface recovery-panel" data-testid="recovery-panel">
      <h2 className="panel__title label">回收质量代价</h2>

      {vehicle === null ? (
        <p className="recovery-panel__hint label">尚未载入参数，无法计算回收代价</p>
      ) : !recoveryEnabled ? (
        <>
          <p className="recovery-panel__empty label" data-testid="recovery-disabled">
            未启用回收，无代价
          </p>
          <p className="recovery-panel__hint label">
            在左栏「参数面板 → 回收（recovery）」启用后，此处给出回收点火 / 结构加强 / 回收系统
            三项代价的分解与解释（§8.9）。
          </p>
        </>
      ) : (
        <>
          <label className="recovery-panel__field">
            <span className="label">目标总 ΔV（m/s，真空口径）</span>
            <input
              type="number"
              className="num"
              step={100}
              min={0}
              placeholder="必填，如 9200"
              value={deltaVDraft}
              onChange={(event) => setDeltaVDraft(event.target.value)}
            />
          </label>

          {!deltaVValid ? (
            <p className="recovery-panel__hint label">
              输入目标总 ΔV 后自动计算（POST /api/sizing/sequence，§8.9）。
            </p>
          ) : failure !== null ? (
            <p className="recovery-panel__error" data-testid="recovery-error">
              {`回收代价不可用（${failure.code}）：${failure.message}——${failure.suggestion}`}
            </p>
          ) : report === null ? (
            <p className="recovery-panel__hint label" data-testid="recovery-loading">
              正在请求回收代价…
            </p>
          ) : costs === null ? (
            <p className="recovery-panel__empty label">未启用回收，无代价</p>
          ) : (
            <>
              <dl className="recovery-panel__costs" data-testid="recovery-costs">
                <div className="recovery-panel__cost">
                  <dt className="label">
                    回收系统质量
                    <span className="recovery-panel__explain">
                      回收系统本体（伞降 / 反推硬件等外挂设备）——随箭体上升的惯性质量
                    </span>
                  </dt>
                  <dd className="num">{kgText(costs.system_mass_kg)}</dd>
                </div>
                <div className="recovery-panel__cost">
                  <dt className="label">
                    结构加强质量
                    <span className="recovery-panel__explain">
                      增强结构与热防护——再入加固的惯性代价（§8.9 规则 3 第三项）
                    </span>
                  </dt>
                  <dd className="num">{kgText(costs.reinforcement_mass_kg)}</dd>
                </div>
                <div className="recovery-panel__cost">
                  <dt className="label">
                    预留推进剂
                    <span className="recovery-panel__explain">
                      {`回收点火消耗的预留推进剂（份额 ${costs.landing_propellant_margin_fraction} × 满装量），直接减运力`}
                    </span>
                  </dt>
                  <dd className="num">{kgText(costs.landing_propellant_kg)}</dd>
                </div>
                <div className="recovery-panel__cost recovery-panel__cost--total">
                  <dt className="label">
                    惯性代价合计
                    <span className="recovery-panel__explain">
                      回收系统 + 结构加强；预留推进剂属推进剂预算，按后端口径不计入
                    </span>
                  </dt>
                  <dd className="num">{kgText(costs.inert_cost_kg)}</dd>
                </div>
              </dl>
              <p className="recovery-panel__hint label" data-testid="recovery-context">
                {`回收级号：${
                  costs.recovered_stage_indices.length > 0
                    ? costs.recovered_stage_indices.join('、')
                    : '（未指定）'
                } · 运力损失 ${kgText(report.capacity_penalty_kg)}` +
                  `（不回收 ${kgText(report.payload_capacity_expendable_kg)} → 回收 ${kgText(
                    report.payload_capacity_recoverable_kg,
                  )}）`}
              </p>
            </>
          )}
        </>
      )}
    </section>
  )
}
