import { useEffect, useRef, useState } from 'react'

import { fetchMaterials, type MaterialEntry } from '../api/materials'
import { fetchUnits, type Quantity, type Vehicle } from '../api/params'
import { fetchTemplateDetail, matchTemplateName, type TemplateMatchResponse } from '../api/templates'
import type { SourcedField, VehicleRecordResponse, VehicleSearchHit } from '../api/vehicleSearch'
import { fetchVehicleRecord, searchVehicleRecords } from '../api/vehicleSearch'
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

/** 下拉选项：value 进 store 与诊断载荷（材料 = 库内 id），label 仅用于展示。 */
interface SelectOption {
  value: string
  label: string
}

/** 一个可编辑字段的声明。`path` 是**相对路径**（级字段由面板补上 `stages[i].` 前缀）。 */
interface FieldSpec {
  path: string
  label: string
  /** 量；`null` = 无量纲（比例 / 计数 / 枚举）。 */
  quantity: Quantity | null
  /** 单位表不可用时的回退单位文本（= 内部 SI 符号，§1.4-3）。 */
  siUnit: string
  kind: 'number' | 'text' | 'select'
  /** 静态选项（value = label）；`optionsFrom: 'materials'` 的下拉不用它。 */
  options?: readonly string[]
  /** QA-3 材料引用化：选项动态来自 `fetchMaterials`（值域 = 库内材料 id）。 */
  optionsFrom?: 'materials'
  step?: number
  /** 输入框占位文本（省略语义字段用：注明「空 = 默认值」）。 */
  placeholder?: string
  /** 后端可省略的字段（如 flatness_ratio / separation_s）：**清空输入 = 写回 null**
   *  （null 即从载荷中移除，后端按默认值处理）；非 optional 字段清空不写回。 */
  optional?: boolean
}

const VEHICLE_FIELDS: readonly FieldSpec[] = [
  { path: 'name', label: '火箭名称', quantity: null, siUnit: '', kind: 'text' },
  { path: 'payload_mass_kg', label: '有效载荷质量', quantity: 'mass', siUnit: 'kg', kind: 'number', step: 100 },
  {
    path: 'material',
    label: '箭体材料（全局默认，可被各级覆盖）',
    quantity: null,
    siUnit: '',
    kind: 'select',
    optionsFrom: 'materials',
  },
]

