import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  fetchAssistantStatus,
  openAssistantStream,
  saveAssistantConfig,
  type AssistantEvent,
  type AssistantParamChange,
  type AssistantStatus,
  type AssistantStream,
} from '../api/assistant'
import { ApiError } from '../api/client'
import type { Diagnostic, RuleOutcome, Vehicle } from '../api/params'
import { useVehicleStore } from '../store/vehicle'
import './ChatPanel.css'

/**
 * AI 助手 ChatPanel（规格 §10.2 / OI-10 / §11 行 1894）。
 *
 * 形态与降级：
 *
 * - 右下角悬浮按钮展开成对话面板。**未配置**（`status.configured === false`）时
 *   入口自动隐藏（OI-10 降级①——机检，不是「点了再弹降级」）；本地「方案诊断」
 *   按钮永远可达（零 LLM，复用既有诊断通路）。
 * - 三降级全部收敛为**事件**：未配置 / 断网 / 上游错误各自下发明确的
 *   `degraded`，前端据此呈现提示行；AI 通道异常**不**拖垮其余功能（§10.2）。
 *
 * 确认制（OI-10 / §10.2）：
 *
 * - `param_diff` 结构化块呈现「前后值 + 确认 / 拒绝」按钮；**不点确认就不应用**，
 *   后端只持有 pending，应用（`apply_diff`）才落账——前端机检锚点同口径。
 * - 确认后调用既有 `useVehicleStore.setVehicle` 整包写入候选 Vehicle，再触发既有
 *   建模刷新通路（防抖诊断）。AI **永不**静默改参数。
 */

/** 对话消息（增量拼装 + 最终整段；UI 渲染用这一条）。 */
interface ChatMessage {
  id: number
  role: 'user' | 'assistant'
  text: string
  /** 与本条 assistant 消息关联的待确认 diff（若有）。 */
  pendingDiff?: PendingDiff | null
  /** 与本条 assistant 消息关联的诊断块（若有）。 */
  diagnosis?: DiagnosisPayload | null
  /** 降级 / 错误提示行（若有）。 */
  notice?: NoticePayload | null
}

interface PendingDiff {
  diffId: string
  changes: AssistantParamChange[]
  vehicle: Vehicle
  /** 用户是否已处理（应用 / 拒绝）；处理即销账，按钮撤掉。 */
  resolved: 'applied' | 'rejected' | null
}

interface DiagnosisPayload {
  constraints: Diagnostic[]
  diagnostics: Diagnostic[]
  rules: RuleOutcome[]
  note: string
}

interface NoticePayload {
  kind: 'degraded' | 'error'
  code: string
  message: string
  suggestion: string
}

const NOT_CONFIGURED: NoticePayload = {
  kind: 'degraded',
  code: 'ASSISTANT_NOT_CONFIGURED',
  message: 'AI 助手未配置',
  suggestion: '在「AI 设置」中填写 base_url / api_key / model；未配置不影响任何手工建模功能',
}

