import { useEffect, useRef, useState } from 'react'

import { fetchUnits, type Quantity, type Vehicle } from '../api/params'
import { fetchTemplateDetail, matchTemplateName, type TemplateMatchResponse } from '../api/templates'
import { toDisplayValue, toSiValue, toUnitTable, unitSymbol, type UnitTable } from '../api/units'
import { readFieldPath } from '../store/fieldPath'
import { useVehicleStore } from '../store/vehicle'
import './VehiclePanel.css'

/**
 * 左栏「参数面板」（规格 §11.5 核心操作流 ①）。
 *
 * 三条硬约束：
 *
 * 1. **起始箭来自后端**（§11.5 ① 第 6 条）：`GET /api/params/template`，并原样呈现它的
 *    `note` 与 `sourced_fields`。不在此表里的数值按**占位值**标注（§1.4-4）——
 *    把占位数当成"已核对的型号数据"是本条要拦的形态。
 * 2. **控件以 `data-field-path` 标记**（§1.7.3 OI-32）：取值与后端 `field_path` **逐字一致**，
 *    诊断清单据此定位（两条通路共用一张映射）。
 * 3. **单位按后端下发的 `factor` 格式化**（§6.4 / OI-32）：前端零换算系数。
 */

/** 一个可编辑字段的声明。`path` 是**相对路径**（级字段由面板补上 `stages[i].` 前缀）。 */
interface FieldSpec {
  path: string
  label: string
  /** 量；`null` = 无量纲（比例 / 计数 / 枚举）。 */
  quantity: Quantity | null
  /** 单位表不可用时的回退单位文本（= 内部 SI 符号，§1.4-3）。 */
  siUnit: string
  kind: 'number' | 'text' | 'select'
  options?: readonly string[]
  step?: number
}

const VEHICLE_FIELDS: readonly FieldSpec[] = [
  { path: 'name', label: '火箭名称', quantity: null, siUnit: '', kind: 'text' },
  { path: 'payload_mass_kg', label: '有效载荷质量', quantity: 'mass', siUnit: 'kg', kind: 'number', step: 100 },
]

/** 级字段：面板按 `stages[i].` 前缀展开为逐级控件（§6.1 的级层与发动机层）。 */
const STAGE_FIELDS: readonly FieldSpec[] = [
  { path: 'length_m', label: '级高度', quantity: 'length', siUnit: 'm', kind: 'number', step: 0.1 },
  { path: 'diameter_m', label: '级直径', quantity: 'length', siUnit: 'm', kind: 'number', step: 0.1 },
  { path: 'wall_thickness_m', label: '级壁厚', quantity: 'length', siUnit: 'm', kind: 'number', step: 0.0005 },
  { path: 'structure_coefficient', label: '结构系数 σ', quantity: null, siUnit: '', kind: 'number', step: 0.005 },
  { path: 'fill_fraction', label: '加注比例', quantity: null, siUnit: '', kind: 'number', step: 0.01 },
  { path: 'engine_count', label: '发动机台数', quantity: null, siUnit: '', kind: 'number', step: 1 },
  { path: 'isp_vacuum_s', label: '该级真空比冲', quantity: 'isp', siUnit: 's', kind: 'number', step: 1 },
  { path: 'isp_sea_level_s', label: '该级海平面比冲', quantity: 'isp', siUnit: 's', kind: 'number', step: 1 },
  {
    path: 'isp_source',
    label: '比冲来源',
    quantity: null,
    siUnit: '',
    kind: 'select',
    options: ['default', 'custom'],
  },
  {
    path: 'engine.propellant_phase',
    label: '推进剂相态（§6.5 推重比档位依据）',
    quantity: null,
    siUnit: '',
    kind: 'select',
    options: ['liquid', 'solid', 'hybrid'],
  },
  { path: 'engine.thrust_sea_level_n', label: '单机海平面推力', quantity: 'force', siUnit: 'N', kind: 'number', step: 1000 },
  { path: 'engine.thrust_vacuum_n', label: '单机真空推力', quantity: 'force', siUnit: 'N', kind: 'number', step: 1000 },
  { path: 'engine.chamber_pressure_pa', label: '室压', quantity: 'pressure', siUnit: 'Pa', kind: 'number', step: 100000 },
  { path: 'engine.expansion_ratio', label: '喷管膨胀比 ε', quantity: null, siUnit: '', kind: 'number', step: 0.1 },
  { path: 'engine.mixture_ratio', label: '混合比 O/F', quantity: null, siUnit: '', kind: 'number', step: 0.01 },
  { path: 'engine.efficiency_factor', label: '效率因子', quantity: null, siUnit: '', kind: 'number', step: 0.01 },
]