/** 级字段：面板按 `stages[i].` 前缀展开为逐级控件（§6.1 的级层与发动机层）。 */
const STAGE_FIELDS: readonly FieldSpec[] = [
  { path: 'length_m', label: '级高度', quantity: 'length', siUnit: 'm', kind: 'number', step: 0.1 },
  { path: 'diameter_m', label: '级直径', quantity: 'length', siUnit: 'm', kind: 'number', step: 0.1 },
  {
    path: 'flatness_ratio',
    label: '扁度系数（封头短长轴比）',
    quantity: null,
    siUnit: '',
    kind: 'number',
    step: 0.05,
    placeholder: '空 = 0.5（2:1 椭圆封头）',
    optional: true,
  },
  { path: 'wall_thickness_m', label: '级壁厚', quantity: 'length', siUnit: 'm', kind: 'number', step: 0.0005 },
  {
    path: 'material',
    label: '该级材料（覆盖整箭默认）',
    quantity: null,
    siUnit: '',
    kind: 'select',
    optionsFrom: 'materials',
  },
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
    path: 'geometry.oxidizer_tank.material',
    label: '氧化剂箱材料',
    quantity: null,
    siUnit: '',
    kind: 'select',
    optionsFrom: 'materials',
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
    path: 'geometry.fuel_tank.material',
    label: '燃料箱材料',
    quantity: null,
    siUnit: '',
    kind: 'select',
    optionsFrom: 'materials',
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

/** 助推器组字段（OI-36 §6.1 Booster 层的组级参数；侧级 Stage 字段复用 STAGE_FIELDS）。 */
const BOOSTER_GROUP_FIELDS: readonly FieldSpec[] = [
  { path: 'count', label: '并联数量', quantity: null, siUnit: '', kind: 'number', step: 1 },
  {
    path: 'separation_s',
    label: '分离时刻',
    quantity: null,
    siUnit: 's',
    kind: 'number',
    step: 1,
    placeholder: '空 = 芯一级关机时刻',
    optional: true,
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

/**
 * 回收方案字段（§6.1 Recovery 层 / §8.9 三项代价挂点，M5 最小编辑）。
 *
 * 「是否回收」用独立复选框（启用时才创建 `recovery` 对象，不在载荷里预置任何数值）；
 * 其余字段全部可省略（清空 = 写回 null，后端按默认/未配置处理，不造值）。
 * 回收级号 `stage_indices` 本片不做逐级编辑（最小可用口径），经参数 JSON 导入可改。
 */
const RECOVERY_FIELDS: readonly FieldSpec[] = [
  {
    path: 'method',
    label: '回收方式（parachute 伞降 / propulsive 动力反推）',
    quantity: null,
    siUnit: '',
    kind: 'select',
    options: ['parachute', 'propulsive'],
  },
  {
    path: 'landing_propellant_margin_fraction',
    label: '着陆推进剂余量（占该级满装量）',
    quantity: null,
    siUnit: '',
    kind: 'number',
    step: 0.01,
    placeholder: '空 = 未填写',
    optional: true,
  },
  {
    path: 'system_mass_kg',
    label: '回收系统质量（本体外挂设备）',
    quantity: 'mass',
    siUnit: 'kg',
    kind: 'number',
    step: 10,
    placeholder: '空 = 未填写',
    optional: true,
  },
  {
    path: 'reinforcement_mass_kg',
    label: '增强结构与热防护质量',
    quantity: 'mass',
    siUnit: 'kg',
    kind: 'number',
    step: 10,
    placeholder: '空 = 未填写',
    optional: true,
  },
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

/** 材料下拉选项：label = 「名称（id）」；typical 条目尾缀 [典型值]（§1.4-4：工程典型值不得冒充实测手册值）。 */
function materialOption(material: MaterialEntry): SelectOption {
  const label = `${material.name}（${material.id}）`
  return {
    value: material.id,
    label: material.quality === 'typical' ? `${label}[典型值]` : label,
  }
}

/**
 * 材料库（QA-3 引用化）：挂载时拉一次，各层 material 下拉共用同一份 options。
 * 失败不阻断编辑——下拉降级为空 options，面板顶部给一行「材料库不可用」提示（不得白屏）。
 */
function useMaterials(): { options: readonly SelectOption[]; failed: boolean } {
  const [options, setOptions] = useState<readonly SelectOption[]>([])
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let alive = true
    fetchMaterials()
      .then((response) => {
        if (alive) setOptions(response.materials.map(materialOption))
      })
      .catch(() => {
        if (alive) setFailed(true)
      })
    return () => {
      alive = false
    }
  }, [])
  return { options, failed }
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
  /** 省略语义字段的占位文本（如「空 = 0.5」）。 */
  placeholder?: string
  /** 省略语义字段（optional）：清空输入时提交 null（从载荷移除该键，后端按默认处理）。 */
  onEmpty?: () => void
}

/**
 * 数值控件：**草稿字符串 + 提交时解析**。
 *
 * 为什么不能直接把 `Number(event.target.value)` 写回：输入 `0.0005` 的中间态 `0.`、`0.0`
 * 在 `Number` 下会退化成 `0`，逐字符重置会把小数点吃掉（壁厚、O/F 这类小数首当其冲）。
 */
function NumberField({ fieldPath, label, unitText, step, value, unsourced, source, onCommit, placeholder, onEmpty }: NumberFieldProps) {
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
        placeholder={placeholder}
        onChange={(event) => {
          setDraft(event.target.value)
          if (event.target.value.trim() === '') {
            // 省略语义字段：空 = null（后端默认值）；其余字段清空不写回（保留旧值待补）
            if (onEmpty !== undefined) onEmpty()
            return
          }
          const parsed = Number(event.target.value)
          if (Number.isFinite(parsed)) onCommit(parsed)
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
  /** 材料下拉的动态选项（QA-3）；静态枚举字段不用它。 */
  materialOptions: readonly SelectOption[]
  sourced: string | undefined
  onCommit: (path: string, value: unknown) => void
}

function Field({ spec, fieldPath, value, unitTable, materialOptions, sourced, onCommit }: FieldProps) {
  const unsourced = sourced === undefined
  const unitText =
    spec.quantity === null ? spec.siUnit : (unitSymbol(unitTable, spec.quantity) || spec.siUnit)
  // 出处标注（§11.5 ⑤ 规则 5）：有出处的字段在控件下方以小字呈现；用户改动后由
  // store 转为「用户修改」。无出处的字段不标注文字（只有「占位」徽标）。
  const sourceNode = sourced === undefined ? null : (
    <span className="vehicle-panel__source-name">{sourced}</span>
  )

  if (spec.kind === 'select') {
    // QA-3：材料下拉的 options 动态来自材料库（值 = 库 id）；静态枚举仍取声明里的 options。
    const selectOptions: readonly SelectOption[] =
      spec.optionsFrom === 'materials'
        ? materialOptions
        : (spec.options ?? []).map((option) => ({ value: option, label: option }))
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
          {selectOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
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
      placeholder={spec.placeholder}
      onEmpty={spec.optional ? () => onCommit(fieldPath, null) : undefined}
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
  const addBooster = useVehicleStore((state) => state.addBooster)
  const removeBooster = useVehicleStore((state) => state.removeBooster)
  const unitTable = useUnitTable()
  const materials = useMaterials()

  // —— OI-34 名称匹配（§11.5 ⑤ 规则 5）——
  // 命中只在提示条里呈现，绝不静默改写任何参数：载不载入由用户点按钮决定。
  const [nameMatch, setNameMatch] = useState<NameMatch | null>(null)
  const [templateLoadError, setTemplateLoadError] = useState<string | null>(null)
  const matchTimer = useRef<number | null>(null)
  const ignoredName = useRef<string | null>(null)

  // —— OI-39 型号检索（GCAT 全库）——
  // 候选下拉只呈现与出处摘要，载不载入同样由用户点选决定；精校模板命中置顶标注
  // 「精校」，但点击后仍走既有 OI-34 模板载入通路——两条通路 UI 不混、不静默代选。
  const [searchResult, setSearchResult] = useState<{ queriedName: string; hits: VehicleSearchHit[] } | null>(null)
  const [gcatRecord, setGcatRecord] = useState<VehicleRecordResponse | null>(null)
  const [recordError, setRecordError] = useState<string | null>(null)
  const [confirmNewVehicle, setConfirmNewVehicle] = useState(false)
  const searchTimer = useRef<number | null>(null)
  const searchToken = useRef(0)
  // —— 助推器组的展开状态（OI-36）：组级参数常驻，侧级 Stage 字段按需展开 ——
  const [expandedBoosters, setExpandedBoosters] = useState<Record<number, boolean>>({})

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

  /**
   * OI-39 型号检索：与名称匹配同一节奏（300ms 防抖），但 **≥2 字符才发**——
   * 单字符在 1 836 行的 lv 库里只会制造噪音候选。过期响应按序号作废；
   * 检索失败静默降级（辅助通路，不抢全局错误位）。
   */
  const scheduleVehicleSearch = (name: string) => {
    if (searchTimer.current !== null) window.clearTimeout(searchTimer.current)
    setSearchResult(null)
    if (name.trim().length < 2) return
    const token = ++searchToken.current
    searchTimer.current = window.setTimeout(() => {
      searchVehicleRecords(name)
        .then((response) => {
          if (token !== searchToken.current) return
          setSearchResult({ queriedName: name, hits: response.hits })
        })
        .catch(() => {
          // 检索失败不弹全局错误：候选下拉缺席即可，编辑通路不受影响。
        })
    }, NAME_MATCH_DEBOUNCE_MS)
  }

  // 卸载时清掉未触发的防抖回调（jsdom 测试与快速切换页面都靠它兜底）。
  useEffect(() => {
    return () => {
      if (matchTimer.current !== null) window.clearTimeout(matchTimer.current)
      if (searchTimer.current !== null) window.clearTimeout(searchTimer.current)
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
    if (path === 'name' && typeof value === 'string') {
      scheduleNameMatch(value)
      scheduleVehicleSearch(value)
    }
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

  /**
   * 点选 GCAT 候选 → 已知参数集载入（OI-39 ③）。
   *
   * - 名称与发射场**保留用户输入**（系统不静默代选，与模板载入同一纪律）；
   * - 可得字段经既有 `setField` 通路写入，GCAT 出处随写随记（`setSourcedFields`）；
   * - GCAT 缺失的字段**不写**（留空不造值，占位状态保持原样）；
   * - 比冲 / 推力不写入：GCAT 比冲是真空口径、推力环境未声明（OI-35），
   *   写入级参数会误进口径——它们只呈现在检索详情与出处标注里；
   * - GCAT 级数多于当前骨架时，多余级**不建**（不造结构，由用户决定加级）。
   */
  const loadGcatRecord = (hit: VehicleSearchHit) => {
    const current = useVehicleStore.getState().vehicle
    if (current === null) return
    setRecordError(null)
    fetchVehicleRecord(hit.name, hit.variant ?? undefined)
      .then((record) => {
        const latest = useVehicleStore.getState().vehicle
        if (latest === null) return
        const sourced: Record<string, string> = {}
        const setIfKnown = (path: string, field: SourcedField) => {
          if (typeof field.value !== 'number') return // null = GCAT 缺失，留空不造值
          setField(path, field.value)
          sourced[path] = field.source
        }
        setIfKnown('payload_mass_kg', record.vehicle.payload_leo_kg)
        record.stages.forEach((assembly, index) => {
          // 骨架级数少于 GCAT 装配时，多余级不建——写入不存在的 stages[i] 会抛错。
          if (assembly.record === null || index >= latest.stages.length) return
          const prefix = `stages[${index}].`
          setIfKnown(`${prefix}length_m`, assembly.record.length_m)
          setIfKnown(`${prefix}diameter_m`, assembly.record.diameter_m)
          setIfKnown(`${prefix}engine_count`, assembly.record.engine_count)
        })
        // 出处合并而非整体替换：先前「用户修改」的标注只在本此写入的字段上让位给 GCAT。
        setSourcedFields({ ...useVehicleStore.getState().sourcedFields, ...sourced })
        setGcatRecord(record)
        setSearchResult(null)
        useVehicleStore.getState().requestDiagnose()
      })
      .catch((error: unknown) => {
        // §10.3：点过的候选必须给个说法，失败不能静默。
        setRecordError(error instanceof Error ? error.message : '未知错误')
      })
  }

  /** 「新建空白火箭」：两步确认防误触，重置为起始箭骨架（fetchTemplate，§11.5 ① 第 6 条）。 */
  const handleNewVehicle = () => {
    if (!confirmNewVehicle) {
      setConfirmNewVehicle(true)
      return
    }
    setConfirmNewVehicle(false)
    setSearchResult(null)
    setGcatRecord(null)
    setRecordError(null)
    setNameMatch(null)
    ignoredName.current = null
    void loadTemplate()
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
          materialOptions={materials.options}
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
        {materials.failed ? (
          <p className="vehicle-panel__hint">
            材料库不可用（GET /api/catalog/materials 未返回）——材料下拉暂无可选项，其余参数仍可编辑。
          </p>
        ) : null}
        {diagnosing ? <p className="vehicle-panel__hint">诊断计算中…</p> : null}

        <div className="vehicle-panel__group">
          <h3 className="label">整箭</h3>
          {renderFields(VEHICLE_FIELDS, '')}
          {searchResult !== null && (searchResult.hits.length > 0 || nameMatch?.response.matched) ? (
            <ul className="vehicle-panel__search-list">
              {/* 精校模板命中置顶：标「精校」，点击走既有 OI-34 模板载入通路（UI 不混） */}
              {nameMatch !== null && nameMatch.response.matched ? (
                <li key="template-pinned">
                  <button
                    type="button"
                    className="vehicle-panel__search-item"
                    onClick={() => {
                      setSearchResult(null)
                      loadMatchedTemplate()
                    }}
                  >
                    <span>
                      <span className="vehicle-panel__badge">精校</span>
                      {` ${nameMatch.response.name}（内置模板）`}
                    </span>
                    <span className="vehicle-panel__availability">
                      来源已核对的精校模板——点击按模板载入，不静默代选
                    </span>
                  </button>
                </li>
              ) : null}
              {searchResult.hits.map((hit) => {
                const label = `${hit.name}${hit.variant === null ? '' : `（${hit.variant}）`}`
                const availability = [
                  hit.country ?? '国家未知',
                  hit.stage_count === null ? '级数未知' : `${hit.stage_count} 级`,
                  `质量${hit.availability.glow ? '✓' : '✗'}`,
                  `长度${hit.availability.length_m ? '✓' : '✗'}`,
                  `直径${hit.availability.diameter_m ? '✓' : '✗'}`,
                  `LEO${hit.availability.payload_leo_kg ? '✓' : '✗'}`,
                ].join(' · ')
                return (
                  <li key={hit.record_id}>
                    <button
                      type="button"
                      className="vehicle-panel__search-item"
                      aria-label={`载入 GCAT 记录 ${label}`}
                      onClick={() => loadGcatRecord(hit)}
                    >
                      <span>{label}</span>
                      <span className="vehicle-panel__availability">{availability}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          ) : searchResult !== null ? (
            <p className="vehicle-panel__hint">
              {`GCAT 目录中未找到与「${searchResult.queriedName}」匹配的型号（GCAT 为英文谱名库，可试型号代号）`}
            </p>
          ) : null}
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
          {gcatRecord !== null ? (
            <p className="vehicle-panel__hint">
              {`已从 GCAT ${gcatRecord.snapshot.id} 载入 ${gcatRecord.name}${
                gcatRecord.variant === null ? '' : `（${gcatRecord.variant}）`
              } 的已知参数——缺失 ${gcatRecord.missing.length} 项留空待补，全字段仍可编辑`}
            </p>
          ) : null}
          {gcatRecord !== null && gcatRecord.reference_only ? (
            <p className="vehicle-panel__error">GCAT 数据缺口较多，仅可作参照，建模需手动补参</p>
          ) : null}
          {gcatRecord?.warnings.map((warning, index) => (
            <p key={`gcat-warning-${index}`} className="vehicle-panel__hint">
              {`GCAT 装配提示：${warning}`}
            </p>
          ))}
          {recordError !== null ? (
            <p className="vehicle-panel__error">{`GCAT 参数载入失败：${recordError}`}</p>
          ) : null}
          <p className="vehicle-panel__hint">
            <button type="button" className="vehicle-panel__button" onClick={handleNewVehicle}>
              {confirmNewVehicle ? '确认新建？当前参数将被重置为起始箭骨架' : '新建空白火箭'}
            </button>
          </p>
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

        {/*
         * 助推器组（OI-36 / §6.1 Booster 层）：侧级与 Stage 同构复用——组级参数（数量 /
         * 分离时刻）常驻，侧级字段经既有 field_path 通路编辑（`boosters[i].stage.…`，
         * 下标解析复用 fieldPath.ts）。新增的侧级骨架 = 芯一级克隆（前端不造数），完整
         * 字段集可经参数 JSON 导入。
         */}
        <div className="vehicle-panel__group">
          <h3 className="label">并联助推器（boosters）</h3>
          <p className="vehicle-panel__hint">
            侧级与芯级同构（级号 0，与芯一级构成 0 级段）；新增组的骨架取自芯一级克隆，完整字段亦经参数 JSON 导入。
          </p>
          {(vehicle.boosters ?? []).map((booster, index) => {
            const expanded = expandedBoosters[index] === true
            return (
              <div className="vehicle-panel__booster" key={`booster-${index}`}>
                <div className="vehicle-panel__booster-head">
                  <button
                    type="button"
                    className="vehicle-panel__button"
                    aria-expanded={expanded}
                    onClick={() => {
                      setExpandedBoosters((prev) => ({ ...prev, [index]: !expanded }))
                    }}
                  >
                    {`助推器组 ${index + 1}（boosters[${index}] · ${booster.count} 枚）${expanded ? ' ▾' : ' ▸'}`}
                  </button>
                  <button
                    type="button"
                    className="vehicle-panel__button"
                    onClick={() => {
                      removeBooster(index)
                    }}
                  >
                    删除本组
                  </button>
                </div>
                {renderFields(BOOSTER_GROUP_FIELDS, `boosters[${index}].`)}
                {expanded ? (
                  <>
                    {renderFields(STAGE_FIELDS, `boosters[${index}].stage.`)}
                    {renderFields(TANK_FIELDS, `boosters[${index}].stage.`)}
                  </>
                ) : null}
              </div>
            )
          })}
          <p className="vehicle-panel__hint">
            <button
              type="button"
              className="vehicle-panel__button"
              onClick={() => {
                const next = (vehicle.boosters ?? []).length
                setExpandedBoosters({ [next]: true }) // 新组默认展开，改完再收起
                addBooster()
              }}
            >
              新增助推器组
            </button>
          </p>
        </div>

        {/*
         * 回收方案（§6.1 Recovery 层 / §8.9）：QA-4 挂点的最小编辑。启用复选框控制
         * `recovery` 对象的创建（未启用时载荷中不含该层）；启用后右栏「回收质量代价」
         * 面板按后端 `POST /api/sizing/sequence` 的响应呈现三项代价分解。
         */}
        <div className="vehicle-panel__group">
          <h3 className="label">回收（recovery）</h3>
          <label className="vehicle-panel__field">
            <span className="vehicle-panel__label label">启用回收（§8.9 三项代价挂点）</span>
            <input
              type="checkbox"
              data-field-path="recovery.enabled"
              checked={vehicle.recovery?.enabled === true}
              onChange={(event) => {
                if (event.target.checked) {
                  if (vehicle.recovery == null) {
                    // 首次启用：只建结构（enabled + 空级号清单），不预置任何数值
                    commitField('recovery', { enabled: true, stage_indices: [], method: null })
                  } else {
                    commitField('recovery.enabled', true)
                  }
                } else {
                  commitField('recovery.enabled', false)
                }
              }}
            />
          </label>
          {vehicle.recovery?.enabled === true ? (
            <>
              <p className="vehicle-panel__hint">
                回收级号（stage_indices）当前为{' '}
                {(vehicle.recovery.stage_indices ?? []).length > 0
                  ? vehicle.recovery.stage_indices.join('、')
                  : '空'}
                ，可经参数 JSON 导入指定。
              </p>
              {renderFields(RECOVERY_FIELDS, 'recovery.')}
            </>
          ) : (
            <p className="vehicle-panel__hint">
              未启用回收：右栏「回收质量代价」面板显示「无代价」。启用后可配置回收方式与三项代价参数。
            </p>
          )}
        </div>

        <div className="vehicle-panel__group">
          <h3 className="label">任务</h3>
          {renderFields(MISSION_FIELDS, '')}
        </div>
      </section>
    </div>
  )
}
