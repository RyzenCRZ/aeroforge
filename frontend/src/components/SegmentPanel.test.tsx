import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it } from 'vitest'

import { useParamsStore } from '../store/params'
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
