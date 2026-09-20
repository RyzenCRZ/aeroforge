import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import type { Vehicle } from '../api/params'
import type { BandSection, SectionBand, SectionsResponse, SectionsStage } from '../api/sections'
import { useVehicleStore } from '../store/vehicle'
import { Profile2D } from './Profile2D'

/**
 * 右栏 2D 视图组（外观图 + 工程剖面图，规格 §11.10 / OI-37）。
 *
 * 这组用例钉死的性质：
 *
 * 1. **读数据渲染，不硬编码**：条带数量 / 顺序完全跟随后端 bands 数组
 *    （含 tank_order 翻转用例——数据顺序变渲染顺序变，§5.9 非铁律）；缺失分区不渲染。
 * 2. **液体 / 气枕 / 共底 / 隔热层**按后端字段寻址渲染（SVG 属性断言）。
 * 3. **输送管两种走法**元素位置不同：external 在体外、internal 穿下箱。
 * 4. **尺寸标注**全部渲染（§5.9 共性 7）；**缩放两图独立**；失败降级不白屏；
 *    vehicle 快速变更经 300 ms 防抖只发最后一次请求。
 */

const LABEL_ZH: Record<BandSection, string> = {
  fairing: '整流罩',
  adapter: '载荷适配器',
  avionics: '仪器舱',
  forward_skirt: '前裙',
  ox_tank: '氧化剂箱',
  intertank: '级间舱',
  common_bulkhead: '共底',
  fuel_tank: '燃料箱',
  thrust_structure: '推力结构',
  engine_bay: '发动机舱',
  interstage: '级间段',
}

function band(section: BandSection, lengthM: number, extra: Partial<SectionBand> = {}): SectionBand {
  return { section, label_zh: LABEL_ZH[section], length_m: lengthM, ...extra }
}

function stage(
  index: number,
  level: number,
  bands: SectionBand[],
  routing: 'external' | 'internal' = 'external',
  tankOrder: 'oxidizer_first' | 'fuel_first' = 'oxidizer_first',
): SectionsStage {
  return {
    stage_index: index,
    level,
    bands,
    delivery_pipe_routing: routing,
    tank_order: tankOrder,
  }
}

function dimensions(totalLengthM: number): SectionsResponse['dimensions'] {
  return {
    total_length_m: totalLengthM,
    max_diameter_m: 3.35,
    fairing_diameter_m: 4.2,
    labels: [
      { key: 'total_length', text: '总长 24.0 m' },
      { key: 'max_diameter', text: '最大直径 3.35 m' },
      { key: 'fairing_diameter', text: '整流罩直径 4.2 m' },
    ],
  }
}

/** 单级箭夹具（氧箱在上，external 输送管）。 */
const SINGLE_STAGE_BANDS = [
  band('forward_skirt', 1.5),
  band('ox_tank', 10, { liquid_level_m: 8, color_key: 'lox', propellant_oxidizer: 'LOX' }),
  band('intertank', 1),
  band('fuel_tank', 6, { liquid_level_m: 4.8, color_key: 'kero', propellant_fuel: 'RP-1' }),
  band('thrust_structure', 2),
  band('engine_bay', 3.5),
]

const SINGLE_RESPONSE: SectionsResponse = {
  stages: [stage(1, 1, SINGLE_STAGE_BANDS)],
  boosters: [],
  dimensions: dimensions(24),
  warnings: [],
  provenance: {},
}

/** 共底夹具：共底条带带级长缩减量与隔热层（§5.9 口径 2）。 */
const BULKHEAD_RESPONSE: SectionsResponse = {
  stages: [
    stage(1, 1, [
      band('forward_skirt', 1.5),
      band('ox_tank', 10, { liquid_level_m: 9.5, color_key: 'lox' }),
      band('common_bulkhead', 0.5, { bulkhead_saving_m: 3.2, insulation: true }),
      band('fuel_tank', 6, { liquid_level_m: 5.7, color_key: 'lh2' }),
      band('thrust_structure', 2),
      band('engine_bay', 2),
    ]),
  ],
  boosters: [],
  dimensions: dimensions(22),
  warnings: [],
  provenance: {},
}

