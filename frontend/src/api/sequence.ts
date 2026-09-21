import { isRecord, postJson } from './client'
import type { Vehicle } from './params'
import type { components } from './schema'

/**
 * 任务时序域 API（§8.9 / §10.1：`POST /api/sizing/sequence`，同步）。
 *
 * 时序耦合求解：按 Sequence 层事件线与 Recovery 层代价配置（§8.9 规则 3 的三项
 * **独立**代价）重跑尺寸综合并回写 §8.5，响应带回收代价分解与运力对照。
 *
 * 类型一律取自 OpenAPI 生成物（§18.2：后端类型禁止手写重复定义）；本模块只做传输与
 * **边界**校验，不含任何物理计算（ADR-011）。
 */

export type SizingSequenceRequest = components['schemas']['SizingSequenceRequest']
export type SequenceReport = components['schemas']['SequenceReport']
export type SequenceEventReport = components['schemas']['SequenceEventReport']
export type RecoveryCosts = components['schemas']['RecoveryCosts']

/** 回收三项独立代价的边界校验（§8.9 规则 3：五数值 + 级号清单，缺一即契约破坏）。 */
function isRecoveryCosts(value: unknown): value is RecoveryCosts {
  if (!isRecord(value)) return false
  return (
    typeof value.system_mass_kg === 'number' &&
    typeof value.reinforcement_mass_kg === 'number' &&
    typeof value.landing_propellant_margin_fraction === 'number' &&
    typeof value.landing_propellant_kg === 'number' &&
    typeof value.inert_cost_kg === 'number' &&
    Array.isArray(value.recovered_stage_indices) &&
    value.recovered_stage_indices.every((item) => typeof item === 'number')
  )
}

function isSequenceReport(value: unknown): value is SequenceReport {
  if (!isRecord(value)) return false
  return (
    Array.isArray(value.events) &&
    typeof value.glow_kg === 'number' &&
    typeof value.glow_kg_expendable === 'number' &&
    typeof value.payload_mass_kg === 'number' &&
    typeof value.twr_liftoff === 'number' &&
    (value.recovery === null || value.recovery === undefined || isRecoveryCosts(value.recovery)) &&
    typeof value.payload_capacity_expendable_kg === 'number' &&
    typeof value.payload_capacity_recoverable_kg === 'number' &&
    typeof value.capacity_penalty_kg === 'number' &&
    typeof value.writeback_iterations === 'number' &&
    Array.isArray(value.warnings)
  )
}

/**
 * 时序耦合求解（§8.9）。同步接口：响应即终态（无作业通道）。
 *
 * @param vehicle 飞行器参数（Sequence / Recovery 层内嵌其中）
 * @param targetDeltaVMs 目标总 ΔV（m/s，真空口径）——请求体显式携带（§8.9 / sizing 同口径）
 */
export function solveSequence(vehicle: Vehicle, targetDeltaVMs: number): Promise<SequenceReport> {
  const body: SizingSequenceRequest = { vehicle, target_delta_v_m_s: targetDeltaVMs }
  return postJson('/api/sizing/sequence', body, isSequenceReport)
}