export function ChatPanel() {
  const vehicle = useVehicleStore((state) => state.vehicle)
  const applyCandidate = useVehicleStore((state) => state.applyCandidate)
  const requestDiagnose = useVehicleStore((state) => state.requestDiagnose)

  const [status, setStatus] = useState<AssistantStatus | null>(null)
  const [statusError, setStatusError] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [draft, setDraft] = useState('')
  const [stream, setStream] = useState<AssistantStream | null>(null)
  const [configOpen, setConfigOpen] = useState(false)
  const [configForm, setConfigForm] = useState({ base_url: '', model: '', api_key: '' })
  const [configError, setConfigError] = useState<string | null>(null)
  const [configBusy, setConfigBusy] = useState(false)

  const nextId = useRef(1)
  const messagesEndRef = useRef<HTMLDivElement | null>(null)
  // diff_id → 变更字段路径（param_diff 时登记，diff_applied 时据此写 sourcedFields）
  const diffPathsRef = useRef(new Map<string, string[]>())

  const loadStatus = useCallback(() => {
    fetchAssistantStatus()
      .then((value) => {
        setStatus(value)
        setStatusError(null)
        setConfigForm((prev) => ({
          ...prev,
          base_url: value.base_url ?? '',
          model: value.model ?? '',
          api_key: '',
        }))
      })
      .catch((error: unknown) => {
        setStatusError(error instanceof ApiError ? error.suggestion : '无法读取 AI 状态')
      })
  }, [])

  useEffect(() => {
    loadStatus()
  }, [loadStatus])

  // 本地「方案诊断」（零 LLM）：复用既有 store 的诊断通路（不触发任何 AI 请求）。
  const runLocalDiagnosis = useCallback(() => {
    if (vehicle === null) return
    requestDiagnose()
    setMessages((prev) => [
      ...prev,
      {
        id: nextId.current++,
        role: 'assistant',
        text: '已触发本地方案诊断（零 LLM）—— 结果见左栏诊断清单',
      },
    ])
  }, [vehicle, requestDiagnose])

  // 打开面板时建立 WS；关闭面板时断开（省资源，不常驻连接）。
  useEffect(() => {
    if (!open || status?.configured !== true) {
      if (stream !== null) {
        stream.close()
        setStream(null)
      }
      return
    }

    const connection = openAssistantStream({
      onEvent: (event) => handleEvent(event),
      onError: () => {
        setMessages((prev) => [
          ...prev,
          {
            id: nextId.current++,
            role: 'assistant',
            text: '',
            notice: {
              kind: 'error',
              code: 'ASSISTANT_STREAM_ERROR',
              message: 'AI 对话通道异常',
              suggestion: '已断开；重新收起再展开面板可重试，其余功能不受影响',
            },
          },
        ])
      },
      onClose: () => {
        setStream(null)
      },
    })
    setStream(connection)
    return () => {
      connection.close()
    }
    // 只在 open / configured 切换时重建；handleEvent 与 setMessages 稳定（useState）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, status?.configured])

  const handleEvent = useCallback((event: AssistantEvent) => {
    switch (event.type) {
      case 'ready':
        // 后端确认就绪：无 UI 动作（面板已展开）
        return
      case 'assistant_delta': {
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (last !== undefined && last.role === 'assistant' && last.pendingDiff == null && last.diagnosis == null && last.notice == null) {
            const updated = { ...last, text: last.text + event.text }
            return [...prev.slice(0, -1), updated]
          }
          return [...prev, { id: nextId.current++, role: 'assistant' as const, text: event.text }]
        })
        return
      }
      case 'assistant_text': {
        setMessages((prev) => {
          const last = prev[prev.length - 1]
          if (last !== undefined && last.role === 'assistant' && last.pendingDiff == null && last.diagnosis == null && last.notice == null) {
            return [...prev.slice(0, -1), { ...last, text: event.text }]
          }
          return [...prev, { id: nextId.current++, role: 'assistant' as const, text: event.text }]
        })
        return
      }
      case 'param_diff': {
        const messageId = nextId.current++
        const pending: PendingDiff = {
          diffId: event.diff_id,
          changes: event.changes,
          vehicle: event.vehicle,
          resolved: null,
        }
        diffPathsRef.current.set(
          event.diff_id,
          event.changes.map((change) => change.path),
        )
        setMessages((prev) => [...prev, { id: messageId, role: 'assistant', text: '', pendingDiff: pending }])
        return
      }
      case 'diff_applied':
      case 'diff_rejected': {
        setMessages((prev) =>
          prev.map((msg) => {
            if (msg.pendingDiff?.diffId === event.diff_id) {
              return {
                ...msg,
                pendingDiff: {
                  ...msg.pendingDiff,
                  resolved: event.type === 'diff_applied' ? 'applied' : 'rejected',
                },
              }
            }
            return msg
          }),
        )
        if (event.type === 'diff_applied') {
          // 确认制唯一生效口：候选 Vehicle 写入既有 store（改动路径标用户来源），
          // 再触发建模刷新。AI 永不静默改参数——没有 apply_diff 就没有本分支。
          applyCandidate(event.vehicle, diffPathsRef.current.get(event.diff_id) ?? [])
          requestDiagnose()
        }
        return
      }
      case 'diagnosis': {
        const messageId = nextId.current++
        const payload: DiagnosisPayload = {
          constraints: event.constraints,
          diagnostics: event.diagnostics,
          rules: event.rules,
          note: event.note,
        }
        setMessages((prev) => [...prev, { id: messageId, role: 'assistant', text: '', diagnosis: payload }])
        return
      }
      case 'instruction_rejected':
      case 'instruction_invalid':
      case 'degraded':
      case 'error': {
        const messageId = nextId.current++
        const notice: NoticePayload = {
          kind: event.type === 'degraded' ? 'degraded' : 'error',
          code: event.type === 'degraded' || event.type === 'error' ? event.code : event.type,
          message: event.message,
          suggestion: event.suggestion,
        }
        // 指令被拒可带 diagnostics（AI 提议的硬违打回）；降级 / 错误不带。
        const diagnostics =
          event.type === 'instruction_rejected' ? event.diagnostics : []
        setMessages((prev) => [
          ...prev,
          { id: messageId, role: 'assistant', text: '', notice, diagnosis: diagnostics.length > 0 ? { constraints: [], diagnostics, rules: [], note: '' } : null },
        ])
        return
      }
    }
  }, [applyCandidate, requestDiagnose])

  const sendMessage = useCallback(() => {
    const text = draft.trim()
    if (text === '' || stream === null || vehicle === null) return
    setMessages((prev) => [...prev, { id: nextId.current++, role: 'user', text }])
    stream.send({ type: 'user_message', text, vehicle })
    setDraft('')
  }, [draft, stream, vehicle])

  const confirmDiff = useCallback(
    (diffId: string) => {
      stream?.send({ type: 'apply_diff', diff_id: diffId })
    },
    [stream],
  )

  const rejectDiff = useCallback(
    (diffId: string) => {
      stream?.send({ type: 'reject_diff', diff_id: diffId })
    },
    [stream],
  )

  const saveConfig = useCallback(() => {
    setConfigBusy(true)
    setConfigError(null)
    saveAssistantConfig({
      base_url: configForm.base_url.trim(),
      model: configForm.model.trim(),
      api_key: configForm.api_key === '' ? null : configForm.api_key,
    })
      .then((value) => {
        setStatus(value)
        setConfigOpen(false)
        setConfigForm((prev) => ({ ...prev, api_key: '' }))
        // 配置完成后若已展开面板，重建 WS 以让后端重新发 ready(configured=true)
        if (open && stream !== null) {
          stream.close()
          setStream(null)
        }
      })
      .catch((error: unknown) => {
        setConfigError(error instanceof ApiError ? error.suggestion : '保存失败')
      })
      .finally(() => setConfigBusy(false))
  }, [configForm, open, stream])

  // 自动滚到最新消息
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const configured = status?.configured === true
  const vehicleMissing = vehicle === null

  // 未配置 → 隐藏 AI 入口（OI-10 降级①机检）；本地诊断按钮永远可达。
  return (
    <>
      <div className={`chat-fab${configured ? '' : ' chat-fab--hidden'}`}>
        <button
          type="button"
          className="chat-fab__button"
          aria-label={open ? '收起 AI 助手' : '展开 AI 助手'}
          onClick={() => setOpen((prev) => !prev)}
        >
          {open ? '×' : 'AI'}
        </button>
      </div>

      {open ? (
        <section className="chat-panel surface" aria-label="AI 助手">
          <header className="chat-panel__header">
            <h2 className="chat-panel__title">AI 助手</h2>
            <div className="chat-panel__actions">
              <button type="button" className="chat-panel__action label" onClick={() => setConfigOpen((prev) => !prev)}>
                AI 设置
              </button>
              <button type="button" className="chat-panel__action label" onClick={runLocalDiagnosis} disabled={vehicleMissing}>
                方案诊断
              </button>
            </div>
          </header>

          {configOpen ? (
            <div className="chat-config">
              <label className="chat-config__field">
                <span className="label">base_url</span>
                <input
                  type="url"
                  className="num"
                  placeholder="https://api.example.com/v1"
                  value={configForm.base_url}
                  onChange={(event) => setConfigForm((prev) => ({ ...prev, base_url: event.target.value }))}
                />
              </label>
              <label className="chat-config__field">
                <span className="label">model</span>
                <input
                  type="text"
                  className="num"
                  placeholder="gpt-4o / deepseek-chat"
                  value={configForm.model}
                  onChange={(event) => setConfigForm((prev) => ({ ...prev, model: event.target.value }))}
                />
              </label>
              <label className="chat-config__field">
                <span className="label">api_key（只写，不回传）</span>
                <input
                  type="password"
                  className="num"
                  placeholder={status?.api_key_masked ?? '留空 = 保留现有值'}
                  value={configForm.api_key}
                  onChange={(event) => setConfigForm((prev) => ({ ...prev, api_key: event.target.value }))}
                />
              </label>
              {configError !== null ? <p className="chat-config__error">{configError}</p> : null}
              <div className="chat-config__actions">
                <button type="button" className="label" onClick={() => setConfigOpen(false)}>
                  取消
                </button>
                <button type="button" disabled={configBusy} onClick={saveConfig}>
                  保存
                </button>
              </div>
              {status?.api_key_masked != null ? (
                <p className="chat-config__hint label">{`当前密钥：${status.api_key_masked}（明文不回传）`}</p>
              ) : null}
            </div>
          ) : null}

          <div className="chat-panel__messages">
            {messages.length === 0 ? (
              <p className="chat-panel__hint label">
                {configured ? '描述你的需求，例如「造一枚两级火箭，载荷 5 吨到 LEO」' : NOT_CONFIGURED.suggestion}
              </p>
            ) : (
              messages.map((message) => (
                <MessageRow
                  key={message.id}
                  message={message}
                  onConfirm={confirmDiff}
                  onReject={rejectDiff}
                />
              ))
            )}
            <div ref={messagesEndRef} />
          </div>

          <div className="chat-panel__input">
            <input
              type="text"
              className="num"
              placeholder={configured ? '输入需求…' : '未配置 AI，先点「AI 设置」'}
              value={draft}
              disabled={!configured || vehicleMissing}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault()
                  sendMessage()
                }
              }}
            />
            <button type="button" disabled={!configured || vehicleMissing || draft.trim() === ''} onClick={sendMessage}>
              发送
            </button>
          </div>
        </section>
      ) : null}

      {/* 未配置但面板展开过 / 状态读取失败时给一个可见入口去配置 */}
      {!configured && (open || statusError !== null) ? (
        <div className="chat-degraded-hint surface">
          <p className="label">{statusError ?? 'AI 助手未配置'}</p>
          <button type="button" className="label" onClick={() => setConfigOpen(true)}>
            前往设置
          </button>
        </div>
      ) : null}
    </>
  )
}