/** 两级夹具（level 2 在上、含 fairing；level 1 在底、含 interstage）。 */
const TWO_STAGE_RESPONSE: SectionsResponse = {
  stages: [
    stage(2, 2, [
      band('fairing', 6),
      band('avionics', 1.5),
      band('ox_tank', 8, { liquid_level_m: 7.6, color_key: 'ch4' }),
      band('intertank', 1),
      band('fuel_tank', 4, { liquid_level_m: 3.8, color_key: 'lh2' }),
      band('thrust_structure', 2),
      band('engine_bay', 1.5),
    ]),
    stage(1, 1, [
      band('forward_skirt', 1.5),
      band('ox_tank', 10, { liquid_level_m: 9.5, color_key: 'lox' }),
      band('fuel_tank', 6, { liquid_level_m: 5.7, color_key: 'kero' }),
      band('thrust_structure', 2.5),
      band('engine_bay', 3),
      band('interstage', 1),
    ]),
  ],
  boosters: [],
  dimensions: dimensions(48),
  warnings: [],
  provenance: {},
}

/** tank_order 翻转夹具：后端已把燃料箱排在氧化剂箱上方（数据顺序变 → 渲染顺序应变）。 */
const TANK_FLIP_RESPONSE: SectionsResponse = {
  stages: [
    stage(
      1,
      1,
      [
        band('forward_skirt', 1.5),
        band('fuel_tank', 6, { liquid_level_m: 4.8, color_key: 'kero' }),
        band('intertank', 1),
        band('ox_tank', 10, { liquid_level_m: 8, color_key: 'lox' }),
        band('thrust_structure', 2),
        band('engine_bay', 3.5),
      ],
      'external',
      'fuel_first',
    ),
  ],
  boosters: [],
  dimensions: dimensions(24),
  warnings: [],
  provenance: {},
}

/** internal 输送管夹具：同一单级箭，走法换内置穿越。 */
const INTERNAL_RESPONSE: SectionsResponse = {
  ...SINGLE_RESPONSE,
  stages: [stage(1, 1, SINGLE_STAGE_BANDS, 'internal')],
}

type FetchMock = Mock<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response
}

/** POST /api/geometry/sections 替身；ok = false 时走 500 错误体（验降级）。 */
function mockSectionsFetch(body: unknown, ok = true): FetchMock {
  return vi.fn(async () =>
    ok
      ? jsonResponse(body)
      : ({
          ok: false,
          status: 500,
          json: async () => ({
            error: {
              code: 'SECTIONS_FAILED',
              stage: 'geometry',
              message: '分区计算失败',
              suggestion: '查看后端日志',
            },
          }),
        }) as unknown as Response,
  )
}

/** 面板只把 vehicle 当作"已载入参数"的开关与请求体，形状不参与断言。 */
function vehicleNamed(name: string): Vehicle {
  return { name, stages: [] } as unknown as Vehicle
}

function requestBodyNames(mock: FetchMock): string[] {
  return mock.mock.calls.map(([, init]) => {
    const body = JSON.parse(String((init as RequestInit).body)) as { name: string }
    return body.name
  })
}

/** 载入 vehicle → 渲染 → 等 300 ms 防抖后的分区数据到位（超时给足 ≥4 s）。 */
async function renderWithVehicle(vehicle: Vehicle, mock: FetchMock): Promise<void> {
  useVehicleStore.setState({ vehicle })
  render(<Profile2D />)
  await waitFor(() => expect(screen.getByTestId('profile-svg')).toBeInTheDocument(), { timeout: 4000 })
  expect(mock).toHaveBeenCalled()
}

/** profile-svg 内条带的 data-band-section 序列（自上而下）。 */
function bandSectionSequence(svg: HTMLElement): (string | null)[] {
  return Array.from(svg.querySelectorAll('[data-band-section]')).map((el) =>
    el.getAttribute('data-band-section'),
  )
}

