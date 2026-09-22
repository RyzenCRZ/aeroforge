import { useEffect, useState } from 'react'

import { fetchHealth, type HealthResponse } from '../api/health'
import type { JobStage } from '../api/geometry'
import { useModelStore, type Channel, type ModelError } from '../store/model'
import './StatusBar.css'

/**
 * 状态栏文案表（规格 §11.11 规则 1：文案集中在单一表，禁止各模块自行拼接字符串）。
 *
 * M1 覆盖：连接中 / 就绪 / 后端未连接 / 校验中 / 校验未通过 / 构建中（含阶段名）/
 * 已就绪（权威）/ 构建失败。§11.11 的 2D 与计算类状态（section_ready / computed / frozen）
 * 属 M4/M5 消费者，届时在此表内增补而非另起字符串。
 */
const STATUS_TEXT = {
  connecting: '正在连接后端…',
  ready: '就绪',
  unreachable: '后端未连接',
  validating: '校验中…',
  invalid: '校验未通过',
  building: '构建中（{stage}）',
  authoritative: '已就绪（权威）',
  build_failed: '构建失败',
} as const

export type StatusKey = keyof typeof STATUS_TEXT

/**
 * 作业阶段中文名（§11.11 `geometry_ready` 的进度表述）。
 *
 * 与状态栏同属一处文案表：右栏构建区的阶段名也从这里取，避免两处各写一份。
 */
export const JOB_STAGE_TEXT: Record<JobStage, string> = {
  queued: '排队中',
  meridian: '计算母线',
  solid: '生成实体',
  mesh: '生成网格',
  step: '导出 STEP',
  export: '导出产物',
  sampling: 'MC 采样中',
  evaluating: 'MC 求值中',
  summarizing: 'MC 统计汇总',
  integrating: '弹道积分中',
  optimizing: '优化评估中',
  done: '完成',
}

/** 状态 → 指示灯语义色（引用语义 token，禁止字面色值，ADR-013） */
const STATUS_TONE: Record<StatusKey, string> = {
  connecting: 'warn',
  ready: 'ok',
  unreachable: 'error',
  validating: 'warn',
  invalid: 'error',
  building: 'warn',
  authoritative: 'ok',
  build_failed: 'error',
}

/** 连接态（本组件自身持有）：后端存活探针的结果。 */
export type ConnectionState = 'connecting' | 'ready' | 'unreachable'

export interface StatusInputs {
  connection: ConnectionState
  validating: boolean
  error: ModelError | null
  building: boolean
  job: { stage: JobStage } | null
  /** `ValidationReport.ok`；尚未取得报告时为 null */
  reportOk: boolean | null
  channel: Channel
  authoritativeKey: string | null
}

/**
 * 状态推导（§11.11 规则 2：同一时刻只显示一条主状态）。
 *
 * 优先级：连接态 → 构建中 → 失败态 → 校验中 → 校验未通过 → 权威就绪 → 就绪。
 * 失败态不自动消失（由 `clearError` 或下一次成功覆盖），符合规则 3。
 */
export function deriveStatusKey(inputs: StatusInputs): StatusKey {
  if (inputs.connection === 'connecting') return 'connecting'
  if (inputs.connection === 'unreachable') return 'unreachable'
  if (inputs.building) return 'building'
  if (inputs.error !== null) return inputs.error.origin === 'build' ? 'build_failed' : 'invalid'
  if (inputs.validating) return 'validating'
  if (inputs.reportOk === false) return 'invalid'
  if (inputs.channel === 'authoritative' && inputs.authoritativeKey !== null) return 'authoritative'
  return 'ready'
}

function formatStatus(key: StatusKey, stage: JobStage | null): string {
  const template: string = STATUS_TEXT[key]
  return template.replace('{stage}', stage === null ? '' : JOB_STAGE_TEXT[stage])
}

/** 状态栏：全局唯一反馈位（规格 §11.11 / FR-15）。 */
export function StatusBar() {
  const [connection, setConnection] = useState<ConnectionState>('connecting')
  const [health, setHealth] = useState<HealthResponse | null>(null)

  const report = useModelStore((state) => state.report)
  const validating = useModelStore((state) => state.validating)
  const error = useModelStore((state) => state.error)
  const building = useModelStore((state) => state.building)
  const job = useModelStore((state) => state.job)
  const channel = useModelStore((state) => state.channel)
  const authoritativeKey = useModelStore((state) => state.authoritativeKey)

  useEffect(() => {
    const controller = new AbortController()
    let active = true

    fetchHealth(controller.signal)
      .then((body) => {
        if (!active) return
        setHealth(body)
        setConnection('ready')
      })
      .catch(() => {
        if (!active) return
        setHealth(null)
        setConnection('unreachable')
      })

    return () => {
      active = false
      controller.abort()
    }
  }, [])

  const status = deriveStatusKey({
    connection,
    validating,
    error,
    building,
    job,
    reportOk: report === null ? null : report.ok,
    channel,
    authoritativeKey,
  })

  return (
    <footer className="status-bar surface">
      <span
        className={`status-bar__dot status-bar__dot--${STATUS_TONE[status]}`}
        aria-hidden="true"
      />
      <span className="status-bar__text" role="status">
        {formatStatus(status, job === null ? null : job.stage)}
      </span>
      {error !== null ? (
        <span className="status-bar__error" title={error.suggestion}>
          {error.message}
        </span>
      ) : null}
      {health !== null ? (
        <span className="status-bar__meta label">
          {health.name} v{health.version}
        </span>
      ) : null}
    </footer>
  )
}
