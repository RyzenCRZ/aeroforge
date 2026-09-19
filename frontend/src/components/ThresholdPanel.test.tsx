import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ThresholdEntry, ThresholdsResponse } from '../api/params'
import { ThresholdPanel } from './ThresholdPanel'

/**
 * 阈值设置项（规格 §1.7.3 OI-31 / §6.5 / §18.4）。
 *
 * 两条必须由用例钉死的性质：
 *
 * 1. **「未配置」是一个可见状态**，不是排版细节：判据在表内而无数值的项要显式写出
 *    "该条判据不判定"，界面**不得**替它编一个默认值，也不得老实留空。
 * 2. **显示的是生效值**（优先级：环境变量 > config.toml > 默认值）与它的**来源**；
 *    来源文字照抄后端 `source`，前端不得自拟措辞。
 */

const CONFIGURED: ThresholdEntry = {
  key: 'twr_solid_min',
  label: '固体助推起飞推重比下限',
  value: 1.5,
  source: '规格 §6.5 表 · 通用工程惯例（本项已由用户显式配置）',
  origin: 'config.toml',
  note: '生效边界：仅当该级标识为固体助推时使用此档。',
}

const UNSET: ThresholdEntry = {
  key: 'min_tank_wall_thickness_m',
  label: '贮箱最小工艺厚度',
  value: null,
  source: '规格 §6.5 表 · 阈值由用户在设置项中配置',
  origin: '未配置',
  note: '未配置时该条判据不判定（不得假定一个默认值）。',
}

function response(thresholds: ThresholdEntry[]): ThresholdsResponse {
  return { config_file: 'D:/data/aeroforge/config.toml', thresholds }
}

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(response([CONFIGURED, UNSET]))))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('生效值与来源（§18.4 / OI-31）', () => {
  it('已配置项显示生效值与来源，未配置项显式写「未配置」', async () => {
    render(<ThresholdPanel />)

    expect(await screen.findByText('生效值 1.5')).toBeInTheDocument()
    expect(screen.getByText('来源：config.toml')).toBeInTheDocument()
    expect(screen.getByText(`来源标注：${CONFIGURED.source}`)).toBeInTheDocument()

    expect(screen.getByText('未配置（该条判据不判定）')).toBeInTheDocument()
    expect(screen.getByText('来源：未配置')).toBeInTheDocument()
  })

  it('「未配置」项**不得**被顶替成一个默认值（输入框为空而非填了数）', async () => {
    render(<ThresholdPanel />)

    const input = await screen.findByLabelText(`贮箱最小工艺厚度（${UNSET.key}）`)
    expect(input).toHaveValue(null)

    const configured = screen.getByLabelText(`固体助推起飞推重比下限（${CONFIGURED.key}）`)
    expect(configured).toHaveValue(1.5)
  })

  it('写入目标路径可见（用户要知道改动落到哪个文件）', async () => {
    render(<ThresholdPanel />)

    expect(
      await screen.findByText('写入目标：D:/data/aeroforge/config.toml'),
    ).toBeInTheDocument()
  })
})

describe('写回 config.toml', () => {
  it('改值并离开输入框即 PUT，且请求体是 `{键: 值}`', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn(async (_path: string, init?: RequestInit) => {
      if (init?.method === 'PUT') {
        return jsonResponse(response([CONFIGURED, { ...UNSET, value: 0.00051, origin: 'config.toml' }]))
      }
      return jsonResponse(response([CONFIGURED, UNSET]))
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<ThresholdPanel />)
    const input = await screen.findByLabelText(`贮箱最小工艺厚度（${UNSET.key}）`)

    await user.type(input, '0.00051')
    await user.tab()

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2)
    })
    const [path, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit]
    expect(path).toBe('/api/params/thresholds')
    expect(init.method).toBe('PUT')
    expect(JSON.parse(String(init.body))).toEqual({ [UNSET.key]: 0.00051 })

    // 写入成功后界面跟随后端下发的**生效值**
    expect(await screen.findByText('生效值 0.00051')).toBeInTheDocument()
  })

  it('清空输入框并离开写回「未配置」（null = 删除该键），不拿默认值顶替', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn(async (_path: string, init?: RequestInit) =>
      init?.method === 'PUT'
        ? jsonResponse(response([{ ...CONFIGURED, value: null, origin: '未配置' }, UNSET]))
        : jsonResponse(response([CONFIGURED, UNSET])),
    )
    vi.stubGlobal('fetch', fetchMock)

    render(<ThresholdPanel />)
    const input = await screen.findByLabelText(`固体助推起飞推重比下限（${CONFIGURED.key}）`)

    await user.clear(input)
    await user.tab()

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2)
    })
    const [, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit]
    expect(JSON.parse(String(init.body))).toEqual({ [CONFIGURED.key]: null })
  })

  it('没改值就离开不发请求（不得把一次点击变成一次写盘）', async () => {
    const user = userEvent.setup()
    const fetchMock = vi.fn(async () => jsonResponse(response([CONFIGURED, UNSET])))
    vi.stubGlobal('fetch', fetchMock)

    render(<ThresholdPanel />)
    const input = await screen.findByLabelText(`固体助推起飞推重比下限（${CONFIGURED.key}）`)

    await user.click(input)
    await user.tab()

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
