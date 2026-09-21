import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { useParamsStore } from '../store/params'
import { useModelStore } from '../store/model'
import { useViewStore } from '../store/view'
import { SegmentPanel } from './SegmentPanel'

/**
 * 组件树 ↔ 视口双向联动（规格 §11.4 / §13.6）。
 *
 * 粒度是**母线段链**：树上的第 i 行 = 视口里的 `seg-<i>`。两个方向都必须成立，否则
 * "在树上隐藏了某段，视口却没变"（或反之）——这正是 OI-19 落地前实测到的病：
 * GLB 只有 1 个无名字节点，按名找不到就**不隐藏**，全程零报错。
 *
 * 本文件只验**树侧**与共享状态：视口侧的读取由 `r3f/sceneState.test.ts` 的两个通道
 * 同粒度用例覆盖；两侧都读同一个 `hiddenSegments` / `selectedSegment`。
 */

// 在**渲染前**复位（而非 afterEach）：afterEach 里改 store 会落在组件卸载前，
// 触发"未包裹 act"的告警，把真实失败淹没在噪声里
beforeEach(() => {
  useViewStore.getState().reset()
  useParamsStore.getState().reset()
  useModelStore.getState().reset()
})

describe('显隐（树 → 视口）', () => {
  it('取消勾选即隐藏该段：写进 store/view 的 hiddenSegments（视口读的就是这一份）', async () => {
    const user = userEvent.setup()
    render(<SegmentPanel />)

    await user.click(screen.getByLabelText('显示第 1 段'))

    expect([...useViewStore.getState().hiddenSegments]).toEqual([1])
    expect(screen.getByLabelText('显示第 1 段')).not.toBeChecked()
    // 其余段不受影响——隐藏必须**精确到段**，不能顺手把邻居一起带走
    expect(screen.getByLabelText('显示第 0 段')).toBeChecked()
    expect(screen.getByLabelText('显示第 2 段')).toBeChecked()
  })

  it('再次勾选即恢复显示', async () => {
    const user = userEvent.setup()
    render(<SegmentPanel />)

    const checkbox = screen.getByLabelText('显示第 1 段')
    await user.click(checkbox)
    await user.click(checkbox)

    expect([...useViewStore.getState().hiddenSegments]).toEqual([])
    expect(checkbox).toBeChecked()
  })

  it('「全部显示」一次清空；无隐藏段时该按钮不可用（不给无意义的可点项）', async () => {
    const user = userEvent.setup()
    render(<SegmentPanel />)

    const showAll = screen.getByRole('button', { name: '全部显示' })
    expect(showAll).toBeDisabled()

    await user.click(screen.getByLabelText('显示第 0 段'))
    await user.click(screen.getByLabelText('显示第 2 段'))
    expect(showAll).toBeEnabled()

    await user.click(showAll)
    expect([...useViewStore.getState().hiddenSegments]).toEqual([])
  })
})

