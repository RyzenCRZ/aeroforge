import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import {
  isInverseResult,
  isNsga2Result,
  isSweepResult,
  isTradeStudyResult,
  startInverse,
  startNsga2,
  startSweep,
  startTradeStudy,
  waitForOptimizeJob,
  type InverseResult,
  type InverseTargetOrbit,
  type Nsga2Result,
  type OptimizeObjectiveKey,
  type OptimizeSolutionRow,
  type OptimizeVariableKind,
  type SweepResult,
  type TradeStudyResult,
} from '../api/optimize'
import { ApiError } from '../api/client'
import { fetchUnits } from '../api/params'
import { readFieldPath } from '../store/fieldPath'
import { toDisplayValue, toUnitTable, unitSymbol, type UnitTable } from '../api/units'
import type { Vehicle } from '../api/params'
import { useVehicleStore } from '../store/vehicle'
import './OptimizePanel.css'

/**
 * 右栏「优化与权衡」面板（规格 §14，M6 缺项补做的前端片；挂在整箭数据面板下方）。
 *
 * 硬约束：
 *
 * 1. **四个能力各一块（四标签）**：多目标优化（NSGA-II）/ 权衡研究（≤ 20 方案）/
 *    批量扫描（≤ 3 轴，组合 ≤ 10⁴）/ 逆向设计——变量清单从**基线 Vehicle** 推导
 *    （length 缩放 / 发动机数 / 助推器数量，field_path 同口径）；
 * 2. **全异步作业**：复用既有作业通道（WS 优先 → 降级轮询 → 总超时兜底，§10.2），
 *    长任务进度显式呈现；`JobRecord` 有 `cancelled` 终态但既有 API 无取消端点，
 *    故不提供取消按钮，取消经失败降级通路呈现（JOB_CANCELLED）；
 * 3. **同一时刻只允许一个优化作业在途**：任一作业进行中，四个触发按钮全部置灰；
 * 4. **数值一律后端下发**（ADR-011）：前端只格式化；Pareto 行按 GLOW 升序是
 *    排版约定；失败降级 code / message / suggestion 必须可见（§10.3）；
 * 5. **优化结果不静默应用**：「载入此构型」走既有参数载入通路
 *    （`store/vehicle` 的 `setField`），载入即**覆盖当前参数**并自动触发诊断。
 */

/** 面板需要展示的错误（§10.3 的 suggestion 必填且必须可见）。 */
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

/** 单位表：载入一次并缓存（失败保持空表 = 按 SI 显示，不静默换口径——同族面板惯例）。 */
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

/** kg → 显示单位文本（§6.4：换算只按后端下发的 factor，前端零换算系数）。 */
function massText(table: UnitTable, kg: number): string {
  return `${toDisplayValue(table, 'mass', kg).toFixed(1)} ${unitSymbol(table, 'mass') || 'kg'}`
}

/** 正整数输入的解析（非法输入回退缺省；范围校验由后端 422 把关）。 */
function parsePositiveInt(draft: string, fallback: number): number {
  const value = Math.trunc(Number(draft))
  return Number.isFinite(value) && value >= 1 ? value : fallback
}

/** 面板的四个标签（同一面板四个能力各一块）。 */
const TABS = [
  { key: 'nsga2', label: '多目标优化' },
  { key: 'trade', label: '权衡研究' },
  { key: 'sweep', label: '批量扫描' },
  { key: 'inverse', label: '逆向设计' },
] as const

type TabKey = (typeof TABS)[number]['key']

/** 变量清单项：从基线 Vehicle 推导（路径即 field_path 词汇，§6.3 末注）。 */
interface VariableOption {
  path: string
  label: string
  kind: OptimizeVariableKind
  base: number
}

/**
 * 从基线 Vehicle 推导可用优化变量：各级「长度缩放」与「发动机数」；
 * 有并联助推器组时追加「助推器数量」（任务约定的最小变量族；搜索边界由后端推导）。
 */
