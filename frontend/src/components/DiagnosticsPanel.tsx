import { useState } from 'react'

import type { Diagnostic, RuleOutcome } from '../api/params'
import { useVehicleStore } from '../store/vehicle'
import './DiagnosticsPanel.css'

/**
 * 左栏「诊断清单」（规格 §6.5 / §11.5 ①）。
 *
 * 三件事必须**分别可见**（§6.5 的三条账目）：
 *
 * 1. **命中项**——按 `level` / `code` 分组，`hard` 与 `warning` 分色；判定码前缀即 §6.3 的类别。
 * 2. **未判定的规则**（`deferred_reason`）单列一栏，写明"缺什么"。
 * 3. **分支未启用**（`uncovered`）再一栏。
 *
 * 只渲染命中项会让"未判"与"通过"看起来一模一样——本项目最贵的一课就是"没报错 ≠ 正确"。
 *
 * ⚠ 两条通路（§6.3 的 422 与 §6.5 的 200）的裁定**同形**，故这里合并渲染；
 * 定位失败（`field_path` 为空或控件未挂载）时**降级为路径文本**而**不得丢弃该条**。
 */

/** 定位高亮时长（ms）：够看清，又不至于常驻干扰。 */
export const LOCATE_HIGHLIGHT_MS = 1600

/**
 * 按 `field_path` 找控件。
 *
 * 刻意用「遍历取属性比对」而不是 `querySelector('[data-field-path="..."]')`：
 * 路径里含 `[` `]` `.`，拼进 CSS 选择器要正确转义，而少转义一次就会**永远定位不到**
 * （表现为"功能静默失效"，不是报错）。逐个比对没有转义问题。
 */
export function locateField(fieldPath: string, root: ParentNode = document): HTMLElement | null {
  if (fieldPath === '') return null
  for (const node of root.querySelectorAll<HTMLElement>('[data-field-path]')) {
    if (node.getAttribute('data-field-path') === fieldPath) return node
  }
  return null
}

/** 滚动到控件并高亮。返回是否命中（未命中时由调用方降级显示路径文本）。 */
export function highlightField(fieldPath: string): boolean {
  const target = locateField(fieldPath)
  if (target === null) return false
  target.scrollIntoView({ block: 'center' })
  target.classList.add('field-located')
  window.setTimeout(() => target.classList.remove('field-located'), LOCATE_HIGHLIGHT_MS)
  return true
}

/** 判定码前缀 → §6.3 的类别名（未列出的码不编类别，只显示判定码本身）。 */
const CATEGORY_TEXT: Record<string, string> = {
  HARD_: '硬约束',
  ENGINEER_: '工程约束',
  COMPAT_: '相容约束',
  SAFETY_: '安全边界',
}

function categoryOf(code: string): string | null {
  for (const [prefix, text] of Object.entries(CATEGORY_TEXT)) {
    if (code.startsWith(prefix)) return text
  }
  return null
}

function groupByCode(items: readonly Diagnostic[]): [string, Diagnostic[]][] {
  const groups = new Map<string, Diagnostic[]>()
  for (const item of items) {
    const bucket = groups.get(item.code)
    if (bucket === undefined) groups.set(item.code, [item])
    else bucket.push(item)
  }
  return [...groups.entries()]
}

interface FindingProps {
  item: Diagnostic
}

/** 一条裁定：判定码 + 类别 + 说明 + 建议 + 「路径 → 控件」定位。 */
function Finding({ item }: FindingProps) {
  const [missed, setMissed] = useState(false)
  const category = categoryOf(item.code)

  return (
    <li className={`diagnostics__item diagnostics__item--${item.level}`}>
      <div className="diagnostics__item-head">
        <span className="diagnostics__code num">{item.code}</span>
        {category === null ? null : <span className="label">{category}</span>}
      </div>
      <p className="diagnostics__message">{item.message}</p>
      <p className="diagnostics__suggestion">{item.suggestion}</p>

      {item.field_path === '' ? (
        <p className="diagnostics__path label">无字段路径（整箭层判定）</p>
      ) : (
        <div className="diagnostics__path-row">
          {/* 定位失败后**撤掉按钮**：规格要求降级时路径以纯文本呈现、不可点击
              （§11.5 ① 第 4 条）——留一个按不出结果的按钮只会反复误导用户。 */}
          {missed ? null : (
            <button
              type="button"
              className="diagnostics__locate"
              onClick={() => {
                setMissed(!highlightField(item.field_path))
              }}
            >
              定位到控件
            </button>
          )}
          {/* 路径始终以纯文本呈现：定位失败时它就是唯一的线索（§11.5 ① 第 4 条） */}
          <code className="diagnostics__path num">{item.field_path}</code>
        </div>
      )}
      {missed ? (
        <p className="diagnostics__path-fallback label">
          当前视图里没有该控件（所在级可能未渲染或面板未挂载），已降级显示路径文本
        </p>
      ) : null}
    </li>
  )
}

