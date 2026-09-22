import { ApiError, getJson, isRecord, postJson } from './client'
import { isDiagnostic, isRuleOutcome, type Diagnostic, type RuleOutcome, type Vehicle } from './params'
import type { components } from './schema'

/**
 * AI 助手域 API（规格 §10.2 / OI-10 / M7）。
 *
 * 三条通路：
 *
 * - `GET /api/assistant/status` —— 机检依据：`configured === false` 时 AI 入口
 *   必须隐藏（OI-10 降级①），本地「方案诊断」不受影响；
 * - `POST /api/assistant/config` —— 写入 base_url / api_key / model。`api_key`
 *   为**只写**字段：请求 `null` = 保留现有值、空串 = 清除；任何响应只回**脱敏**
 *   形态（`sk-…abcd`），前端没有任何读回明文的通路；
 * - `WS /ws/assistant` —— 对话流（可选通道）。WS 协议不在 OpenAPI 内，事件形状
 *   在本模块**边界校验**（与 `openJobStream` 同一惯例），不符的事件被丢弃而不是
 *   带着坏数据进渲染层。
 *
 * 类型一律取自 OpenAPI 生成物（§18.2）；本模块只做传输与边界校验（ADR-011）。
 */

export type AssistantStatus = components['schemas']['AssistantStatusResponse']

/** `POST /api/assistant/config` 的请求体（`api_key` 语义见模块注释）。 */
export interface AssistantConfigInput {
  base_url: string
  model: string
  /** null = 保留现有密钥；'' = 清除（回到未配置）；非空 = 设置新值（只写）。 */
  api_key: string | null
}

function isAssistantStatus(value: unknown): value is AssistantStatus {
  if (!isRecord(value)) return false
  return (
    typeof value.configured === 'boolean' &&
    (value.base_url === null || typeof value.base_url === 'string') &&
    (value.model === null || typeof value.model === 'string') &&
    (value.api_key_masked === null || typeof value.api_key_masked === 'string') &&
    (value.reachable === null || typeof value.reachable === 'boolean') &&
    typeof value.config_file === 'string'
  )
}

/** 读 AI 通道状态（前端据此做「入口隐藏」机检，OI-10）。 */
export function fetchAssistantStatus(): Promise<AssistantStatus> {
  return getJson('/api/assistant/status', isAssistantStatus)
}

/** 写 AI 通道配置；响应是**写入后**的状态（脱敏形态），据此刷新机检。 */
export function saveAssistantConfig(input: AssistantConfigInput): Promise<AssistantStatus> {
  return postJson('/api/assistant/config', input, isAssistantStatus)
}

// ---------------------------------------------------------------------------
// /ws/assistant 对话流（协议形状见后端 aeroforge/api/assistant.py 的模块注释）
// ---------------------------------------------------------------------------

/** 一个字段的改动（param_diff 结构化块的行；old/new 是 JSON 值）。 */
export interface AssistantParamChange {
  path: string
  old: unknown
  new: unknown
}

/** 服务端 → 客户端事件（与后端逐字对齐；解析不符的一律丢弃）。 */
export type AssistantEvent =
  | { type: 'ready'; configured: boolean }
  | { type: 'assistant_delta'; text: string }
  | { type: 'assistant_text'; text: string }
  | {
      type: 'param_diff'
      diff_id: string
      changes: AssistantParamChange[]
      vehicle: Vehicle
    }
  | { type: 'diff_applied'; diff_id: string; vehicle: Vehicle }
  | { type: 'diff_rejected'; diff_id: string }
  | {
      type: 'diagnosis'
      constraints: Diagnostic[]
      diagnostics: Diagnostic[]
      rules: RuleOutcome[]
      note: string
    }
  | { type: 'instruction_rejected'; message: string; suggestion: string; diagnostics: Diagnostic[] }
  | { type: 'instruction_invalid'; message: string; suggestion: string }
  | { type: 'degraded'; code: string; message: string; suggestion: string }
  | { type: 'error'; code: string; message: string; suggestion: string }

/** 会下发 `code` 的事件（降级三态与错误体；用于「未配置 → 入口隐藏」机检联动）。 */
export function eventCode(event: AssistantEvent): string | null {
  return event.type === 'degraded' || event.type === 'error' ? event.code : null
}

/** 客户端 → 服务端消息（确认制的唯一生效口是 `apply_diff`）。 */
export type AssistantClientMessage =
  | { type: 'user_message'; text: string; vehicle: Vehicle }
  | { type: 'apply_diff'; diff_id: string }
  | { type: 'reject_diff'; diff_id: string }

function isParamChange(value: unknown): value is AssistantParamChange {
  return isRecord(value) && typeof value.path === 'string' && 'old' in value && 'new' in value
}

function isDiagnosticList(value: unknown): value is Diagnostic[] {
  return Array.isArray(value) && value.every(isDiagnostic)
}