function buildVariableOptions(vehicle: Vehicle): VariableOption[] {
  const options: VariableOption[] = []
  vehicle.stages.forEach((_stage, index) => {
    const n = index + 1
    const length = readFieldPath(vehicle, `stages[${index}].length_m`)
    if (typeof length === 'number') {
      options.push({
        path: `stages[${index}].length_m`,
        label: `第${n}级 长度缩放`,
        kind: 'continuous_scale',
        base: length,
      })
    }
    const engines = readFieldPath(vehicle, `stages[${index}].engine_count`)
    if (typeof engines === 'number') {
      options.push({
        path: `stages[${index}].engine_count`,
        label: `第${n}级 发动机数`,
        kind: 'integer_count',
        base: engines,
      })
    }
  })
  ;(vehicle.boosters ?? []).forEach((_booster, index) => {
    const count = readFieldPath(vehicle, `boosters[${index}].count`)
    if (typeof count === 'number') {
      options.push({
        path: `boosters[${index}].count`,
        label: `第${index + 1}组 助推器数量`,
        kind: 'integer_count',
        base: count,
      })
    }
  })
  return options
}

/** 方案参数摘要的一格数值：整数原样、小数三位（只格式化，不换算）。 */
function formatParam(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(3)
}

/** 批量扫描的一个轴草稿（输入态保持字符串，提交时统一解析）。 */
interface AxisDraft {
  path: string
  min: string
  max: string
  steps: string
}

/** 各能力的已收结果（切标签不清空；同一能力新作业开始时覆盖）。 */
interface CabResult {
  rows: OptimizeSolutionRow[]
  warnings: string[]
}

/** 结果表（Pareto 前沿 / 权衡对比 / 扫描聚合 / 逆向最小构型共用）。 */
function SolutionTable(props: {
  rows: OptimizeSolutionRow[]
  variableLabels: Record<string, string>
  unitTable: UnitTable
  onLoad: (row: OptimizeSolutionRow) => void
  testId: string
}) {
  const [openIndex, setOpenIndex] = useState<number | null>(null)

  if (props.rows.length === 0) {
    return <p className="optimize-panel__hint label">后端未返回可行方案</p>
  }

  return (
    <table className="optimize-panel__table" data-testid={props.testId}>
      <thead>
        <tr>
          <th className="label">方案</th>
          <th className="label">参数摘要</th>
          <th className="label">GLOW</th>
          <th className="label">运力</th>
          <th className="label">来源与操作</th>
        </tr>
      </thead>
      <tbody>
        {props.rows.map((row, index) => {
          const hasProvenance =
            row.provenance !== undefined && Object.keys(row.provenance).length > 0
          const summary = Object.entries(row.params)
            .map(
              ([path, value]) =>
                `${props.variableLabels[path] ?? path} = ${formatParam(value)}`,
            )
            .join('；')
          return (
            <SolutionRowView
              key={`${index}-${row.glow_kg}`}
              index={index}
              label={row.label ?? `方案 ${index + 1}`}
              summary={summary}
              glow={massText(props.unitTable, row.glow_kg)}
              payload={massText(props.unitTable, row.payload_kg)}
              hasProvenance={hasProvenance}
              open={openIndex === index}
              provenance={row.provenance ?? {}}
              onToggle={() => setOpenIndex(openIndex === index ? null : index)}
              onLoad={() => props.onLoad(row)}
            />
          )
        })}
      </tbody>
    </table>
  )
}

/** 结果表的一行（含行内 provenance 展开行——审计纪律 §14：候选方案必须可溯源）。 */
function SolutionRowView(props: {
  index: number
  label: string
  summary: string
  glow: string
  payload: string
  hasProvenance: boolean
  open: boolean
  provenance: Record<string, string>
  onToggle: () => void
  onLoad: () => void
}) {
  return (
    <>
      <tr>
        <th scope="row" className="label">
          {props.label}
        </th>
        <td className="optimize-panel__summary">{props.summary}</td>
        <td className="num">{props.glow}</td>
        <td className="num">{props.payload}</td>
        <td className="optimize-panel__row-actions">
          {props.hasProvenance ? (
            <button
              type="button"
              className="optimize-panel__button optimize-panel__button--small"
              data-testid={`prov-toggle-${props.index}`}
              onClick={props.onToggle}
            >
              {props.open ? '收起来源' : '来源'}
            </button>
          ) : null}
          <button
            type="button"
            className="optimize-panel__button optimize-panel__button--small"
            data-testid={`load-config-${props.index}`}
            title="把该方案参数写回当前构型（覆盖当前参数，走既有参数载入通路）"
            onClick={props.onLoad}
          >
            载入此构型
          </button>
        </td>
      </tr>
      {props.open ? (
        <tr>
          <td colSpan={5} className="optimize-panel__prov" data-testid={`prov-detail-${props.index}`}>
            <dl className="optimize-panel__prov-list">
              {Object.entries(props.provenance).map(([key, value]) => (
                <div key={key} className="optimize-panel__prov-item">
                  <dt className="label">{key}</dt>
                  <dd className="num">{value}</dd>
                </div>
              ))}
            </dl>
          </td>
        </tr>
      ) : null}
    </>
  )
}

