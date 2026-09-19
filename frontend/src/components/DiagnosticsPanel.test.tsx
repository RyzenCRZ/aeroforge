import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Diagnostic, DiagnoseResponse, RuleOutcome, Vehicle } from '../api/params'
import { useVehicleStore } from '../store/vehicle'
import { DiagnosticsPanel, highlightField, locateField } from './DiagnosticsPanel'

/**
 * 左栏「诊断清单」（规格 §6.5 / §11.5 ①）。
 *
 * 这一组用例的重心不是"画得对不对"，而是**三条账目必须分别可见**：
 * 命中项 / 未判定的规则（`deferred_reason`）/ 分支未启用（`uncovered`）——
 * 只渲染命中项会让"未判"与"通过"看起来一模一样，而"没报错 ≠ 正确"是本项目最贵的一课。
 *
 * 另一条是「路径 → 控件」定位的**降级行为**：定位失败时该条**仍须显示**，
 * 不得从清单里消失（§11.5 ① 第 4 条）。
 */

const HARD: Diagnostic = {
  level: 'hard',
  code: 'HARD_TWR_TOO_LOW',
  field_path: 'stages[0].engine.thrust_sea_level_n',
  message: '起飞推重比 0.8 低于下限 1.2',
  suggestion: '提高单机推力或减少起飞质量',
}

const WARN: Diagnostic = {
  level: 'warning',
  code: 'SAFETY_TANK_WALL_THIN',
  field_path: 'stages[0].geometry.oxidizer_tank.wall_thickness_m',
  message: '氧化剂箱壁厚 0.0004 m 低于最小工艺厚度 0.00051 m',
  suggestion: '加厚箱壁或改用更高强度材料',
}

/** 整箭层裁定：`field_path` 为空，面板应给出"无字段路径"而不是不显示。 */
const WARN_VEHICLE_LEVEL: Diagnostic = {
  level: 'warning',
  code: 'ENGINEER_AERO_DEFAULTED',
  field_path: '',
  message: '未提供气动参数，已按默认值 0.3 / 0.02 计算',
  suggestion: '如需精确结果，请补充气动参数',
}

const DEFERRED_REASON =
  '最小工艺厚度未配置（config.toml 无 min_tank_wall_thickness_m），该条判据本次不判定'

const UNCOVERED_NOTE = '仅判定了液体档（下限 1.2）；该级未标识为固体助推，固体档（1.5）未启用'

const RULES: RuleOutcome[] = [
  {
    code: 'SAFETY_TANK_WALL_MIN',
    title: '贮箱最小工艺厚度',
    level: 'warning',
    threshold: '未配置',
    source: '规格 §6.5 表 · 阈值由用户在设置项中配置',
    diagnostics: [],
    uncovered: [],
    deferred_reason: DEFERRED_REASON,
  },
  {
    code: 'TWR_TOO_LOW',
    title: '起飞推重比',
    level: 'hard',
    threshold: '≥ 1.2（液体）',
    source: '规格 §6.5 表 · 通用工程惯例',
    diagnostics: [HARD],
    uncovered: [UNCOVERED_NOTE],
    deferred_reason: null,
  },
  {
    code: 'ENGINEER_LENGTH_TO_DIAMETER',
    title: '长径比',
    level: 'warning',
    threshold: '∈ [5, 30]',
    source: '规格 §6.5 表 · 通用工程惯例',
    diagnostics: [],
    uncovered: [],
    deferred_reason: null,
  },
]

const REPORT: DiagnoseResponse = {
  constraints: [WARN_VEHICLE_LEVEL],
  diagnostics: [HARD, WARN],
  rules: RULES,
}

/** 面板只把 `vehicle` 当作"是否已载入起始箭"的开关，故此处的形状不参与断言。 */
const VEHICLE_STUB = { stages: [] } as unknown as Vehicle

interface Overrides {
  report?: DiagnoseResponse | null
  errorDiagnostics?: Diagnostic[]
  diagnoseError?: { code: string; stage: string; message: string; suggestion: string } | null
}

