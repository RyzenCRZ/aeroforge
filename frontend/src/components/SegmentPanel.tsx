import type { MeridianSegment, SegmentType } from '../api/geometry'
import { canAppendSegment, chainEndRadius, useParamsStore } from '../store/params'
import { useViewStore } from '../store/view'
import './SegmentPanel.css'

/** 段类型的中文名（§16.3 的 M1 曲线族裁剪）。 */
const SEGMENT_TYPE_TEXT: Record<SegmentType, string> = {
  line: '直线（柱 / 锥 / 锥台）',
  arc: '圆弧（球冠 / 球底）',
  ellipse: '椭圆弧（椭球底 / 共底）',
}

const SEGMENT_TYPE_OPTIONS: SegmentType[] = ['line', 'arc', 'ellipse']

/** 解析数值输入框；空串或非法输入时不改动剖面（避免把 NaN 写进契约）。 */
function toNumber(value: string): number | null {
  if (value.trim() === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
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
                {SEGMENT_TYPE_OPTIONS.map((type) => (
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
              '柱段默认保持半径不变，穹顶段默认收拢到轴线（r=0）'}
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