/** 贮箱字段：§6.5 的「贮箱壁厚」规则就是按这些路径定位控件的。 */
const TANK_FIELDS: readonly FieldSpec[] = [
  {
    path: 'geometry.oxidizer_tank.wall_thickness_m',
    label: '氧化剂箱壁厚',
    quantity: 'length',
    siUnit: 'm',
    kind: 'number',
    step: 0.0005,
  },
  {
    path: 'geometry.oxidizer_tank.fill_fraction',
    label: '氧化剂箱加注比例',
    quantity: null,
    siUnit: '',
    kind: 'number',
    step: 0.01,
  },
  {
    path: 'geometry.fuel_tank.wall_thickness_m',
    label: '燃料箱壁厚',
    quantity: 'length',
    siUnit: 'm',
    kind: 'number',
    step: 0.0005,
  },
  {
    path: 'geometry.fuel_tank.fill_fraction',
    label: '燃料箱加注比例',
    quantity: null,
    siUnit: '',
    kind: 'number',
    step: 0.01,
  },
]

const MISSION_FIELDS: readonly FieldSpec[] = [
  {
    path: 'mission.orbit_type',
    label: '目标轨道',
    quantity: null,
    siUnit: '',
    kind: 'select',
    options: ['LEO', 'SSO', 'GTO', 'GEO', 'TLI', 'TMI', 'escape', 'custom'],
  },
  { path: 'mission.altitude_m', label: '轨道高度', quantity: 'length', siUnit: 'm', kind: 'number', step: 1000 },
  { path: 'mission.inclination_deg', label: '轨道倾角', quantity: null, siUnit: '°', kind: 'number', step: 0.5 },
]

/** 名称匹配的防抖间隔（OI-34）：与诊断防抖分开计——匹配查询更廉价，但也不逐字符发。 */
export const NAME_MATCH_DEBOUNCE_MS = 300

/** 提示条的状态：命中的响应 + 触发它的输入名（[忽略] 按「同一名称」判定，§11.5 ⑤ 规则 5）。 */
interface NameMatch {
  queriedName: string
  response: TemplateMatchResponse
}

/** 单位表：载入一次并缓存。失败即保持空表——此时面板按 **SI** 显示并显式说明（不静默换口径）。 */
function useUnitTable(): UnitTable {
  const [table, setTable] = useState<UnitTable>({})
  useEffect(() => {
    let alive = true
    fetchUnits()
      .then((response) => {
        if (alive) setTable(toUnitTable(response.units))
      })
      .catch(() => {
        // 单位表拿不到不影响编辑：面板回退到 SI 并在标题处标出。
      })
    return () => {
      alive = false
    }
  }, [])
  return table
}

interface NumberFieldProps {
  fieldPath: string
  label: string
  unitText: string
  step: number
  /** `null` = 尚未填写（仅 custom 比冲这类"条件必填"字段会出现） */
  value: number | null
  unsourced: boolean
  /** 出处标注（§11.5 ⑤ 规则 5）：渲染在控件下方的小字；`undefined` = 无出处（占位值）。 */
  source: string | undefined
  onCommit: (value: number) => void
}

/**
 * 数值控件：**草稿字符串 + 提交时解析**。
 *
 * 为什么不能直接把 `Number(event.target.value)` 写回：输入 `0.0005` 的中间态 `0.`、`0.0`
 * 在 `Number` 下会退化成 `0`，逐字符重置会把小数点吃掉（壁厚、O/F 这类小数首当其冲）。
 */