function setState(overrides: Overrides = {}): void {
  useVehicleStore.setState({
    vehicle: VEHICLE_STUB,
    report: REPORT,
    errorDiagnostics: [],
    diagnoseError: null,
    diagnosing: false,
    ...overrides,
  })
}

beforeEach(() => {
  // jsdom 未实现 scrollIntoView；定位高亮依赖它，故必须打桩。
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  // ⚠ 这里**不**调用 store.reset()：vitest 的 afterEach 按注册顺序**倒着**跑，
  // 本钩子会先于 RTL 的自动卸载执行，那样 reset 会在组件仍挂载时触发一次 store 更新
  // （表现为满屏 "not wrapped in act(...)"）。收尾统一放在 beforeEach。
  vi.restoreAllMocks()
  // 手工挂到 document 上的控件不属于 RTL 的渲染树，必须自己收尾：
  // 漏掉一个就会让下一条"定位必然失败"的用例变成假绿。
  for (const node of document.querySelectorAll('[data-field-path]')) node.remove()
})

describe('三条账目必须分别可见（§6.5 / §11.5 ① 第 2 条）', () => {
  it('命中项 / 未判定 / 分支未启用 / 已通过 四栏各自计数并渲染', () => {
    setState()
    render(<DiagnosticsPanel />)

    expect(screen.getByText('命中项（硬 1 · 警告 2）')).toBeInTheDocument()
    expect(screen.getByText('未判定的规则（1）')).toBeInTheDocument()
    expect(screen.getByText('分支未启用（1）')).toBeInTheDocument()
    expect(screen.getByText('已判定且通过（1）')).toBeInTheDocument()

    // 命中项：判定码与建议都必须在
    expect(screen.getByText('HARD_TWR_TOO_LOW')).toBeInTheDocument()
    expect(screen.getByText(HARD.suggestion)).toBeInTheDocument()
    expect(screen.getByText('SAFETY_TANK_WALL_THIN')).toBeInTheDocument()

    // 未判定：必须写明"缺什么"，而不只是列个码
    expect(screen.getByText(DEFERRED_REASON)).toBeInTheDocument()
    expect(screen.getByText(/不是一回事/)).toBeInTheDocument()

    // 分支未启用：写明未启用的分支
    expect(screen.getByText(UNCOVERED_NOTE)).toBeInTheDocument()

    // 已通过：以判定码列表呈现
    expect(screen.getByText('ENGINEER_LENGTH_TO_DIAMETER')).toBeInTheDocument()
  })

  it('阈值与来源照抄后端字段，前端不自拟措辞（OI-31）', () => {
    setState()
    render(<DiagnosticsPanel />)

    // 未判定的规则必须交代"判据是什么、来源在哪"，否则用户不知道缺的是什么
    expect(screen.getByText('阈值：未配置')).toBeInTheDocument()
    expect(screen.getByText('规格 §6.5 表 · 阈值由用户在设置项中配置')).toBeInTheDocument()
  })

  it('422 通路的字段级裁定与 200 的裁定合并渲染（§6.3 末注）', () => {
    setState({
      report: null,
      errorDiagnostics: [HARD],
      diagnoseError: {
        code: 'PARAMS_HARD_CONSTRAINT',
        stage: 'params',
        message: '起飞推重比低于硬约束下限',
        suggestion: '按裁定逐条修正',
      },
    })
    render(<DiagnosticsPanel />)

    expect(screen.getByText('命中项（硬 1 · 警告 0）')).toBeInTheDocument()
    expect(screen.getByText('HARD_TWR_TOO_LOW')).toBeInTheDocument()
    expect(screen.getByText(/诊断被拒绝（PARAMS_HARD_CONSTRAINT）/)).toBeInTheDocument()
  })

  it('三条账目未产出时显式说明，不得用「0 / 无」冒充「没有未判定的规则」', () => {
    // 硬约束 422 时后端拒绝进入诊断，`rules` 根本不存在。若照常渲染「未判定的规则（0）」，
    // 读起来会像"判过了、没有未判定的"——正是 §11.5 ① 第 2 条要拦的形态。
    setState({ report: null, errorDiagnostics: [HARD] })
    render(<DiagnosticsPanel />)

    expect(screen.getByText('未判定的规则')).toBeInTheDocument()
    expect(screen.queryByText('未判定的规则（0）')).not.toBeInTheDocument()
    expect(screen.queryByText('分支未启用（0）')).not.toBeInTheDocument()
    expect(screen.queryByText('已判定且通过（0）')).not.toBeInTheDocument()
    expect(screen.getAllByText(/本轮未产出逐规则账目/)).toHaveLength(3)
  })

  it('尚未载入起始箭时给出明确提示，而不是渲染一屏空栏', () => {
    useVehicleStore.setState({ vehicle: null })
    render(<DiagnosticsPanel />)

    expect(screen.getByText('尚未载入参数，无法诊断')).toBeInTheDocument()
  })
})