function RuleLine({ rule }: { rule: RuleOutcome }) {
  return (
    <li className="diagnostics__rule">
      <div className="diagnostics__item-head">
        <span className="diagnostics__code num">{rule.code}</span>
        <span className="diagnostics__rule-title">{rule.title}</span>
      </div>
      {/* 「缺什么」必须写出来：否则「未判」与「未通过」在界面上无从区分（§11.5 ① 第 2 条） */}
      {rule.deferred_reason == null || rule.deferred_reason === '' ? null : (
        <p className="diagnostics__deferred">{rule.deferred_reason}</p>
      )}
      <p className="diagnostics__threshold">{`阈值：${rule.threshold}`}</p>
      {/* 来源字样**照抄**后端 source，前端不得自拟措辞（§6.5 / OI-31） */}
      <p className="diagnostics__source label">{rule.source}</p>
    </li>
  )
}

export function DiagnosticsPanel() {
  const report = useVehicleStore((state) => state.report)
  const errorDiagnostics = useVehicleStore((state) => state.errorDiagnostics)
  const diagnoseError = useVehicleStore((state) => state.diagnoseError)
  const diagnosing = useVehicleStore((state) => state.diagnosing)
  const vehicle = useVehicleStore((state) => state.vehicle)

  if (vehicle === null) {
    return (
      <section className="panel surface">
        <h2 className="panel__title label">方案诊断</h2>
        <p className="diagnostics__hint label">尚未载入参数，无法诊断</p>
      </section>
    )
  }

  const findings = [...errorDiagnostics, ...(report?.constraints ?? []), ...(report?.diagnostics ?? [])]
  const rules = report?.rules ?? []
  /**
   * 是否拿到了逐规则账目。
   *
   * ⚠ 诊断被拒绝（硬约束 422）或尚未跑完时 `report` 为空，此时三条账目**根本不存在**。
   * 若照常渲染「无 / 0」，读起来会像「没有未判定的规则」——那正是 §11.5 ① 第 2 条
   * 要拦的形态（把"未判"与"通过"混为一谈）。故此处必须显式说明"本轮未产出账目"。
   */
  const ledgerReady = report !== null
  const deferred = rules.filter((rule) => rule.deferred_reason != null && rule.deferred_reason !== '')
  const uncovered = rules.filter((rule) => rule.uncovered.length > 0)
  const passed = rules.filter(
    (rule) =>
      (rule.deferred_reason == null || rule.deferred_reason === '') &&
      rule.diagnostics.length === 0 &&
      rule.uncovered.length === 0,
  )
  const hard = findings.filter((item) => item.level === 'hard')
  const warnings = findings.filter((item) => item.level === 'warning')

  const noLedger = (
    <p className="diagnostics__hint label">
      本轮未产出逐规则账目（诊断被拒绝或尚未完成），这不等于「没有这一栏的情况」。
    </p>
  )

  return (
    <section className="panel surface">
      <h2 className="panel__title label">方案诊断</h2>

      {diagnosing ? <p className="diagnostics__hint label">诊断计算中…</p> : null}

      {diagnoseError !== null ? (
        <div className="diagnostics__error">
          <p>{`诊断被拒绝（${diagnoseError.code}）：${diagnoseError.message}`}</p>
          <p className="diagnostics__suggestion">{diagnoseError.suggestion}</p>
        </div>
      ) : null}

      <div className="diagnostics__section">
        <h3 className="label">{`命中项（硬 ${hard.length} · 警告 ${warnings.length}）`}</h3>
        {findings.length === 0 ? (
          <p className="diagnostics__hint label">无命中项</p>
        ) : (
          [...groupByCode(hard), ...groupByCode(warnings)].map(([code, items]) => (
            <ul className="diagnostics__list" key={code}>
              {items.map((item, index) => (
                <Finding key={`${code}-${index}`} item={item} />
              ))}
            </ul>
          ))
        )}
      </div>

      <div className="diagnostics__section">
        <h3 className="label">
          {ledgerReady ? `未判定的规则（${deferred.length}）` : '未判定的规则'}
        </h3>
        {!ledgerReady ? (
          noLedger
        ) : deferred.length === 0 ? (
          <p className="diagnostics__hint label">无</p>
        ) : (
          <ul className="diagnostics__list">
            {deferred.map((rule) => (
              <RuleLine key={rule.code} rule={rule} />
            ))}
          </ul>
        )}
        <p className="diagnostics__hint label">
          这些规则本次没有判定，与「判了且通过」不是一回事。
        </p>
      </div>

      <div className="diagnostics__section">
        <h3 className="label">
          {ledgerReady ? `分支未启用（${uncovered.length}）` : '分支未启用'}
        </h3>
        {!ledgerReady ? (
          noLedger
        ) : uncovered.length === 0 ? (
          <p className="diagnostics__hint label">无</p>
        ) : (
          <ul className="diagnostics__list">
            {uncovered.map((rule) => (
              <li className="diagnostics__rule" key={rule.code}>
                <div className="diagnostics__item-head">
                  <span className="diagnostics__code num">{rule.code}</span>
                  <span className="diagnostics__rule-title">{rule.title}</span>
                </div>
                {rule.uncovered.map((note, index) => (
                  <p className="diagnostics__threshold" key={`${rule.code}-${index}`}>
                    {note}
                  </p>
                ))}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="diagnostics__section">
        <h3 className="label">
          {ledgerReady ? `已判定且通过（${passed.length}）` : '已判定且通过'}
        </h3>
        {!ledgerReady ? (
          noLedger
        ) : passed.length === 0 ? (
          <p className="diagnostics__hint label">无</p>
        ) : (
          <p className="diagnostics__hint num">{passed.map((rule) => rule.code).join(' · ')}</p>
        )}
      </div>
    </section>
  )
}
