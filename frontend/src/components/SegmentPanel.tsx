import { useEffect, useState } from 'react'

import type { MeridianSegment, SegmentType } from '../api/geometry'
import { canAppendSegment, chainEndRadius, useParamsStore } from '../store/params'
import { useModelStore } from '../store/model'
import { useViewStore } from '../store/view'
import './SegmentPanel.css'

/** 段类型的中文名（§5.3 曲线族——M1 三段型 + M5 六曲线族，共九种全量）。 */
const SEGMENT_TYPE_TEXT: Record<SegmentType, string> = {
  line: '直线（柱 / 锥 / 锥台）',
  arc: '圆弧（球冠 / 球底）',
  ellipse: '椭圆弧（椭球底 / 共底）',
  ogive: '切线卵形（头锥）',
  parabola: '抛物线（头锥）',
  von_karman: '冯·卡门（跨声速头锥）',
  power: '幂律（通用过渡）',
  bell: '钟形喷管（Rao 近似）',
  spline: '样条（自定义）',
}

/** 「追加段」按钮仍只暴露 M1 三段型（先追加再切换类型可得任意族，UI 不膨胀）。 */
const SEGMENT_TYPE_OPTIONS: SegmentType[] = ['line', 'arc', 'ellipse']

/** 段类型下拉的九族全集（M5：六曲线族入口，§5.2 / §5.3）。 */
const ALL_SEGMENT_TYPES: SegmentType[] = [
  'line',
  'arc',
  'ellipse',
  'ogive',
  'parabola',
  'von_karman',
  'power',
  'bell',
  'spline',
]

/** 解析数值输入框；空串或非法输入时不改动剖面（避免把 NaN 写进契约）。 */
function toNumber(value: string): number | null {
  if (value.trim() === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

interface FamilyNumberFieldProps {
  label: string
  step: number
  min?: number
  max?: number
  value: number
  onCommit: (value: number) => void
}

/**
 * 族参数数值控件：**草稿字符串 + 提交时解析**（VehiclePanel.NumberField 同一教训）。
 *
 * 受控直接写回会把 `0.5` 的中间态 `0.` 在 `Number` 下退化成 0，逐字符重置会把小数点
 * 吃掉——系数 K / 长度比这类小数首当其冲。草稿态不写回剖面，合法数值才提交。
 */
function FamilyNumberField({ label, step, min, max, value, onCommit }: FamilyNumberFieldProps) {
  const [draft, setDraft] = useState(String(value))

  useEffect(() => {
    setDraft(String(value))
  }, [value])

  return (
    <label className="segment-panel__field">
      <span className="label">{label}</span>
      <input
        type="number"
        className="num"
        step={step}
        min={min}
        max={max}
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value)
          const parsed = toNumber(event.target.value)
          if (parsed !== null) onCommit(parsed)
        }}
      />
    </label>
  )
}

function metersText(value: number): string {
  return `${value.toFixed(2)} m`
}