function NumberField({ fieldPath, label, unitText, step, value, unsourced, source, onCommit }: NumberFieldProps) {
  const [draft, setDraft] = useState(value === null ? '' : String(value))

  useEffect(() => {
    setDraft(value === null ? '' : String(value))
  }, [value])

  return (
    <label className="vehicle-panel__field">
      <span className="vehicle-panel__label label">
        {label}
        {unitText === '' ? '' : `（${unitText}）`}
        {unsourced ? <span className="vehicle-panel__placeholder">占位</span> : null}
      </span>
      <input
        type="number"
        className="num"
        step={step}
        data-field-path={fieldPath}
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value)
          const parsed = Number(event.target.value)
          if (event.target.value.trim() !== '' && Number.isFinite(parsed)) onCommit(parsed)
        }}
      />
      {source === undefined ? null : <span className="vehicle-panel__source-name">{source}</span>}
    </label>
  )
}

interface FieldProps {
  spec: FieldSpec
  fieldPath: string
  value: unknown
  unitTable: UnitTable
  sourced: string | undefined
  onCommit: (path: string, value: unknown) => void
}

function Field({ spec, fieldPath, value, unitTable, sourced, onCommit }: FieldProps) {
  const unsourced = sourced === undefined
  const unitText =
    spec.quantity === null ? spec.siUnit : (unitSymbol(unitTable, spec.quantity) || spec.siUnit)
  // 出处标注（§11.5 ⑤ 规则 5）：有出处的字段在控件下方以小字呈现；用户改动后由
  // store 转为「用户修改」。无出处的字段不标注文字（只有「占位」徽标）。
  const sourceNode = sourced === undefined ? null : (
    <span className="vehicle-panel__source-name">{sourced}</span>
  )

  if (spec.kind === 'select') {
    return (
      <label className="vehicle-panel__field">
        <span className="vehicle-panel__label label">
          {spec.label}
          {unsourced ? <span className="vehicle-panel__placeholder">占位</span> : null}
        </span>
        <select
          className="num"
          data-field-path={fieldPath}
          value={typeof value === 'string' ? value : ''}
          onChange={(event) => onCommit(fieldPath, event.target.value)}
        >
          {(spec.options ?? []).map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        {sourceNode}
      </label>
    )
  }

  if (spec.kind === 'text') {
    return (
      <label className="vehicle-panel__field">
        <span className="vehicle-panel__label label">
          {spec.label}
          {unsourced ? <span className="vehicle-panel__placeholder">占位</span> : null}
        </span>
        <input
          type="text"
          data-field-path={fieldPath}
          value={typeof value === 'string' ? value : ''}
          onChange={(event) => onCommit(fieldPath, event.target.value)}
        />
        {sourceNode}
      </label>
    )
  }

  // 数值型：契约里是 SI，界面按后端下发的 factor 换算（§6.4 / OI-32）。
  // 未填写（null）显示空串而不是 0——把"没填"渲染成 0 会诱导用户提交假值（§1.4-4）。
  const siValue = typeof value === 'number' ? value : null
  const display =
    siValue === null
      ? null
      : spec.quantity === null
        ? siValue
        : toDisplayValue(unitTable, spec.quantity, siValue)

  return (
    <NumberField
      fieldPath={fieldPath}
      label={spec.label}
      unitText={unitText}
      step={spec.step ?? 1}
      value={display}
      unsourced={unsourced}
      source={sourced}
      onCommit={(next) => {
        const si = spec.quantity === null ? next : toSiValue(unitTable, spec.quantity, next)
        onCommit(fieldPath, si)
      }}
    />
  )
}

/** 左栏：参数面板（起始箭 + 逐级字段 + 任务层字段）。 */
export function VehiclePanel() {
  const vehicle = useVehicleStore((state) => state.vehicle)
  const label = useVehicleStore((state) => state.label)
  const note = useVehicleStore((state) => state.note)
  const sourcedFields = useVehicleStore((state) => state.sourcedFields)
  const loading = useVehicleStore((state) => state.loading)
  const loadError = useVehicleStore((state) => state.loadError)
  const diagnosing = useVehicleStore((state) => state.diagnosing)
  const loadTemplate = useVehicleStore((state) => state.loadTemplate)
  const setField = useVehicleStore((state) => state.setField)
  const setSourcedFields = useVehicleStore((state) => state.setSourcedFields)
  const markUserModified = useVehicleStore((state) => state.markUserModified)
  const unitTable = useUnitTable()

  // —— OI-34 名称匹配（§11.5 ⑤ 规则 5）——
  // 命中只在提示条里呈现，绝不静默改写任何参数：载不载入由用户点按钮决定。
  const [nameMatch, setNameMatch] = useState<NameMatch | null>(null)
  const [templateLoadError, setTemplateLoadError] = useState<string | null>(null)
  const matchTimer = useRef<number | null>(null)
  const ignoredName = useRef<string | null>(null)

  /** 名称提交后 300ms 防抖匹配；未命中 / 空名 / 已忽略的同名 → 无任何提示（宁漏勿错）。 */
  const scheduleNameMatch = (name: string) => {
    if (matchTimer.current !== null) window.clearTimeout(matchTimer.current)
    // 名称已变：旧提示条立即作废——它描述的是上一个名称。
    setNameMatch(null)
    if (name.trim() === '') return // 空名不发起请求
    matchTimer.current = window.setTimeout(() => {
      matchTemplateName(name)
        .then((response) => {
          if (response.matched && name !== ignoredName.current) {
            setNameMatch({ queriedName: name, response })
          }
        })
        .catch(() => {
          // 匹配查询失败不是用户可见错误（输入过程中的高频查询）：静默，宁漏勿错。
        })
    }, NAME_MATCH_DEBOUNCE_MS)
  }

  // 卸载时清掉未触发的防抖回调（jsdom 测试与快速切换页面都靠它兜底）。
  useEffect(() => {
    return () => {
      if (matchTimer.current !== null) window.clearTimeout(matchTimer.current)
    }
  }, [])

  /**
   * 提交包装（QA-1，v0.6.2）：把比冲来源切回 `default` 时**同时清空**两个级层比冲字段
   * ——留着旧覆写值，后端会判 HARD_ISP_DEFAULT_MISMATCH；null 写回即从载荷中移除语义。
   */
  const commitField = (path: string, value: unknown) => {
    setField(path, value)
    // §11.5 ⑤ 规则 5：用户改动的字段，模板出处即失效，标注转为「用户修改」。
    // 只对已有出处的字段转换——给占位字段补一条"用户修改"等于替它编出处。
    if (sourcedFields[path] !== undefined) markUserModified(path)
    if (value === 'default' && path.endsWith('.isp_source')) {
      const prefix = path.slice(0, -'isp_source'.length)
      setField(`${prefix}isp_vacuum_s`, null)
      setField(`${prefix}isp_sea_level_s`, null)
    }
    if (path === 'name' && typeof value === 'string') scheduleNameMatch(value)
  }

  /**
   * 载入命中模板（§11.5 ⑤ 规则 3）：detail.vehicle 整套写入，**但保留**用户当前的
   * 名称与 mission 里的发射场——用户已经输入的东西不因载入而被覆盖。
   */
  const loadMatchedTemplate = () => {
    if (nameMatch === null) return
    const templateId = nameMatch.response.template_id
    if (templateId === null || templateId === undefined) return
    const current = useVehicleStore.getState().vehicle
    if (current === null) return
    setTemplateLoadError(null)
    fetchTemplateDetail(templateId)
      .then((detail) => {
        const nextVehicle: Vehicle = {
          ...detail.vehicle,
          name: current.name,
          mission: {
            ...detail.vehicle.mission,
            launch_site: current.mission.launch_site,
            launch_site_id: current.mission.launch_site_id,
          },
        }
        useVehicleStore.setState({ vehicle: nextVehicle })
        // 出处表整体替换 = 旧表的「用户修改」标注一并清掉（新表以模板为准）。
        setSourcedFields(detail.sourced_fields)
        useVehicleStore.getState().requestDiagnose()
        setNameMatch(null)
        ignoredName.current = null
      })
      .catch((error: unknown) => {
        // §10.3：错误必须可见。这里不用 ApiError 的 suggestion（面板顶部已有全局口径），
        // 但失败绝不能静默——用户点过的按钮必须给个说法。
        setTemplateLoadError(error instanceof Error ? error.message : '未知错误')
      })
  }

  /** [忽略]：只收起提示条；同一名称本轮内不再提示，名称再次变更后重新匹配。 */
  const ignoreMatchedTemplate = () => {
    if (nameMatch === null) return
    ignoredName.current = nameMatch.queriedName
    setNameMatch(null)
  }

  // 起始箭只在首次挂载时取一次：空面板对用户没有意义，且它**只能**来自后端（§11.5 ① 第 6 条）。
  const requested = useRef(false)
  useEffect(() => {
    if (requested.current || vehicle !== null) return
    requested.current = true
    void loadTemplate()
  }, [vehicle, loadTemplate])

  const unitsReady = Object.keys(unitTable).length > 0

  if (loadError !== null) {
    return (
      <section className="panel surface">
        <h2 className="panel__title label">参数面板</h2>
        <p className="vehicle-panel__error">
          {`起始箭载入失败（${loadError.code}）：${loadError.message}`}
        </p>
        <p className="vehicle-panel__hint">{loadError.suggestion}</p>
        <button
          type="button"
          className="vehicle-panel__button"
          onClick={() => {
            requested.current = true
            void loadTemplate()
          }}
        >
          重试载入起始箭
        </button>
      </section>
    )
  }

  if (vehicle === null) {
    return (
      <section className="panel surface">
        <h2 className="panel__title label">参数面板</h2>
        <p className="vehicle-panel__hint">{loading ? '正在载入起始箭…' : '尚未载入起始箭'}</p>
      </section>
    )
  }

  const renderFields = (specs: readonly FieldSpec[], prefix: string) =>
    specs.map((spec) => {
      const fieldPath = `${prefix}${spec.path}`
      return (
        <Field
          key={fieldPath}
          spec={spec}
          fieldPath={fieldPath}
          value={readFieldPath(vehicle, fieldPath)}
          unitTable={unitTable}
          sourced={sourcedFields[fieldPath]}
          onCommit={commitField}
        />
      )
    })

  return (
    <div className="vehicle-panel">
      <section className="panel surface">
        <h2 className="panel__title label">参数面板</h2>

        <p className="vehicle-panel__source-name">{label}</p>
        <p className="vehicle-panel__hint">{note}</p>
        <p className="vehicle-panel__hint">
          {unitsReady
            ? '数值按后端下发的显示单位（§6.4）；换算系数取自 GET /api/params/units。'
            : '单位表尚未载入（GET /api/params/units 未返回），当前一律按 SI 显示。'}
        </p>
        {diagnosing ? <p className="vehicle-panel__hint">诊断计算中…</p> : null}

        <div className="vehicle-panel__group">
          <h3 className="label">整箭</h3>
          {renderFields(VEHICLE_FIELDS, '')}
          {nameMatch !== null ? (
            <div>
              <p className="vehicle-panel__hint">
                {`检测到已知型号 ${nameMatch.response.name}（来源已核对）——载入其参数？`}
              </p>
              <p className="vehicle-panel__hint">
                <button type="button" className="vehicle-panel__button" onClick={loadMatchedTemplate}>
                  载入参数
                </button>{' '}
                <button type="button" className="vehicle-panel__button" onClick={ignoreMatchedTemplate}>
                  忽略
                </button>
              </p>
            </div>
          ) : null}
          {templateLoadError !== null ? (
            <p className="vehicle-panel__error">{`模板载入失败：${templateLoadError}`}</p>
          ) : null}
        </div>

        {vehicle.stages.map((stage, index) => {
          const prefix = `stages[${index}].`
          // 唯一权威（QA-1，v0.6.2）：default 语义下级层不存比冲——控件随之隐藏，
          // 载荷里也就不含这两个字段（后端取发动机标称值）。
          const ispOverridden = readFieldPath(vehicle, `${prefix}isp_source`) === 'custom'
          const stageSpecs = ispOverridden
            ? STAGE_FIELDS
            : STAGE_FIELDS.filter(
                (spec) => spec.path !== 'isp_vacuum_s' && spec.path !== 'isp_sea_level_s',
              )
          return (
            <div className="vehicle-panel__group" key={`stage-${index}`}>
              <h3 className="label">{`第 ${stage.index} 级（自下而上 · stages[${index}]）`}</h3>
              {renderFields(stageSpecs, prefix)}
              {renderFields(TANK_FIELDS, prefix)}
            </div>
          )
        })}

        <div className="vehicle-panel__group">
          <h3 className="label">任务</h3>
          {renderFields(MISSION_FIELDS, '')}
        </div>
      </section>
    </div>
  )
}
