import { useCallback, useEffect, useState } from 'react'

import { ApiError } from '../api/client'
import { fetchThresholds, putThresholds, type ThresholdEntry } from '../api/params'
import './ThresholdPanel.css'

/**
 * 阈值设置项（规格 §1.7.3 OI-31 / §6.5 / §18.4）。
 *
 * 两条硬要求：
 *
 * 1. **显示生效值而非"文件里的值"**：`PUT` 之后环境变量仍然覆盖 `config.toml`（§18.4），
 *    故界面必须显示后端算好的**生效值**与它的**来源**——否则"保存了却不生效"会表现为
 *    一个无法解释的现象。
 * 2. **「未配置」是必须可见的状态**：判据在表内而无数值的项（现为「最小工艺厚度」）
 *    显示为「未配置」并说明"该条判据不判定"，**不得**留空、也不得替它编一个默认值。
 *
 * 来源文字一律**照抄**后端 `source` 字段，前端不得自拟措辞（§6.5）。
 */

/** 解析输入框；空串 = 清空该键（回到「未配置」）。 */
function toValue(text: string): number | null {
  if (text.trim() === '') return null
  const parsed = Number(text)
  return Number.isFinite(parsed) ? parsed : null
}

interface EntryRowProps {
  entry: ThresholdEntry
  disabled: boolean
  onWrite: (key: string, value: number | null) => void
}

function EntryRow({ entry, disabled, onWrite }: EntryRowProps) {
  const [draft, setDraft] = useState(entry.value === null ? '' : String(entry.value))

  // 生效值可能因为**环境变量**而与本行编辑无关地变化（例如后端重启后读到新配置），
  // 故每次下发新值都同步草稿——界面显示的是生效值，不是用户上次敲进去的字符串。
  useEffect(() => {
    setDraft(entry.value === null ? '' : String(entry.value))
  }, [entry.value])

  const configured = entry.value !== null

  return (
    <li className="thresholds__row">
      <div className="thresholds__head">
        <span className="thresholds__label">{entry.label}</span>
        <span
          className={`thresholds__origin${configured ? '' : ' thresholds__origin--unset'}`}
        >{`来源：${entry.origin}`}</span>
      </div>

      <div className="thresholds__edit">
        <input
          type="number"
          className="num"
          step="any"
          aria-label={`${entry.label}（${entry.key}）`}
          data-threshold-key={entry.key}
          value={draft}
          disabled={disabled}
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => {
            const next = toValue(draft)
            if (next !== entry.value) onWrite(entry.key, next)
          }}
        />
        <span className="thresholds__value num">
          {configured ? `生效值 ${entry.value}` : '未配置（该条判据不判定）'}
        </span>
      </div>

      <p className="thresholds__source label">{`来源标注：${entry.source}`}</p>
      <p className="thresholds__note">{entry.note}</p>
      <p className="thresholds__key label">{`config.toml 键：${entry.key}`}</p>
    </li>
  )
}

export function ThresholdPanel() {
  const [entries, setEntries] = useState<ThresholdEntry[]>([])
  const [configFile, setConfigFile] = useState('')
  const [error, setError] = useState<{ code: string; message: string; suggestion: string } | null>(
    null,
  )
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    fetchThresholds()
      .then((response) => {
        setEntries(response.thresholds)
        setConfigFile(response.config_file)
        setError(null)
      })
      .catch((reason: unknown) => {
        setError(
          reason instanceof ApiError
            ? { code: reason.code, message: reason.message, suggestion: reason.suggestion }
            : {
                code: 'UNEXPECTED',
                message: reason instanceof Error ? reason.message : '未知错误',
                suggestion: '确认后端已启动（GET /api/health）后重试',
              },
        )
      })
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const write = useCallback((key: string, value: number | null) => {
    setBusy(true)
    putThresholds({ [key]: value })
      .then((response) => {
        setEntries(response.thresholds)
        setConfigFile(response.config_file)
        setError(null)
      })
      .catch((reason: unknown) => {
        // 被拒绝时**文件保持原样**，故这里重新拉一次生效值，避免界面停留在用户敲的值上。
        setError(
          reason instanceof ApiError
            ? { code: reason.code, message: reason.message, suggestion: reason.suggestion }
            : {
                code: 'UNEXPECTED',
                message: reason instanceof Error ? reason.message : '未知错误',
                suggestion: '重试；若持续失败请查看后端日志',
              },
        )
        load()
      })
      .finally(() => {
        setBusy(false)
      })
  }, [load])

  return (
    <section className="panel surface">
      <h2 className="panel__title label">阈值设置（§6.5）</h2>
      <p className="thresholds__hint">
        显示的是生效值与它的来源（优先级：环境变量 &gt; config.toml &gt; 默认值，§18.4）。
      </p>

      {error !== null ? (
        <div className="thresholds__error">
          <p>{`配置不可用（${error.code}）：${error.message}`}</p>
          <p className="thresholds__note">{error.suggestion}</p>
        </div>
      ) : null}

      <ul className="thresholds__list">
        {entries.map((entry) => (
          <EntryRow key={entry.key} entry={entry} disabled={busy} onWrite={write} />
        ))}
      </ul>

      {configFile === '' ? null : (
        <p className="thresholds__hint label">{`写入目标：${configFile}`}</p>
      )}
      <p className="thresholds__hint">
        清空输入框并离开即写回「未配置」（删除该键），不会用一个默认值顶替。
      </p>
    </section>
  )
}