/** 左栏：组件树（base_radius + 段链，含分级显隐）+ 选中段的参数表单（规格 §16.3 三栏骨架）。 */
export function SegmentPanel() {
  const profile = useParamsStore((state) => state.profile)
  const setBaseRadius = useParamsStore((state) => state.setBaseRadius)
  const updateSegment = useParamsStore((state) => state.updateSegment)
  const addSegment = useParamsStore((state) => state.addSegment)
  const removeSegment = useParamsStore((state) => state.removeSegment)

  // 选中项放在 `store/view`：组件树与视口**双向联动**（点视口里的段也会写这里，§11.4）
  const selected = useViewStore((state) => state.selectedSegment)
  const hiddenSegments = useViewStore((state) => state.hiddenSegments)
  const selectSegment = useViewStore((state) => state.selectSegment)
  const toggleSegmentHidden = useViewStore((state) => state.toggleSegmentHidden)
  const showAllSegments = useViewStore((state) => state.showAllSegments)

  // 母线校验错误走**既有约束错误体系**（store/model 的 validate 422 → MeridianEditor
  // 错误块完整呈现）；此处只复述一行，让出错时参数表单旁边也可见（可发现性）。
  const modelError = useModelStore((state) => state.error)

  const selectedSegment: MeridianSegment | null =
    selected !== null && selected >= 0 && selected < profile.segments.length
      ? profile.segments[selected]
      : null
  const appendable = canAppendSegment(profile)

  return (
    <div className="segment-panel">
      <section className="panel surface">
        <div className="segment-panel__head">
          <h2 className="panel__title label">组件树</h2>
          <button
            type="button"
            className="segment-panel__button"
            disabled={hiddenSegments.size === 0}
            onClick={showAllSegments}
          >
            全部显示
          </button>
        </div>
        <ul className="segment-panel__tree">
          <li>
            <div className="segment-panel__row segment-panel__row--root">
              <span className="segment-panel__name">底部半径</span>
              <span className="segment-panel__summary num">{metersText(profile.base_radius)}</span>
            </div>
          </li>
          {profile.segments.map((segment, index) => {
            const hidden = hiddenSegments.has(index)
            return (
              <li key={`${index}-${segment.type}`}>
                <div
                  className={`segment-panel__row segment-panel__row--button${
                    selected === index ? ' segment-panel__row--selected' : ''
                  }${hidden ? ' segment-panel__row--hidden' : ''}`}
                >
                  <input
                    type="checkbox"
                    className="segment-panel__visible"
                    checked={!hidden}
                    aria-label={`显示第 ${index} 段`}
                    onChange={() => toggleSegmentHidden(index)}
                  />
                  <button
                    type="button"
                    className="segment-panel__select"
                    aria-pressed={selected === index}
                    onClick={() => selectSegment(index)}
                  >
                    <span className="segment-panel__name">{`#${index} ${segment.type}`}</span>
                    <span className="segment-panel__summary num">
                      {`L ${segment.length.toFixed(2)} · R ${segment.end_radius.toFixed(2)}`}
                    </span>
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
        <p className="segment-panel__hint label">
          勾选框 = 该段在 3D 视口中的显隐。显隐是<strong>纯视图状态</strong>：不重算几何、不触发
          后端请求；隐藏某段时露出的是该处的真实分界（§11.4）。
        </p>
      </section>

      <section className="panel surface">
        <h2 className="panel__title label">参数</h2>

        {modelError !== null && modelError.origin === 'validate' ? (
          <p className="segment-panel__hint segment-panel__hint--warn" role="alert">
            {`母线校验未通过（${modelError.code}）：${modelError.message}`}
          </p>
        ) : null}

        <label className="segment-panel__field">
          <span className="label">底部半径 base_radius（m）</span>
          <input
            type="number"
            className="num"
            step={0.1}
            min={0}
            value={profile.base_radius}
            onChange={(event) => {
              const value = toNumber(event.target.value)
              if (value !== null) setBaseRadius(value)
            }}
          />
        </label>

        {selected !== null && selectedSegment !== null ? (
          <div className="segment-panel__fields">
            <p className="segment-panel__hint label">{`正在编辑段 #${selected}`}</p>

            <label className="segment-panel__field">
              <span className="label">段类型</span>
              <select
                value={selectedSegment.type}
                onChange={(event) => {
                  const next = event.target.value as SegmentType
                  updateSegment(selected, { type: next })
                }}
              >
                {ALL_SEGMENT_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {SEGMENT_TYPE_TEXT[type]}
                  </option>
                ))}
              </select>
            </label>

            <label className="segment-panel__field">
              <span className="label">轴向跨度 length（m）</span>
              <input
                type="number"
                className="num"
                step={0.1}
                min={0}
                value={selectedSegment.length}
                onChange={(event) => {
                  const value = toNumber(event.target.value)
                  if (value !== null) updateSegment(selected, { length: value })
                }}
              />
            </label>

            <label className="segment-panel__field">
              <span className="label">末端半径 end_radius（m）</span>
              <input
                type="number"
                className="num"
                step={0.1}
                min={0}
                value={selectedSegment.end_radius}
                onChange={(event) => {
                  const value = toNumber(event.target.value)
                  if (value !== null) updateSegment(selected, { end_radius: value })
                }}
              />
            </label>

            {/* ---- 族特定参数（M5 六曲线族；字段名与后端 Schema 逐字一致，§5.3）---- */}
            {selectedSegment.type === 'parabola' ? (
              <FamilyNumberField
                label="抛物线系数 K（1 = 全抛物线，趋 0 = 锥形）"
                step={0.05}
                min={0}
                max={1}
                value={selectedSegment.coefficient}
                onCommit={(value) => updateSegment(selected, { coefficient: value })}
              />
            ) : null}
            {selectedSegment.type === 'power' ? (
              <FamilyNumberField
                label="幂律指数 n（1 = 直线；<1 钝过渡；>1 竖直起始）"
                step={0.1}
                min={0}
                max={10}
                value={selectedSegment.exponent}
                onCommit={(value) => updateSegment(selected, { exponent: value })}
              />
            ) : null}
            {selectedSegment.type === 'bell' ? (
              <>
                <FamilyNumberField
                  label="喉部半径 throat_radius（m）"
                  step={0.1}
                  min={0}
                  value={selectedSegment.throat_radius}
                  onCommit={(value) => updateSegment(selected, { throat_radius: value })}
                />
                <FamilyNumberField
                  label="钟形长度比 length_ratio（0.8 = 80% 钟）"
                  step={0.05}
                  min={0}
                  value={selectedSegment.length_ratio}
                  onCommit={(value) => updateSegment(selected, { length_ratio: value })}
                />
                <p className="segment-panel__hint label">
                  出口半径 = 上方 end_radius；两端中恰有一端须为喉部（半径 = throat_radius），且
                  length 与 length_ratio 须自洽（后端校验：L = ratio·(Rₑ−Rₜ)/tan15°）。
                </p>
              </>
            ) : null}
            {selectedSegment.type === 'spline' ? (
              <div className="segment-panel__spline">
                <span className="label">
                  样条控制点（r, z）——内部节点，z 严格递增且落在 (0, length) 内
                </span>
                {selectedSegment.control_points.map(([radius, z], pointIndex) => (
                  <div className="segment-panel__spline-row" key={`cp-${pointIndex}`}>
                    <input
                      type="number"
                      className="num"
                      step={0.1}
                      min={0}
                      aria-label={`控制点 ${pointIndex + 1} 半径 r（m）`}
                      value={radius}
                      onChange={(event) => {
                        const value = toNumber(event.target.value)
                        if (value === null) return
                        const next = selectedSegment.control_points.map(([r, z], i) =>
                          i === pointIndex ? ([value, z] as [number, number]) : ([r, z] as [number, number]),
                        )
                        updateSegment(selected, { control_points: next })
                      }}
                    />
                    <input
                      type="number"
                      className="num"
                      step={0.1}
                      aria-label={`控制点 ${pointIndex + 1} 轴向 z（m）`}
                      value={z}
                      onChange={(event) => {
                        const value = toNumber(event.target.value)
                        if (value === null) return
                        const next = selectedSegment.control_points.map(([r, z], i) =>
                          i === pointIndex ? ([r, value] as [number, number]) : ([r, z] as [number, number]),
                        )
                        updateSegment(selected, { control_points: next })
                      }}
                    />
                    <button
                      type="button"
                      className="segment-panel__button"
                      aria-label={`删除控制点 ${pointIndex + 1}`}
                      disabled={selectedSegment.control_points.length <= 1}
                      onClick={() => {
                        updateSegment(selected, {
                          control_points: selectedSegment.control_points.filter(
                            (_point, i) => i !== pointIndex,
                          ),
                        })
                      }}
                    >
                      删除
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="segment-panel__button"
                  onClick={() => {
                    // 新点取 r = end_radius、z = 上一控制点与段末的中点——由既有字段派生，
                    // 不另造数；越界由后端校验判出（z 须严格落在 (0, length) 内）。
                    const points = selectedSegment.control_points
                    const previousZ = points.length > 0 ? points[points.length - 1][1] : 0
                    const next = [
                      ...points.map(([r, z]) => [r, z] as [number, number]),
                      [selectedSegment.end_radius, (previousZ + selectedSegment.length) / 2] as [number, number],
                    ]
                    updateSegment(selected, { control_points: next })
                  }}
                >
                  添加控制点
                </button>
              </div>
            ) : null}
            {selectedSegment.type === 'ogive' ? (
              <p className="segment-panel__hint label">
                切线卵形由 length 与 end_radius 完全确定：长径比 L/(2R) 为
                <strong>派生量</strong>，无独立参数；后端校验值域 L ≥ R（过钝会破坏 z 单调）。
              </p>
            ) : null}
            {selectedSegment.type === 'von_karman' ? (
              <p className="segment-panel__hint label">
                冯·卡门（Haack C=0）无族参数：外形由 length 与 end_radius 唯一确定；顶端钝头、
                基底与柱段 G1 连续。
              </p>
            ) : null}

            <button
              type="button"
              className="segment-panel__button"
              disabled={profile.segments.length <= 1}
              onClick={() => {
                removeSegment(selected)
                selectSegment(Math.max(0, selected - 1))
              }}
            >
              删除本段
            </button>
          </div>
        ) : (
          <p className="segment-panel__hint label">未选中段（段链为空）</p>
        )}
      </section>

      <section className="panel surface">
        <h2 className="panel__title label">追加段</h2>
        <div className="segment-panel__buttons">
          {SEGMENT_TYPE_OPTIONS.map((type) => (
            <button
              key={type}
              type="button"
              className="segment-panel__button"
              disabled={!appendable}
              onClick={() => {
                addSegment(type)
                selectSegment(profile.segments.length)
              }}
            >
              {`追加 ${SEGMENT_TYPE_TEXT[type]}`}
            </button>
          ))}
        </div>
        {appendable ? (
          <p className="segment-panel__hint label">
            {`新增段自当前末端半径 ${metersText(chainEndRadius(profile))} 起算；` +
              '柱段默认保持半径不变，穹顶段默认收拢到轴线（r=0）。六曲线族（卵形 / 抛物线 / ' +
              '冯·卡门 / 幂律 / 钟形 / 样条）先追加任意段，再在「段类型」下拉切换。'}
          </p>
        ) : (
          <p className="segment-panel__hint segment-panel__hint--warn">
            段链末端半径已为 0（剖面已闭合到轴线），无法再追加段；请先修改末段的 end_radius。
          </p>
        )}
      </section>
    </div>
  )
}