interface MessageRowProps {
  message: ChatMessage
  onConfirm: (diffId: string) => void
  onReject: (diffId: string) => void
}

function MessageRow({ message, onConfirm, onReject }: MessageRowProps) {
  const text = useMemo(() => message.text.trim(), [message.text])
  return (
    <article className={`chat-message chat-message--${message.role}`}>
      {text !== '' ? <p className="chat-message__text">{text}</p> : null}

      {message.pendingDiff != null ? (
        <DiffBlock pending={message.pendingDiff} onConfirm={onConfirm} onReject={onReject} />
      ) : null}

      {message.diagnosis != null && message.diagnosis.rules.length > 0 ? (
        <DiagnosisBlock diagnosis={message.diagnosis} />
      ) : null}

      {message.diagnosis != null && message.diagnosis.diagnostics.length > 0 && message.diagnosis.rules.length === 0 ? (
        <DiagnosisBlock diagnosis={message.diagnosis} />
      ) : null}

      {message.notice != null ? <NoticeBlock notice={message.notice} /> : null}
    </article>
  )
}

function DiffBlock({
  pending,
  onConfirm,
  onReject,
}: {
  pending: PendingDiff
  onConfirm: (diffId: string) => void
  onReject: (diffId: string) => void
}) {
  const resolved = pending.resolved !== null
  return (
    <div className={`chat-diff${resolved ? ' chat-diff--resolved' : ''}`} data-diff-id={pending.diffId}>
      <p className="chat-diff__title label">参数修改建议（确认后应用）</p>
      <ul className="chat-diff__list">
        {pending.changes.map((change) => (
          <li key={change.path} className="chat-diff__row">
            <code className="chat-diff__path num">{change.path}</code>
            <span className="chat-diff__arrow">→</span>
            <span className="chat-diff__old num">{JSON.stringify(change.old)}</span>
            <span className="chat-diff__sep">→</span>
            <span className="chat-diff__new num">{JSON.stringify(change.new)}</span>
          </li>
        ))}
      </ul>
      {resolved ? (
        <p className="chat-diff__resolved label">
          {pending.resolved === 'applied' ? '已应用（触发建模刷新）' : '已拒绝'}
        </p>
      ) : (
        <div className="chat-diff__actions">
          <button type="button" onClick={() => onConfirm(pending.diffId)}>
            确认应用
          </button>
          <button type="button" onClick={() => onReject(pending.diffId)}>
            拒绝
          </button>
        </div>
      )}
    </div>
  )
}