describe('选中（树 ↔ 视口双向）', () => {
  it('点树上的段 = 选中该段（视口据此高亮该 `seg-<i>`）', async () => {
    const user = userEvent.setup()
    render(<SegmentPanel />)

    await user.click(screen.getByRole('button', { name: /#2 / }))

    expect(useViewStore.getState().selectedSegment).toBe(2)
    expect(screen.getByText('正在编辑段 #2')).toBeInTheDocument()
  })

  it('反向：视口点选写同一个字段，树上的选中态随之变化', () => {
    render(<SegmentPanel />)

    // 视口里的点击最终就是这一句（见 `AuthoritativeModel` / `SchematicMesh` 的 onSelect）
    act(() => useViewStore.getState().selectSegment(0))

    expect(screen.getByRole('button', { name: /#0 / })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: /#2 / })).toHaveAttribute('aria-pressed', 'false')
  })

  it('选中退化到越界下标时不崩：参数表单退回「未选中段」而不是抛异常', () => {
    render(<SegmentPanel />)

    act(() => useViewStore.getState().selectSegment(9))

    expect(screen.getByText('未选中段（段链为空）')).toBeInTheDocument()
  })
})

/** 选中第 index 段并把类型切到 type（六曲线族用例的公共前置）。 */
async function selectSegmentAndType(index: number, type: string): Promise<void> {
  const user = userEvent.setup()
  render(<SegmentPanel />)
  await user.click(screen.getByRole('button', { name: new RegExp(`#${index} `) }))
  await user.selectOptions(screen.getByLabelText('段类型'), type)
}

describe('M5 六曲线族：下拉与各族参数字段（§5.2 / §5.3）', () => {
  it('段类型下拉扩至九族：M1 三段型 + 六新族', async () => {
    const user = userEvent.setup()
    render(<SegmentPanel />)
    // 下拉挂在选中段的参数表单里：先选中一段
    await user.click(screen.getByRole('button', { name: /#1 / }))

    const dropdown = screen.getByLabelText('段类型') as HTMLSelectElement
    const values = [...dropdown.options].map((option) => option.value)
    expect(values).toEqual([
      'line',
      'arc',
      'ellipse',
      'ogive',
      'parabola',
      'von_karman',
      'power',
      'bell',
      'spline',
    ])
  })

  it('抛物线：渲染系数 K 字段（Schema 默认 K=1）', async () => {
    await selectSegmentAndType(1, 'parabola')

    const coefficient = screen.getByLabelText(/抛物线系数/) as HTMLInputElement
    expect(coefficient.value).toBe('1')
    expect(useParamsStore.getState().profile.segments[1]).toEqual({
      type: 'parabola',
      length: 3,
      end_radius: 1,
      coefficient: 1,
    })
  })

  it('幂律：渲染指数 n 字段（追加默认 n=1.5）', async () => {
    await selectSegmentAndType(1, 'power')

    expect((screen.getByLabelText(/幂律指数/) as HTMLInputElement).value).toBe('1.5')
  })

  it('钟形喷管：渲染喉部半径与长度比两个字段（出口半径复用公共 end_radius）', async () => {
    await selectSegmentAndType(1, 'bell')

    expect(screen.getByLabelText(/喉部半径/)).toBeInTheDocument()
    expect(screen.getByLabelText(/钟形长度比/)).toBeInTheDocument()
    expect(screen.getByText(/出口半径 = 上方 end_radius/)).toBeInTheDocument()
  })

  it('样条：控制点行列表增删可用（最小行列编辑）', async () => {
    await selectSegmentAndType(1, 'spline')

    // 默认 1 个内部控制点（r=起点半径 1，z=0.5）
    expect(screen.getByLabelText('控制点 1 半径 r（m）')).toBeInTheDocument()
    expect(screen.queryByLabelText('控制点 2 半径 r（m）')).not.toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('button', { name: '添加控制点' }))
    expect(screen.getByLabelText('控制点 2 半径 r（m）')).toBeInTheDocument()

    // 删除第 2 点后回到 1 行；仅剩 1 行时删除按钮禁用（Schema min_length=1）
    await userEvent.setup().click(screen.getByRole('button', { name: '删除控制点 2' }))
    expect(screen.queryByLabelText('控制点 2 半径 r（m）')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '删除控制点 1' })).toBeDisabled()
  })

  it('切线卵形 / 冯·卡门：无族参数，渲染派生量说明（不造输入框）', async () => {
    await selectSegmentAndType(1, 'ogive')
    expect(screen.getByText(/长径比 L\/\(2R\)/)).toBeInTheDocument()
    expect(screen.getByText('派生量')).toBeInTheDocument()

    await userEvent.setup().selectOptions(screen.getByLabelText('段类型'), 'von_karman')
    expect(screen.getByText(/冯·卡门（Haack C=0）无族参数/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/抛物线系数/)).not.toBeInTheDocument()
  })
})

describe('M5 段类型切换：公共字段保留 / 族参数清空', () => {
  it('类型变更保留公共字段（length / end_radius 即 z 起止），不重置为默认', async () => {
    await selectSegmentAndType(1, 'parabola')

    expect((screen.getByLabelText(/轴向跨度/) as HTMLInputElement).value).toBe('3')
    expect((screen.getByLabelText(/末端半径/) as HTMLInputElement).value).toBe('1')
  })

  it('类型变更清空旧族参数：切走后不残留，切回时取该族默认（不沿用旧值）', async () => {
    const user = userEvent.setup()
    render(<SegmentPanel />)
    await user.click(screen.getByRole('button', { name: /#1 / }))
    await user.selectOptions(screen.getByLabelText('段类型'), 'parabola')

    // 用户把系数改成 0.5 后切走
    const coefficient = screen.getByLabelText(/抛物线系数/)
    await user.clear(coefficient)
    await user.type(coefficient, '0.5')
    expect(useParamsStore.getState().profile.segments[1]).toMatchObject({ coefficient: 0.5 })

    await user.selectOptions(screen.getByLabelText('段类型'), 'line')
    // 精确相等：旧族的 coefficient 不得跟到 line 段上（后端 extra="forbid"）
    expect(useParamsStore.getState().profile.segments[1]).toEqual({
      type: 'line',
      length: 3,
      end_radius: 1,
    })

    // 切回抛物线：系数回到该族默认 1，而不是记忆里的 0.5
    await user.selectOptions(screen.getByLabelText('段类型'), 'parabola')
    expect(useParamsStore.getState().profile.segments[1]).toMatchObject({ coefficient: 1 })
  })
})

describe('M5 校验错误呈现（复用 store/model 的既有约束错误体系）', () => {
  it('validate 通路失败时，参数表单旁呈现一行错误（code + message）', () => {
    render(<SegmentPanel />)
    expect(screen.queryByText(/母线校验未通过/)).not.toBeInTheDocument()

    act(() => {
      useModelStore.setState({
        error: {
          code: 'PARAMS_VALUE_ERROR',
          stage: 'api',
          message: '抛物线系数 K 越界',
          suggestion: '检查取值范围',
          origin: 'validate',
        },
      })
    })

    expect(
      screen.getByText('母线校验未通过（PARAMS_VALUE_ERROR）：抛物线系数 K 越界'),
    ).toBeInTheDocument()
  })

  it('构建（build）通路的错误不在母线编辑参数区复述（那里由编辑器错误块负责）', () => {
    render(<SegmentPanel />)

    act(() => {
      useModelStore.setState({
        error: {
          code: 'BUILD_FAILED',
          stage: 'kernel',
          message: '内核构建失败',
          suggestion: '重试',
          origin: 'build',
        },
      })
    })

    expect(screen.queryByText(/母线校验未通过/)).not.toBeInTheDocument()
  })
})