beforeEach(() => {
  useVehicleStore.setState({ vehicle: null })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('分区条带（读数据渲染，不硬编码）', () => {
  it('条带数量与顺序与后端 bands 数组一致（§11.10）', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('单级箭'), mock)

    const svg = screen.getByTestId('profile-svg')
    expect(svg.querySelectorAll('[data-testid^="profile-band-"]')).toHaveLength(6)
    expect(bandSectionSequence(svg)).toEqual([
      'forward_skirt',
      'ox_tank',
      'intertank',
      'fuel_tank',
      'thrust_structure',
      'engine_bay',
    ])
  })

  it('tank_order 翻转：数据顺序变 → 渲染顺序变（§5.9 非铁律，不硬编码氧箱在上）', async () => {
    const mock = mockSectionsFetch(TANK_FLIP_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('燃料在上'), mock)

    expect(bandSectionSequence(screen.getByTestId('profile-svg'))).toEqual([
      'forward_skirt',
      'fuel_tank',
      'intertank',
      'ox_tank',
      'thrust_structure',
      'engine_bay',
    ])
  })

  it('多级：level 降序自上而下（上面级含 fairing，底级含 interstage）', async () => {
    const mock = mockSectionsFetch(TWO_STAGE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('两级箭'), mock)

    expect(bandSectionSequence(screen.getByTestId('profile-svg'))).toEqual([
      'fairing',
      'avionics',
      'ox_tank',
      'intertank',
      'fuel_tank',
      'thrust_structure',
      'engine_bay',
      'forward_skirt',
      'ox_tank',
      'fuel_tank',
      'thrust_structure',
      'engine_bay',
      'interstage',
    ])
  })

  it('缺失分区不在响应数组里 → 不渲染（按名寻址）', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('单级箭'), mock)

    const svg = screen.getByTestId('profile-svg')
    expect(svg.querySelector('[data-band-section="fairing"]')).toBeNull()
    expect(svg.querySelector('[data-band-section="interstage"]')).toBeNull()
    expect(svg.querySelector('[data-band-section="common_bulkhead"]')).toBeNull()
  })
})

describe('液面 / 气枕（§11.10）', () => {
  it('液体占条带底部 liquid_level_m，气枕在其上且 15% 透明度', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('单级箭'), mock)

    const svg = screen.getByTestId('profile-svg')
    // band-1 = ox_tank（length 10，liquid_level_m 8 → 液体占 0.8）
    const tankGroup = svg.querySelector('[data-testid="profile-band-1"]')
    expect(tankGroup).not.toBeNull()
    const bandRect = tankGroup!.querySelector('rect')!
    const liquid = svg.querySelector('[data-testid="profile-liquid-1"]') as SVGRectElement
    const ullage = svg.querySelector('[data-testid="profile-ullage-1"]') as SVGRectElement

    const bandH = parseFloat(bandRect.getAttribute('height')!)
    const bandY = parseFloat(bandRect.getAttribute('y')!)
    const liquidH = parseFloat(liquid.getAttribute('height')!)
    const ullageH = parseFloat(ullage.getAttribute('height')!)

    expect(liquidH / bandH).toBeCloseTo(0.8, 6)
    expect(ullageH / bandH).toBeCloseTo(0.2, 6)
    // 液体区底边与条带底边对齐（液体在底部、气枕在顶部）
    expect(parseFloat(liquid.getAttribute('y')!) + liquidH).toBeCloseTo(bandY + bandH, 6)
    expect(parseFloat(ullage.getAttribute('y')!)).toBeCloseTo(bandY, 6)
    // 气枕 15% 透明度（§11.10）
    expect(ullage.getAttribute('fill-opacity')).toBe('0.15')
  })

  it('推进剂编码色取既有 CSS 变量（§11.2 / NFR-05：无色值字面量）', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('单级箭'), mock)

    const svg = screen.getByTestId('profile-svg')
    const liquidOx = svg.querySelector('[data-testid="profile-liquid-1"]') as SVGRectElement
    const liquidFuel = svg.querySelector('[data-testid="profile-liquid-3"]') as SVGRectElement
    expect(liquidOx.getAttribute('style')).toContain('var(--color-prop-lox)')
    expect(liquidFuel.getAttribute('style')).toContain('var(--color-prop-kero)')
  })

  it('无 liquid_level_m 的条带不画液体（缺失字段不造值）', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('单级箭'), mock)

    const svg = screen.getByTestId('profile-svg')
    expect(svg.querySelector('[data-testid="profile-liquid-0"]')).toBeNull()
    expect(svg.querySelector('[data-testid="profile-ullage-0"]')).toBeNull()
  })
})