function DiagnosisBlock({ diagnosis }: { diagnosis: DiagnosisPayload }) {
  const findings = [...diagnosis.constraints, ...diagnosis.diagnostics]
  return (
    <div className="chat-diagnosis">
      <p className="chat-diagnosis__title label">诊断</p>
      {findings.length > 0 ? (
        <ul className="chat-diagnosis__list">
          {findings.map((item, index) => (
            <li key={`${item.code}-${index}`} className={`chat-diagnosis__item chat-diagnosis__item--${item.level}`}>
              <span className="chat-diagnosis__code num">{item.code}</span>
              <p className="chat-diagnosis__message">{item.message}</p>
              <p className="chat-diagnosis__suggestion">{item.suggestion}</p>
            </li>
          ))}
        </ul>
      ) : (
        <p className="chat-diagnosis__hint label">无命中项</p>
      )}
      {diagnosis.rules.length > 0 ? (
        <p className="chat-diagnosis__rules label">{`逐规则账目 ${diagnosis.rules.length} 条`}</p>
      ) : null}
      {diagnosis.note !== '' ? <p className="chat-diagnosis__note label">{diagnosis.note}</p> : null}
    </div>
  )
}

function NoticeBlock({ notice }: { notice: NoticePayload }) {
  return (
    <div className={`chat-notice chat-notice--${notice.kind}`}>
      <p className="chat-notice__message">{notice.message}</p>
      <p className="chat-notice__suggestion">{notice.suggestion}</p>
    </div>
  )
}