describe('「路径 → 控件」定位与降级（§11.5 ① 第 3、4 条）', () => {
  it('按 data-field-path **逐字**比对定位（路径含 [ ] . 不做选择器转义）', () => {
    const control = document.createElement('input')
    control.setAttribute('data-field-path', 'stages[0].engine.thrust_sea_level_n')
    document.body.appendChild(control)

    expect(locateField('stages[0].engine.thrust_sea_level_n')).toBe(control)
    expect(locateField('stages[0].engine.thrust_vacuum_n')).toBeNull()
    expect(locateField('')).toBeNull()

    control.remove()
  })

  it('命中时滚动并挂高亮类，返回 true', () => {
    const control = document.createElement('input')
    control.setAttribute('data-field-path', 'stages[0].engine.thrust_sea_level_n')
    document.body.appendChild(control)

    expect(highlightField('stages[0].engine.thrust_sea_level_n')).toBe(true)
    expect(control.classList.contains('field-located')).toBe(true)
    expect(control.scrollIntoView).toHaveBeenCalled()

    control.remove()
  })

  it('点击「定位到控件」命中已挂载的控件时加高亮，不显示降级提示', async () => {
    setState()
    const control = document.createElement('input')
    control.setAttribute('data-field-path', HARD.field_path)
    document.body.appendChild(control)

    render(<DiagnosticsPanel />)
    // 清单里有多条带路径的裁定，这里点的是第一条（HARD_TWR_TOO_LOW）
    await userEvent.click(screen.getAllByRole('button', { name: '定位到控件' })[0]!)

    expect(control.classList.contains('field-located')).toBe(true)
    expect(screen.queryByText(/已降级显示路径文本/)).not.toBeInTheDocument()

    control.remove()
  })

  it('定位失败时降级显示路径文本，且该条**仍在清单里**（不得丢弃）', async () => {
    setState()
    render(<DiagnosticsPanel />)

    // 没有挂载任何带 data-field-path 的控件 → 定位必然失败
    await userEvent.click(screen.getAllByRole('button', { name: '定位到控件' })[0]!)

    expect(screen.getByText(/已降级显示路径文本/)).toBeInTheDocument()
    // 该条仍显示，且带路径纯文本
    expect(screen.getByText('HARD_TWR_TOO_LOW')).toBeInTheDocument()
    expect(screen.getByText(HARD.field_path)).toBeInTheDocument()
    // 规格要求降级时路径"不可点击"——按不出结果的按钮必须撤掉，只留下另一条的
    expect(screen.getAllByRole('button', { name: '定位到控件' })).toHaveLength(1)
  })

  it('整箭层裁定（field_path 为空）不提供定位按钮，改为明说"整箭层判定"', () => {
    setState()
    render(<DiagnosticsPanel />)

    expect(screen.getByText('无字段路径（整箭层判定）')).toBeInTheDocument()
    // 有路径的两条（HARD 与 WARN）各一个按钮；整箭层那条没有
    expect(screen.getAllByRole('button', { name: '定位到控件' })).toHaveLength(2)
  })
})