describe('共底与隔热层（§5.9 口径 2）', () => {
  it('共底画单线并右侧标注级长缩减量；隔热层画斜线填充', async () => {
    const mock = mockSectionsFetch(BULKHEAD_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('共底箭'), mock)

    const svg = screen.getByTestId('profile-svg')
    // band-2 = common_bulkhead
    expect(svg.querySelector('[data-testid="profile-bulkhead-line-2"]')).not.toBeNull()
    const bulkGroup = svg.querySelector('[data-testid="profile-band-2"]')!
    const label = bulkGroup.querySelector('text')
    expect(label?.textContent).toContain('共底节省')
    expect(label?.textContent).toContain('3.20')
    // 隔热层：斜线元素与其填充定义都在
    expect(svg.querySelector('[data-testid="profile-insulation-2"]')).not.toBeNull()
    expect(svg.querySelector('#profile2d-hatch')).not.toBeNull()
  })

  it('无共底构型不出现共底线与缩减量标注', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('非共底'), mock)

    const svg = screen.getByTestId('profile-svg')
    expect(svg.querySelector('[data-testid^="profile-bulkhead-line-"]')).toBeNull()
    expect(screen.queryByText(/共底节省/)).not.toBeInTheDocument()
  })
})

describe('输送管道两种走法（§5.9 口径 3，只画走法）', () => {
  it('external：管道沿箭体外侧（x 在体宽之外），无内部管道元素', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('外置管'), mock)

    const svg = screen.getByTestId('profile-svg')
    const pipe = svg.querySelector('[data-testid^="profile-pipe-external-"]') as SVGLineElement
    expect(pipe).not.toBeNull()
    const bandRect = svg.querySelector('[data-testid="profile-band-0"] rect') as SVGRectElement
    const bodyRight =
      parseFloat(bandRect.getAttribute('x')!) + parseFloat(bandRect.getAttribute('width')!)
    expect(parseFloat(pipe.getAttribute('x1')!)).toBeGreaterThan(bodyRight)
    expect(svg.querySelector('[data-testid^="profile-pipe-internal-"]')).toBeNull()
  })

  it('internal：管道穿下箱内部（x 在体宽内，纵向自上箱贯到下箱底）', async () => {
    const mock = mockSectionsFetch(INTERNAL_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('内置管'), mock)

    const svg = screen.getByTestId('profile-svg')
    const pipe = svg.querySelector('[data-testid^="profile-pipe-internal-"]') as SVGLineElement
    expect(pipe).not.toBeNull()
    // 下箱 = fuel_tank（条带自上而下，燃料箱在氧化剂箱之下）
    const fuelRect = svg.querySelector('[data-band-section="fuel_tank"] rect') as SVGRectElement
    const left = parseFloat(fuelRect.getAttribute('x')!)
    const width = parseFloat(fuelRect.getAttribute('width')!)
    const pipeX = parseFloat(pipe.getAttribute('x1')!)
    expect(pipeX).toBeGreaterThan(left)
    expect(pipeX).toBeLessThan(left + width)

    const fuelTop = parseFloat(fuelRect.getAttribute('y')!)
    const fuelBottom = fuelTop + parseFloat(fuelRect.getAttribute('height')!)
    // 管道自上箱顶部起（y1 高于下箱顶），贯穿至下箱底（y2 与下箱底对齐）
    expect(parseFloat(pipe.getAttribute('y1')!)).toBeLessThan(fuelTop)
    expect(parseFloat(pipe.getAttribute('y2')!)).toBeCloseTo(fuelBottom, 6)
    expect(svg.querySelector('[data-testid^="profile-pipe-external-"]')).toBeNull()
  })
})

describe('尺寸标注（§5.9 共性 7：全部渲染，不省略）', () => {
  it('dimensions.labels 逐条渲染', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('单级箭'), mock)

    for (const label of SINGLE_RESPONSE.dimensions.labels) {
      expect(screen.getByTestId(`profile-dim-${label.key}`)).toHaveTextContent(label.text)
    }
  })
})