/** 优化与权衡面板本体（挂载点 = views/Workspace 右栏，整箭数据面板下方）。 */
export function OptimizePanel() {
  const vehicle = useVehicleStore((state) => state.vehicle)

  const [activeTab, setActiveTab] = useState<TabKey>('nsga2')
  /** 全局作业态：一次只允许一个优化作业在途（进行中四个触发按钮全部置灰）。 */
  const [runningJob, setRunningJob] = useState<{
    label: string
    progress: number
    stage: string
  } | null>(null)
  const [jobError, setJobError] = useState<PanelError | null>(null)
  const [results, setResults] = useState<Partial<Record<TabKey, CabResult>>>({})
  const [loadNote, setLoadNote] = useState<string | null>(null)
  /** 迟到丢弃令牌：新作业发起后，旧作业的迟到进度 / 结果不再写状态（perf store 同款纪律）。 */
  const jobTokenRef = useRef(0)

  // ---- 多目标优化（NSGA-II）输入 ----
  const [selectedVariables, setSelectedVariables] = useState<Set<string>>(new Set())
  const [populationDraft, setPopulationDraft] = useState('40')
  const [generationsDraft, setGenerationsDraft] = useState('30')
  const [seedDraft, setSeedDraft] = useState('42')
  const [objectives, setObjectives] = useState<{ minGlow: boolean; maxPayloadLeo: boolean }>({
    minGlow: true,
    maxPayloadLeo: true,
  })

  // ---- 权衡研究 / 批量扫描 / 逆向设计输入 ----
  const [variantCountDraft, setVariantCountDraft] = useState('6')
  const [axes, setAxes] = useState<AxisDraft[]>([{ path: '', min: '', max: '', steps: '' }])
  const [targetOrbit, setTargetOrbit] = useState<InverseTargetOrbit>('LEO')
  const [targetPayloadDraft, setTargetPayloadDraft] = useState('')

  const unitTable = useUnitTable()

  const variableOptions = useMemo(
    () => (vehicle === null ? [] : buildVariableOptions(vehicle)),
    [vehicle],
  )
  const variableLabels = useMemo(() => {
    const map: Record<string, string> = {}
    for (const option of variableOptions) map[option.path] = option.label
    return map
  }, [variableOptions])

  // 变量清单首次到达且尚未做选择时，默认勾选第一个变量，避免空选择起步。
  useEffect(() => {
    setSelectedVariables((prev) => {
      if (prev.size > 0 || variableOptions.length === 0) return prev
      return new Set([variableOptions[0].path])
    })
  }, [variableOptions])

  const toggleVariable = (path: string, checked: boolean): void => {
    setSelectedVariables((prev) => {
      const next = new Set(prev)
      if (checked) next.add(path)
      else next.delete(path)
      return next
    })
  }

  /**
   * 统一的作业生命周期：投递 → 作业通道（进度透出）→ 守卫还原结果 → 按 Tab 收纳。
   * 失败（投递 4xx / 作业失败 / 取消 / 契约不符 / 超时）一律走 §10.3 降级呈现。
   */
  const runJob = <T,>(
    key: TabKey,
    label: string,
    start: () => Promise<string>,
    isResult: (value: unknown) => value is T,
    toRows: (result: T) => OptimizeSolutionRow[],
  ): void => {
    if (runningJob !== null) return
    const token = ++jobTokenRef.current
    setRunningJob({ label, progress: 0, stage: 'queued' })
    setJobError(null)
    setLoadNote(null)
    start()
      .then((jobId) =>
        waitForOptimizeJob(jobId, (record) => {
          if (token !== jobTokenRef.current) return
          setRunningJob({ label, progress: record.progress, stage: record.stage })
        }),
      )
      .then((payload) => {
        if (token !== jobTokenRef.current) return
        if (!isResult(payload)) {
          throw new ApiError({
            code: 'CONTRACT_MISMATCH',
            stage: 'api',
            message: `优化作业「${label}」的结果载荷不符合前端最小契约（待切生成物类型）`,
            suggestion:
              '确认后端优化作业的 metrics 组装与前端一致；契约生成物就绪后重跑 npm run gen:api 切换类型',
          })
        }
        const warnings = (payload as { warnings?: string[] }).warnings ?? []
        setResults((prev) => ({ ...prev, [key]: { rows: toRows(payload), warnings } }))
        setRunningJob(null)
      })
      .catch((error: unknown) => {
        if (token !== jobTokenRef.current) return
        setRunningJob(null)
        setJobError(toPanelError(error))
      })
  }

  const busy = runningJob !== null

  // ---- 各能力的启动条件与提示（未配置变量 / 目标时禁用并说明原因） ----
  const objectivesSelected = objectives.minGlow || objectives.maxPayloadLeo
  const nsga2Disabled = vehicle === null || busy || !objectivesSelected || selectedVariables.size === 0
  const nsga2Hint =
    selectedVariables.size === 0
      ? '未选择任何优化变量——至少勾选一个变量才能开始优化'
      : !objectivesSelected
        ? '至少选择一个优化目标'
        : null

  const handleStartNsga2 = (): void => {
    const current = useVehicleStore.getState().vehicle
    if (current === null || busy || !objectivesSelected || selectedVariables.size === 0) return
    const variables = variableOptions
      .filter((option) => selectedVariables.has(option.path))
      .map((option) => ({ path: option.path, kind: option.kind, base: option.base }))
    const selectedObjectives: OptimizeObjectiveKey[] = []
    if (objectives.minGlow) selectedObjectives.push('min_glow')
    if (objectives.maxPayloadLeo) selectedObjectives.push('max_payload_leo')
    runJob(
      'nsga2',
      '多目标优化（NSGA-II）',
      () =>
        startNsga2(current, variables, {
          populationSize: parsePositiveInt(populationDraft, 40),
          generations: parsePositiveInt(generationsDraft, 30),
          seed:
            Number.isFinite(Math.trunc(Number(seedDraft))) && Number(seedDraft) >= 0
              ? Math.trunc(Number(seedDraft))
              : 42,
          objectives: selectedObjectives,
        }),
      isNsga2Result,
      // Pareto 行按 GLOW 升序是排版约定（任务口径）；数值本身照抄后端
      (result: Nsga2Result) => [...result.pareto_front].sort((a, b) => a.glow_kg - b.glow_kg),
    )
  }

  const handleStartTrade = (): void => {
    const current = useVehicleStore.getState().vehicle
    if (current === null || busy) return
    const variantCount = parsePositiveInt(variantCountDraft, 6)
    runJob(
      'trade',
      '权衡研究',
      () => startTradeStudy(current, variantCount),
      isTradeStudyResult,
      (result: TradeStudyResult) => result.variants,
    )
  }

  const parsedAxes = axes.flatMap((axis) => {
    const min = Number(axis.min)
    const max = Number(axis.max)
    const steps = Math.trunc(Number(axis.steps))
    if (
      axis.path === '' ||
      !Number.isFinite(min) ||
      !Number.isFinite(max) ||
      !Number.isFinite(steps) ||
      steps < 2
    ) {
      return []
    }
    return [{ path: axis.path, min, max, steps }]
  })
  const sweepDisabled = vehicle === null || busy || parsedAxes.length === 0
  const sweepHint =
    parsedAxes.length === 0 ? '至少定义一个有效轴（变量 + 范围 + 步数 ≥ 2）' : null

  const handleStartSweep = (): void => {
    const current = useVehicleStore.getState().vehicle
    if (current === null || busy || parsedAxes.length === 0) return
    runJob(
      'sweep',
      '批量扫描',
      () => startSweep(current, parsedAxes),
      isSweepResult,
      (result: SweepResult) => result.rows,
    )
  }

  const targetPayload = Number(targetPayloadDraft)
  const inverseDisabled =
    vehicle === null || busy || !Number.isFinite(targetPayload) || targetPayload <= 0
  const inverseHint =
    !Number.isFinite(targetPayload) || targetPayload <= 0 ? '请填写目标运力（kg，大于 0）' : null

  const handleStartInverse = (): void => {
    const current = useVehicleStore.getState().vehicle
    if (current === null || busy || !Number.isFinite(targetPayload) || targetPayload <= 0) return
    runJob(
      'inverse',
      '逆向设计',
      () => startInverse(current, targetOrbit, targetPayload),
      isInverseResult,
      (result: InverseResult) => [result.config],
    )
  }

  /** 载入此构型：走既有参数载入通路逐项写回（覆盖当前参数，store 会自动触发诊断）。 */
  const handleLoadConfig = (row: OptimizeSolutionRow): void => {
    const store = useVehicleStore.getState()
    for (const [path, value] of Object.entries(row.params)) {
      store.setField(path, value)
    }
    setLoadNote(
      `已把「${row.label ?? '所选方案'}」的参数载入当前构型（覆盖当前参数，走既有参数载入通路）`,
    )
  }

  const renderSolutionTable = (key: TabKey, testId: string): ReactNode => {
    const cab = results[key]
    if (cab === undefined) return null
    return (
      <div className="optimize-panel__result">
        {cab.warnings.length > 0 ? (
          <ul className="optimize-panel__warnings">
            {cab.warnings.map((warning, index) => (
              <li key={`warning-${index}`} className="optimize-panel__warning label">
                {warning}
              </li>
            ))}
          </ul>
        ) : null}
        <SolutionTable
          rows={cab.rows}
          variableLabels={variableLabels}
          unitTable={unitTable}
          onLoad={handleLoadConfig}
          testId={testId}
        />
      </div>
    )
  }

  // 尚未载入基线构型：面板骨架仍在，全部触发按钮不可用（不白屏、给可操作文案）。
  if (vehicle === null) {
    return (
      <section className="panel surface optimize-panel" data-testid="optimize-panel">
        <h2 className="panel__title label">优化与权衡</h2>
        <p className="optimize-panel__hint label">尚未载入参数，无法启动优化作业</p>
      </section>
    )
  }

  return (
    <section className="panel surface optimize-panel" data-testid="optimize-panel">
      <h2 className="panel__title label">优化与权衡</h2>

      <p className="optimize-panel__baseline label">
        基线构型：
        <span className="num" data-testid="optimize-baseline-name">
          {vehicle.name}
        </span>
        <span className="optimize-panel__baseline-note">（载入方案会覆盖当前参数）</span>
      </p>

      {runningJob !== null ? (
        <p className="optimize-panel__job" data-testid="optimize-job-progress">
          {`优化作业进行中：${runningJob.label} · 阶段 ${runningJob.stage} · ${Math.round(
            runningJob.progress * 100,
          )}%（长任务可达数十秒；同一时刻仅允许一个优化作业在途）`}
        </p>
      ) : null}

      {jobError !== null ? (
        <p className="optimize-panel__error" data-testid="optimize-job-error">
          {`优化作业失败（${jobError.code}）：${jobError.message}——${jobError.suggestion}`}
        </p>
      ) : null}

      {loadNote !== null ? (
        <p className="optimize-panel__note" data-testid="optimize-load-note">
          {loadNote}
        </p>
      ) : null}

      <div className="optimize-panel__tabs" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.key}
            className={
              activeTab === tab.key
                ? 'optimize-panel__tab optimize-panel__tab--active'
                : 'optimize-panel__tab'
            }
            data-testid={`optimize-tab-${tab.key}`}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* ---- 多目标优化（NSGA-II）：变量勾选 + 种群/代数/seed + 目标勾选 ---- */}
      {/* 四块常驻 DOM、以 hidden 切换显隐：作业进行中的「全局置灰」跨标签可断言/可感知 */}
      <section
        className="optimize-panel__section"
        hidden={activeTab !== 'nsga2'}
        aria-label="多目标优化"
      >
          <p className="optimize-panel__hint label">
            从基线构型勾选优化变量（变量类别见括注；搜索边界由后端按变量推导）：
          </p>
          <div className="optimize-panel__checks">
            {variableOptions.map((option) => (
              <label key={option.path} className="optimize-panel__check">
                <input
                  type="checkbox"
                  checked={selectedVariables.has(option.path)}
                  onChange={(event) => toggleVariable(option.path, event.target.checked)}
                />
                <span className="label">{`${option.label}（当前 ${option.base}）`}</span>
              </label>
            ))}
          </div>
          <div className="optimize-panel__row">
            <label className="optimize-panel__field">
              <span className="label">种群规模</span>
              <input
                type="number"
                min={1}
                value={populationDraft}
                data-testid="nsga2-population"
                onChange={(event) => setPopulationDraft(event.target.value)}
              />
            </label>
            <label className="optimize-panel__field">
              <span className="label">代数</span>
              <input
                type="number"
                min={1}
                value={generationsDraft}
                data-testid="nsga2-generations"
                onChange={(event) => setGenerationsDraft(event.target.value)}
              />
            </label>
            <label className="optimize-panel__field">
              <span className="label">随机种子</span>
              <input
                type="number"
                value={seedDraft}
                data-testid="nsga2-seed"
                onChange={(event) => setSeedDraft(event.target.value)}
              />
            </label>
          </div>
          <div className="optimize-panel__row">
            <label className="optimize-panel__check">
              <input
                type="checkbox"
                checked={objectives.minGlow}
                onChange={(event) =>
                  setObjectives((prev) => ({ ...prev, minGlow: event.target.checked }))
                }
              />
              <span className="label">目标：最小 GLOW</span>
            </label>
            <label className="optimize-panel__check">
              <input
                type="checkbox"
                checked={objectives.maxPayloadLeo}
                onChange={(event) =>
                  setObjectives((prev) => ({ ...prev, maxPayloadLeo: event.target.checked }))
                }
              />
              <span className="label">目标：最大 LEO 运力</span>
            </label>
          </div>
          {nsga2Hint !== null ? (
            <p className="optimize-panel__hint label" data-testid="nsga2-disabled-hint">
              {nsga2Hint}
            </p>
          ) : null}
          <div className="optimize-panel__actions">
            <button
              type="button"
              className="optimize-panel__button"
              data-testid="nsga2-start-button"
              disabled={nsga2Disabled}
              onClick={handleStartNsga2}
            >
              开始优化
            </button>
          </div>
          {renderSolutionTable('nsga2', 'nsga2-pareto-table')}
      </section>

      {/* ---- 权衡研究：方案数 ≤ 20 说明 + 触发 + 对比表 ---- */}
      <section
        className="optimize-panel__section"
        hidden={activeTab !== 'trade'}
        aria-label="权衡研究"
      >
          <p className="optimize-panel__hint label">
            围绕基线构型并行评估多个方案并生成对比表；方案数上限 20（规格 §14），超限由后端
            422 拒绝。
          </p>
          <div className="optimize-panel__row">
            <label className="optimize-panel__field">
              <span className="label">方案数（≤ 20）</span>
              <input
                type="number"
                min={1}
                max={20}
                value={variantCountDraft}
                data-testid="trade-variant-count"
                onChange={(event) => setVariantCountDraft(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="optimize-panel__button"
              data-testid="trade-start-button"
              disabled={busy}
              onClick={handleStartTrade}
            >
              开始权衡研究
            </button>
          </div>
          {renderSolutionTable('trade', 'trade-table')}
      </section>

      {/* ---- 批量扫描：轴定义（变量 × 范围 × 步数，≤ 3 轴）+ 422 呈现 + 聚合表 ---- */}
      <section
        className="optimize-panel__section"
        hidden={activeTab !== 'sweep'}
        aria-label="批量扫描"
      >
          <p className="optimize-panel__hint label">
            变量 × 范围 × 步数的全因子扫描；最多 3 轴、组合上限 10⁴（规格 §14），超限由后端
            422 拒绝。
          </p>
          {axes.map((axis, index) => (
            <div className="optimize-panel__row" key={index} data-testid={`sweep-axis-${index}`}>
              <label className="optimize-panel__field">
                <span className="label">变量</span>
                <select
                  value={axis.path}
                  data-testid={`sweep-axis-path-${index}`}
                  onChange={(event) =>
                    setAxes((prev) =>
                      prev.map((item, i) =>
                        i === index ? { ...item, path: event.target.value } : item,
                      ),
                    )
                  }
                >
                  <option value="">选择变量…</option>
                  {variableOptions.map((option) => (
                    <option key={option.path} value={option.path}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="optimize-panel__field">
                <span className="label">最小</span>
                <input
                  type="number"
                  value={axis.min}
                  data-testid={`sweep-axis-min-${index}`}
                  onChange={(event) =>
                    setAxes((prev) =>
                      prev.map((item, i) =>
                        i === index ? { ...item, min: event.target.value } : item,
                      ),
                    )
                  }
                />
              </label>
              <label className="optimize-panel__field">
                <span className="label">最大</span>
                <input
                  type="number"
                  value={axis.max}
                  data-testid={`sweep-axis-max-${index}`}
                  onChange={(event) =>
                    setAxes((prev) =>
                      prev.map((item, i) =>
                        i === index ? { ...item, max: event.target.value } : item,
                      ),
                    )
                  }
                />
              </label>
              <label className="optimize-panel__field">
                <span className="label">步数</span>
                <input
                  type="number"
                  min={2}
                  value={axis.steps}
                  data-testid={`sweep-axis-steps-${index}`}
                  onChange={(event) =>
                    setAxes((prev) =>
                      prev.map((item, i) =>
                        i === index ? { ...item, steps: event.target.value } : item,
                      ),
                    )
                  }
                />
              </label>
              <button
                type="button"
                className="optimize-panel__button optimize-panel__button--small"
                data-testid={`sweep-axis-remove-${index}`}
                onClick={() => setAxes((prev) => prev.filter((_, i) => i !== index))}
              >
                移除
              </button>
            </div>
          ))}
          <div className="optimize-panel__row">
            <button
              type="button"
              className="optimize-panel__button optimize-panel__button--small"
              data-testid="sweep-add-axis"
              disabled={axes.length >= 3}
              title="最多 3 轴（规格 §14）"
              onClick={() => setAxes((prev) => [...prev, { path: '', min: '', max: '', steps: '' }])}
            >
              添加轴
            </button>
          </div>
          {sweepHint !== null ? (
            <p className="optimize-panel__hint label" data-testid="sweep-disabled-hint">
              {sweepHint}
            </p>
          ) : null}
          <div className="optimize-panel__actions">
            <button
              type="button"
              className="optimize-panel__button"
              data-testid="sweep-start-button"
              disabled={sweepDisabled}
              onClick={handleStartSweep}
            >
              开始扫描
            </button>
          </div>
          {renderSolutionTable('sweep', 'sweep-table')}
      </section>

      {/* ---- 逆向设计：目标轨道 + 目标运力 → 最小构型结果表 ---- */}
      <section
        className="optimize-panel__section"
        hidden={activeTab !== 'inverse'}
        aria-label="逆向设计"
      >
          <p className="optimize-panel__hint label">
            给定目标轨道与运力，求满足要求的最小构型（规格 §14 约束优化）。
          </p>
          <div className="optimize-panel__row">
            <label className="optimize-panel__field">
              <span className="label">目标轨道</span>
              <select
                value={targetOrbit}
                data-testid="inverse-orbit"
                onChange={(event) => setTargetOrbit(event.target.value as InverseTargetOrbit)}
              >
                <option value="LEO">LEO</option>
                <option value="GTO">GTO</option>
                <option value="GEO">GEO</option>
              </select>
            </label>
            <label className="optimize-panel__field">
              <span className="label">目标运力（kg）</span>
              <input
                type="number"
                min={0}
                value={targetPayloadDraft}
                data-testid="inverse-payload"
                onChange={(event) => setTargetPayloadDraft(event.target.value)}
              />
            </label>
            <button
              type="button"
              className="optimize-panel__button"
              data-testid="inverse-start-button"
              disabled={inverseDisabled}
              onClick={handleStartInverse}
            >
              开始逆向设计
            </button>
          </div>
          {inverseHint !== null ? (
            <p className="optimize-panel__hint label" data-testid="inverse-disabled-hint">
              {inverseHint}
            </p>
          ) : null}
          {renderSolutionTable('inverse', 'inverse-table')}
      </section>
    </section>
  )
}