function isVehicleShape(value: unknown): value is Vehicle {
  return isRecord(value) && typeof value.name === 'string' && Array.isArray(value.stages)
}

function isRuleOutcomeList(value: unknown): value is RuleOutcome[] {
  return Array.isArray(value) && value.every(isRuleOutcome)
}

function isAssistantEvent(value: unknown): value is AssistantEvent {
  if (!isRecord(value) || typeof value.type !== 'string') return false
  switch (value.type) {
    case 'ready':
      return typeof value.configured === 'boolean'
    case 'assistant_delta':
    case 'assistant_text':
      return typeof value.text === 'string'
    case 'param_diff':
      return (
        typeof value.diff_id === 'string' &&
        isVehicleShape(value.vehicle) &&
        Array.isArray(value.changes) &&
        value.changes.every(isParamChange)
      )
    case 'diff_applied':
      return typeof value.diff_id === 'string' && isVehicleShape(value.vehicle)
    case 'diff_rejected':
      return typeof value.diff_id === 'string'
    case 'diagnosis':
      return (
        isDiagnosticList(value.constraints) &&
        isDiagnosticList(value.diagnostics) &&
        isRuleOutcomeList(value.rules) &&
        typeof value.note === 'string'
      )
    case 'instruction_rejected':
      return (
        typeof value.message === 'string' &&
        typeof value.suggestion === 'string' &&
        isDiagnosticList(value.diagnostics)
      )
    case 'instruction_invalid':
    case 'degraded':
    case 'error':
      return typeof value.message === 'string' && typeof value.suggestion === 'string'
    default:
      return false
  }
}

export interface AssistantStreamHandlers {
  /** 一条通过边界校验的服务端事件。 */
  onEvent: (event: AssistantEvent) => void
  /** 传输异常（含握手超时）；调用方据此向用户呈现降级提示。 */
  onError?: (error: ApiError) => void
  /** 通道关闭。 */
  onClose?: () => void
}

export interface AssistantStream {
  /** 发一条客户端消息；连接未就绪时入队，open 后自动冲刷。 */
  send: (message: AssistantClientMessage) => void
  close: () => void
}

/** WS 握手超时（同 `openJobStream` 的教训：连接可能**既不成功也不失败**地挂起）。 */
export const ASSISTANT_HANDSHAKE_TIMEOUT_MS = 3_000

/** WS 地址：由当前页面协议与主机推导（同源相对路径，禁止硬编码主机 / 端口，ADR-015）。 */
export function assistantStreamUrl(): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws/assistant`
}

/** 打开 AI 对话流。发消息在连接就绪前会被缓冲（用户输入不必等握手）。 */
export function openAssistantStream(handlers: AssistantStreamHandlers): AssistantStream {
  const socket = new WebSocket(assistantStreamUrl())
  let closed = false
  let handshaken = false
  const outbox: AssistantClientMessage[] = []

  const clearHandshake = (): void => {
    if (handshakeTimer !== null) {
      window.clearTimeout(handshakeTimer)
      handshakeTimer = null
    }
  }

  const abandon = (): void => {
    closed = true
    clearHandshake()
    socket.onopen = null
    socket.onmessage = null
    socket.onerror = null
    socket.onclose = null
    socket.close()
  }

  const flush = (): void => {
    while (outbox.length > 0) {
      const message = outbox.shift()
      if (message !== undefined) socket.send(JSON.stringify(message))
    }
  }

  let handshakeTimer: number | null = window.setTimeout(() => {
    if (closed || handshaken) return
    abandon()
    handlers.onError?.(
      new ApiError({
        code: 'ASSISTANT_STREAM_HANDSHAKE_TIMEOUT',
        stage: 'api',
        message: `AI 对话通道在 ${ASSISTANT_HANDSHAKE_TIMEOUT_MS} ms 内未建立`,
        suggestion: '确认后端已启动（GET /api/health）；开发态检查 Vite 代理是否转发了 /ws',
      }),
    )
  }, ASSISTANT_HANDSHAKE_TIMEOUT_MS)

  socket.onopen = () => {
    handshaken = true
    clearHandshake()
    flush()
  }

  socket.onmessage = (event: MessageEvent) => {
    let parsed: unknown
    try {
      parsed = JSON.parse(String(event.data))
    } catch {
      return
    }
    if (isAssistantEvent(parsed)) handlers.onEvent(parsed)
  }

  socket.onerror = () => {
    clearHandshake()
    handlers.onError?.(
      new ApiError({
        code: 'ASSISTANT_STREAM_ERROR',
        stage: 'api',
        message: 'AI 对话通道异常',
        suggestion: '已断开连接；重新打开面板可重试，其余建模功能不受影响（§10.2）',
      }),
    )
  }

  socket.onclose = () => {
    clearHandshake()
    handlers.onClose?.()
  }

  return {
    send: (message) => {
      if (closed) return
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(message))
      else outbox.push(message)
    },
    close: () => {
      if (closed) return
      abandon()
    },
  }
}