describe('外观图（退化渲染，OI-37）', () => {
  it('只画外轮廓与级段分界：两级 → 1 条内部线；无液体 / 标注 / 管道', async () => {
    const mock = mockSectionsFetch(TWO_STAGE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('两级箭'), mock)

    const outline = screen.getByTestId('outline-svg')
    expect(outline.querySelectorAll('[data-testid^="outline-band-"]')).toHaveLength(13)
    expect(outline.querySelectorAll('[data-testid^="outline-stage-boundary-"]')).toHaveLength(1)
    // 退化渲染：不画液体 / 引线标注 / 尺寸标注 / 管道（§11.10 外观图口径）
    expect(outline.querySelectorAll('text')).toHaveLength(0)
    expect(outline.querySelector('[fill-opacity]')).toBeNull()
    expect(outline.querySelectorAll('[data-testid^="profile-"]')).toHaveLength(0)
  })

  it('单级 → 0 条内部级段线（n 级 → n−1 条）', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('单级箭'), mock)

    const outline = screen.getByTestId('outline-svg')
    expect(outline.querySelectorAll('[data-testid^="outline-band-"]')).toHaveLength(6)
    expect(outline.querySelectorAll('[data-testid^="outline-stage-boundary-"]')).toHaveLength(0)
  })
})

describe('缩放（两图状态独立，§11.10）', () => {
  it('＋ / － / 复位只改本图 viewBox，另一张图不受影响', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    await renderWithVehicle(vehicleNamed('单级箭'), mock)

    const profileSvg = screen.getByTestId('profile-svg')
    const outlineSvg = screen.getByTestId('outline-svg')
    const profileBase = profileSvg.getAttribute('viewBox')!
    const outlineBase = outlineSvg.getAttribute('viewBox')!
    expect(profileBase).toBe('0 0 340 620')
    expect(outlineBase).toBe('0 0 340 620')

    // 剖面图放大：viewBox 收缩；外观图不动
    await userEvent.click(within(screen.getByTestId('profile-view')).getByRole('button', { name: '放大' }))
    expect(profileSvg.getAttribute('viewBox')).not.toBe(profileBase)
    expect(outlineSvg.getAttribute('viewBox')).toBe(outlineBase)

    // 复位回 1×（全幅）
    await userEvent.click(within(screen.getByTestId('profile-view')).getByRole('button', { name: '复位' }))
    expect(profileSvg.getAttribute('viewBox')).toBe(profileBase)

    // 外观图缩小：viewBox 扩张；剖面图不动
    await userEvent.click(within(screen.getByTestId('outline-view')).getByRole('button', { name: '缩小' }))
    expect(outlineSvg.getAttribute('viewBox')).not.toBe(outlineBase)
    expect(profileSvg.getAttribute('viewBox')).toBe(profileBase)
  })
})

describe('数据获取：降级与防抖', () => {
  it('请求失败：降级为「剖面数据不可用」提示行，不白屏、不出图', async () => {
    const mock = mockSectionsFetch(null, false)
    vi.stubGlobal('fetch', mock)
    useVehicleStore.setState({ vehicle: vehicleNamed('坏箭') })
    render(<Profile2D />)

    expect(await screen.findByText(/剖面数据不可用/, {}, { timeout: 4000 })).toBeInTheDocument()
    expect(screen.queryByTestId('profile-svg')).not.toBeInTheDocument()
    expect(screen.queryByTestId('outline-svg')).not.toBeInTheDocument()
  })

  it('尚未载入参数时给出提示，不发请求', () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    render(<Profile2D />)

    expect(screen.getByText(/尚未载入参数/)).toBeInTheDocument()
    expect(mock).not.toHaveBeenCalled()
    expect(screen.queryByTestId('profile-svg')).not.toBeInTheDocument()
  })

  it('vehicle 快速变更经 300 ms 防抖只发最后一次请求', async () => {
    const mock = mockSectionsFetch(SINGLE_RESPONSE)
    vi.stubGlobal('fetch', mock)
    useVehicleStore.setState({ vehicle: vehicleNamed('A') })
    render(<Profile2D />)

    await waitFor(() => expect(mock).toHaveBeenCalledTimes(1), { timeout: 4000 })

    // 300 ms 内连续改两次：只有最后一张（C）会发出
    act(() => {
      useVehicleStore.setState({ vehicle: vehicleNamed('B') })
    })
    act(() => {
      useVehicleStore.setState({ vehicle: vehicleNamed('C') })
    })

    await waitFor(
      () => expect(requestBodyNames(mock)).toContain('C'),
      { timeout: 4000 },
    )
    const names = requestBodyNames(mock)
    expect(names).not.toContain('B')
    expect(mock).toHaveBeenCalledTimes(2)
  })
})
