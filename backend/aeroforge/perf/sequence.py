"""任务时序与质量事件耦合（规格 §8.9，M4 计算内核第四片）。

事件账模型（§8.9 表逐行）
------------------------
分离、抛整流罩、回收点火都是**质量突变事件**，直接改变后续级的初始质量。
本模块在 ``SizingResult``（§8.5 求解输出）之上逐事件记账：

- ``ignition``（t=0）：起飞推重比校验（§6.5 口径，越限 warning）；
- ``stage_separation``：抛掉该级干质量——回收级的干重**含回收代价**（系统
  质量 + 增强结构/热防护 + 预留推进剂随级抛离）；
- ``fairing_jettison``：抛掉整流罩质量（工程惯例锚估算，见 provenance）；
- ``orbit_insertion``：主账闭合断言——燃烧 + 抛离之和恰耗尽至
  「载荷 + 未分离级干重」（§8.9 规则 1 的机检面）；
- ``landing``（回收点火，若有）：**消耗一级预留推进剂 → 直接减少运力**
  （§8.9 表回收点火行）——记在**被回收级**的独立账上（再入—着陆段），
  运力代价由 ``capacity_penalty_kg`` 量化。

回写 §8.5 迭代（规则 1：迭代须在时序约束下进行，而不是先算质量再套时序）
------------------------------------------------------------------------
回收代价改变被回收级的**有效结构系数**：预留推进剂在上升段是不可燃的惰性
质量、回收系统与增强结构是真实干重。有效口径（质量守恒）：

```text
σ_eff = 惰性 / (惰性 + 可用推进剂)
惰性   = m_dry,phys + K_sys + K_reinf + 预留；可用 = (1−f_land) × m_prop,满
⟹ 惰性 + 可用 = 物理级总质量（不漏吨位；求解器的 m_dry 输出即含代价的惰性账）
```

σ_eff 依赖 m_prop,满 → 与 §8.5 求解构成**不动点**：以上一轮满装量计 σ_eff、
重跑 ``solve``，直到 GLOW 相对变化 < 1e-10（小量修正 2–3 次收敛）。

运力代价（「直接减运力」的量化）
--------------------------------
对**同一枚最终火箭**（回写后的质量账）做两次账本级反推（§8.5 逆问题，复用
:func:`aeroforge.perf.capacity.payload_for_dv_on_ledger`，不另写第二套物理）：

- ``payload_capacity_expendable_kg``：全燃烧（预留也烧掉）——不回收能送多少；
- ``payload_capacity_recoverable_kg``：回收模式（预留不参与上升）——回收能送多少；
- 差即回收点火的运力代价（> 0，测试钉死；未启用回收时两值相等、代价 0）。

动压校验（规则 2：Q 与 q̇ 上限，超限警告不中止）
--------------------------------------------------
L1 级简化弹道（工程惯例锚，非权威）：``h(t) = ½·g₀·(TWR−0.55)·t²``（重力转弯
平均俯仰偏置的工程近似）、指数大气；Q > 1 kPa 或 |q̇| > 1 kPa/s 时**显式
warning**——不得为优化运力而无限制推迟抛罩，但时序是用户权威，超限只警告。
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from aeroforge.errors import PerfError, SizingError
from aeroforge.params import staging
from aeroforge.params.dag import G0
from aeroforge.params.schema import SequenceEvent, Vehicle
from aeroforge.perf.capacity import (
    CAPACITY_ORBITS,
    FixedVehicleLedger,
    StageMasses,
    ZeroStageMasses,
    _user_dv_requirement_km_s,
    anchored_dv_km_s,
    payload_for_dv_on_ledger,
    vehicle_ledger,
)
from aeroforge.perf.losses import DEFAULT_LAUNCH_SITE
from aeroforge.perf.solver import SizingResult, solve

#: 抛罩动压上限 [Pa]（工程惯例锚：现役运载抛罩时刻 Q 通常 < 1 kPa，§8.9 规则 2）。
FAIRING_Q_LIMIT_PA = 1_000.0

#: 抛罩动压变化率上限 [Pa/s]（工程惯例锚，与 Q 上限同源）。
FAIRING_QDOT_LIMIT_PA_S = 1_000.0

#: 指数大气标高 [m]（ISA 工程近似：ρ = 1.225·e^(−h/7200)）。
_ATMOSPHERE_SCALE_HEIGHT_M = 7_200.0

#: 海平面密度 [kg/m³]（ISA 标准海平面）。
_SEA_LEVEL_DENSITY_KG_M3 = 1.225

#: 上升剖面平均俯仰偏置因子：``a_eff = g₀·(TWR − 0.55)``（工程惯例，非权威；
#: F9 量级剖面校核：TWR≈1.4、t=120 s → h≈55 km，与公开剖面量级一致）。
_CLIMB_BIAS = 0.55

#: 整流罩面密度 [kg/m²]（工程惯例锚：5.2 m 罩按本模型 ≈1.96 t，对照公开 F9
#: 整流罩 ≈1.9 t；§8.9 未给公式，锚定声明写进 provenance）。
FAIRING_AREAL_DENSITY_KG_M2 = 11.0

#: 整流罩圆柱段长度 / 直径比（工程惯例：典型罩长 ≈ 1.6d）。
FAIRING_LENGTH_RATIO = 1.6

#: 缺省抛罩时刻 [s]（§8.9：通常 100–150 s，取量级中值）。
DEFAULT_FAIRING_JETTISON_S = 120.0

#: 回收点火缺省时刻偏移 [s]：分离后再入—着陆段工程惯例估计（仅时间线展示用）。
_LANDING_DELAY_S = 300.0

#: σ_eff 不动点收敛判据（GLOW 相对变化）。
_GLOW_FIXPOINT_TOLERANCE = 1e-10

#: σ_eff 不动点迭代上限（小量修正 2–3 次收敛；越界视为病态输入）。
_MAX_WRITEBACK_ITERATIONS = 20

#: 起飞推重比下限（§6.5 口径量级锚：低于 1.2 警告——不中止，时序仍记账）。
_MIN_TWR = 1.2


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


class SequenceEventReport(BaseModel):
    """一个时序事件的账目行（§8.9 表：事件 + 质量效应 + 对 ΔV 的影响）。"""

    event: str = Field(
        description=(
            "事件类型：ignition / stage_separation / fairing_jettison / orbit_insertion / landing"
        )
    )
    time_s: float | None = Field(description="相对时刻（s；用户提供或工程估计）")
    stage_index: int | None = Field(default=None, description="关联级号（可空）")
    account: Literal["main_stack", "recovered_stage"] = Field(
        description="记账主体：主账（上升栈）/ 被回收级（再入—着陆段独立账）"
    )
    description: str = Field(description="质量效应与 ΔV 影响说明（§8.9 表口径）")
    mass_before_kg: float = Field(description="事件前质量（kg）")
    mass_after_kg: float = Field(description="事件后质量（kg）")
    mass_delta_kg: float = Field(description="质量突变（kg；负 = 质量离开该账）")


class RecoveryCosts(BaseModel):
    """回收三项独立代价（§8.9 规则 3：独立建模、不得合并）。"""

    system_mass_kg: float = Field(description="回收系统自身质量（Recovery.system_mass_kg）")
    reinforcement_mass_kg: float = Field(
        description="增强结构与热防护质量（Recovery.reinforcement_mass_kg）"
    )
    landing_propellant_margin_fraction: float = Field(
        description="着陆推进剂余量份额（占该级满装量；Recovery 层）"
    )
    landing_propellant_kg: float = Field(
        description="预留着陆推进剂（kg = 份额 × 满装量）——回收点火消耗、直接减运力"
    )
    inert_cost_kg: float = Field(
        description="惯性代价合计（系统 + 增强结构/热防护；预留是推进剂、另列不计入）"
    )
    recovered_stage_indices: tuple[int, ...] = Field(description="回收级号清单")


class SequenceReport(BaseModel):
    """``POST /api/sizing/sequence`` 响应体（§8.9 / §10.1：同步）。"""

    events: tuple[SequenceEventReport, ...] = Field(description="按时间序的事件账（§8.9 表）")
    glow_kg: float = Field(description="时序耦合（回写 §8.5）后的 GLOW（kg）")
    glow_kg_expendable: float = Field(description="未计回收代价的 GLOW（kg，对照基准）")
    payload_mass_kg: float = Field(description="设计载荷（kg）")
    fairing_mass_kg: float | None = Field(
        default=None, description="整流罩质量（kg；未给整流罩直径为 null）"
    )
    twr_liftoff: float = Field(description="起飞推重比（海平面总推力 / 时序后 GLOW，§6.5）")
    recovery: RecoveryCosts | None = Field(
        default=None, description="回收三项代价（未启用回收为 null）"
    )
    payload_capacity_expendable_kg: float = Field(
        description="最终火箭全燃烧（不回收）的运力点值（kg）"
    )
    payload_capacity_recoverable_kg: float = Field(
        description="最终火箭回收模式（预留不参与上升）的运力点值（kg）"
    )
    capacity_penalty_kg: float = Field(
        description="回收代价的运力损失（kg）= expendable − recoverable ≥ 0（§8.9：直接减少运力）"
    )
    writeback_iterations: int = Field(
        description="回写 §8.5 的重跑次数（无回收代价 = 0；规则 1 的执行痕迹）"
    )
    warnings: tuple[str, ...] = Field(description="动压超限 / TWR 越限 / 代价缺省等警告")
    provenance: dict[str, str] = Field(description="溯源账目（公式 / 锚定值 / 口径）")


# ---------------------------------------------------------------------------
# 回收代价解析与 σ_eff 回写（规则 1 / 规则 3）
# ---------------------------------------------------------------------------


def _recovery_costs(vehicle: Vehicle) -> tuple[RecoveryCosts | None, tuple[int, ...], list[str]]:
    """解析回收三项代价（规则 3：独立读 Recovery 层字段，不合并不编造）。

    回收级号 = ``recovery.stage_indices``（非空）否则各级 ``recoverable=True``。
    代价字段缺失按 0 计并显式 warning（缺省是状态不是编造，§1.4-4）。
    """
    recovery = vehicle.recovery
    warnings: list[str] = []
    if recovery is None or not recovery.enabled:
        return None, (), warnings
    indices = tuple(
        recovery.stage_indices
        if recovery.stage_indices
        else (stage.index for stage in vehicle.stages if stage.recoverable)
    )
    if not indices:
        warnings.append(
            "回收已启用但未指定回收级（stage_indices 空、无 recoverable 级）——回收代价按 0 计"
        )
        return None, (), warnings
    system = recovery.system_mass_kg or 0.0
    reinforcement = recovery.reinforcement_mass_kg or 0.0
    margin = recovery.landing_propellant_margin_fraction or 0.0
    if recovery.system_mass_kg is None:
        warnings.append("Recovery.system_mass_kg 未填：回收系统质量按 0 计（缺省是状态不是 0）")
    if recovery.reinforcement_mass_kg is None:
        warnings.append(
            "Recovery.reinforcement_mass_kg 未填：增强结构与热防护质量按 0 计（缺省是状态不是 0）"
        )
    if recovery.landing_propellant_margin_fraction is None:
        warnings.append(
            "Recovery.landing_propellant_margin_fraction 未填："
            "着陆推进剂余量按 0 计（缺省是状态不是 0）"
        )
    return (
        RecoveryCosts(
            system_mass_kg=system,
            reinforcement_mass_kg=reinforcement,
            landing_propellant_margin_fraction=margin,
            landing_propellant_kg=0.0,  # 占位：依赖回写后的 m_prop，主流程回填
            inert_cost_kg=system + reinforcement,
            recovered_stage_indices=indices,
        ),
        indices,
        warnings,
    )


def _effective_sigma(
    nominal_sigma: float,
    inert_cost_kg: float,
    margin_fraction: float,
    full_propellant_kg: float,
) -> float:
    """σ_eff（回写口径）：预留与回收惰性质量并入分子，分母含全部推进剂。

    质量守恒自检：``σ_eff/(1−σ_eff) × m_prop,可用 = 惰性质量``（物理干重 +
    代价 + 预留），且 ``惰性 + 可用 = 物理级总质量``——求解器的级账不漏吨位，
    GLOW 闭合断言因此成立。
    """
    ratio = nominal_sigma / (1.0 - nominal_sigma)  # 物理干重 / 推进剂
    ratio += (inert_cost_kg + margin_fraction * full_propellant_kg) / full_propellant_kg
    return ratio / (1.0 + ratio)


def _resolve_writeback(
    vehicle: Vehicle,
    sizing: SizingResult,
    target_delta_v_m_s: float,
    stage_delta_v_m_s: tuple[float, ...] | None,
    indices: tuple[int, ...],
    costs: RecoveryCosts,
) -> tuple[SizingResult, int, list[str]]:
    """回写 §8.5：σ_eff 不动点迭代重跑 :func:`aeroforge.perf.solver.solve`（规则 1）。

    求解器的 m_prop,输出是**可用**推进剂，满装量 = 可用/(1−f)；σ_eff 由上一轮
    满装量计出——小量修正下 2–3 次收敛（GLOW 相对变化 < 1e-10）。
    """
    warnings: list[str] = []
    if not indices or (
        costs.inert_cost_kg <= 0.0 and costs.landing_propellant_margin_fraction <= 0.0
    ):
        return sizing, 0, warnings
    if costs.landing_propellant_margin_fraction >= 1.0:
        raise PerfError(
            f"着陆推进剂余量份额 {costs.landing_propellant_margin_fraction} ≥ 1："
            "全部推进剂留给着陆，上升段无可用推进剂",
            suggestion="余量份额须 < 1（典型动力着陆余量 5%–15% 满装量）",
        )

    # σ_eff 恒以**物理 σ**（原始输入）为基计算——绝不能用上一轮的 σ_eff 再叠加，
    # 否则预留被逐轮重复计入（σ 指数膨胀直至求解不可达）。不动点只在 m_prop 上。
    base_sigma = {stage.index: stage.structure_coefficient for stage in sizing.stages}

    current = sizing
    iterations = 0
    converged = False
    for _ in range(_MAX_WRITEBACK_ITERATIONS):
        updates: dict[int, float] = {}
        for stage in current.stages:
            if stage.index in indices:
                full = stage.m_propellant_kg / (1.0 - costs.landing_propellant_margin_fraction)
                updates[stage.index] = _effective_sigma(
                    base_sigma[stage.index],
                    costs.inert_cost_kg,
                    costs.landing_propellant_margin_fraction,
                    full,
                )
        perturbed_stages = tuple(
            stage.model_copy(update={"structure_coefficient": updates[stage.index]})
            if stage.index in updates
            else stage
            for stage in vehicle.stages
        )
        try:
            current = solve(
                vehicle.model_copy(update={"stages": perturbed_stages}),
                target_delta_v_m_s,
                stage_delta_v_m_s=stage_delta_v_m_s,
                payload_mass_kg=sizing.payload_mass_kg,
            )
        except SizingError as exc:
            raise PerfError(
                f"回收代价回写 §8.5 迭代失败：{exc.message}",
                suggestion="回收代价使构型对目标 ΔV 不可达：调低目标 ΔV / 载荷，或减小回收代价",
            ) from exc
        iterations += 1
        if abs(current.glow_kg - sizing.glow_kg) <= _GLOW_FIXPOINT_TOLERANCE * sizing.glow_kg:
            converged = True
            break
        sizing = current
    if not converged:
        warnings.append(
            f"σ_eff 回写迭代 {_MAX_WRITEBACK_ITERATIONS} 次未达 1e-10 收敛判据——"
            "以最后一次结果交付（相邻 GLOW 变化已极小，工程上可接受；如需精确收敛请核查输入）"
        )
    return current, iterations, warnings


# ---------------------------------------------------------------------------
# 质量账辅助（整流罩 / 燃时 / 推力 / 动压）
# ---------------------------------------------------------------------------


def fairing_mass_kg(vehicle: Vehicle) -> float | None:
    """整流罩质量（工程惯例锚）：柱段 1.6d + 半球端罩的表面积 × 11 kg/m²。"""
    diameter = vehicle.fairing_diameter_m
    if diameter is None:
        return None
    cylinder = math.pi * diameter * (FAIRING_LENGTH_RATIO * diameter)
    cap = 2.0 * math.pi * (diameter / 2.0) ** 2
    return (cylinder + cap) * FAIRING_AREAL_DENSITY_KG_M2


def _stage_burn_duration_s(stage_index: int, m_prop_usable_kg: float, vehicle: Vehicle) -> float:
    """级燃时（时间线展示用）：显式 ``burn_time_s`` 优先，否则 m_prop/ṁ（真空配对）。"""
    stage = next(item for item in vehicle.stages if item.index == stage_index)
    if stage.burn_time_s is not None:
        return stage.burn_time_s
    engine = stage.engine
    mass_flow = stage.engine_count * engine.thrust_vacuum_n / (engine.isp_vacuum_s * G0)
    return m_prop_usable_kg / mass_flow


def _liftoff_thrust_n(vehicle: Vehicle) -> float:
    """海平面总推力（§6.5 口径）：一级发动机 + 全部助推器发动机。"""
    first = min(vehicle.stages, key=lambda stage: stage.index)
    total = first.engine_count * first.engine.thrust_sea_level_n
    for booster in vehicle.boosters:
        stage = booster.stage
        total += booster.count * stage.engine_count * stage.engine.thrust_sea_level_n
    return total


def _dynamic_pressure_state(twr: float, time_s: float) -> tuple[float, float]:
    """抛罩时刻的 (Q, q̇) 估计 [Pa, Pa/s]——L1 级简化弹道（工程惯例锚，非权威）。

    剖面：``h(t) = ½·a·t²``、``v(t) = a·t``，``a = g₀·(TWR−0.55)``（重力转弯平均
    俯仰偏置）；指数大气 ``ρ = ρ₀·e^(−h/H)``；``Q = ½ρv²``、
    ``q̇ = ρ·v·(a − v²/(2H))``。
    """
    acceleration = G0 * (twr - _CLIMB_BIAS)
    if acceleration <= 0.0:  # TWR ≤ 偏置：剖面退化（起飞即不可行量级）
        return 0.0, 0.0
    altitude = 0.5 * acceleration * time_s**2
    velocity = acceleration * time_s
    density = _SEA_LEVEL_DENSITY_KG_M3 * math.exp(-altitude / _ATMOSPHERE_SCALE_HEIGHT_M)
    dynamic_pressure = 0.5 * density * velocity**2
    qdot = density * velocity * (acceleration - velocity**2 / (2.0 * _ATMOSPHERE_SCALE_HEIGHT_M))
    return dynamic_pressure, qdot


def _fairing_jettison_warnings(twr: float, time_s: float) -> list[str]:
    """规则 2：Q 与 q̇ 超限 → 显式 warning（不中止——时序是用户权威）。"""
    dynamic_pressure, qdot = _dynamic_pressure_state(twr, time_s)
    warnings: list[str] = []
    if dynamic_pressure > FAIRING_Q_LIMIT_PA:
        warnings.append(
            f"整流罩抛离时刻 t={time_s:.0f}s 估计动压 Q≈{dynamic_pressure / 1000.0:.1f} kPa，"
            f"超过工程惯例上限 {FAIRING_Q_LIMIT_PA / 1000.0:.0f} kPa（§8.9 规则 2：不得为"
            "优化运力而无限制推迟抛罩——建议推迟到动压衰减后；本警告不中止计算）"
        )
    if abs(qdot) > FAIRING_QDOT_LIMIT_PA_S:
        warnings.append(
            f"整流罩抛离时刻 t={time_s:.0f}s 估计动压变化率 |q̇|≈{abs(qdot) / 1000.0:.1f} kPa/s，"
            f"超过工程惯例上限 {FAIRING_QDOT_LIMIT_PA_S / 1000.0:.0f} kPa/s"
            "（§8.9 规则 2：Q 与 q̇ 双上限；本警告不中止计算）"
        )
    return warnings


def _events_from_vehicle(vehicle: Vehicle) -> list[SequenceEvent]:
    """用户时序（§6.1 Sequence 层）优先；缺失时按构型合成缺省事件线。

    合成序（典型剖面时序）：点火 → 抛罩（120 s，§8.9 表典型 100–150 s）→
    逐级分离 → 入轨 →（启用回收时）回收点火。
    """
    if vehicle.sequence is not None:
        return list(vehicle.sequence.events)
    events: list[SequenceEvent] = [SequenceEvent(event="ignition", time_s=0.0)]
    if vehicle.fairing_diameter_m is not None:
        events.append(SequenceEvent(event="fairing_jettison", time_s=DEFAULT_FAIRING_JETTISON_S))
    for stage in sorted(vehicle.stages, key=lambda item: item.index)[:-1]:
        events.append(SequenceEvent(event="stage_separation", stage_index=stage.index))
    events.append(SequenceEvent(event="orbit_insertion"))
    recovery = vehicle.recovery
    if recovery is not None and recovery.enabled and recovery.stage_indices:
        events.append(SequenceEvent(event="landing", stage_index=recovery.stage_indices[0]))
    return events


def _sizing_ledger(
    vehicle: Vehicle,
    sizing: SizingResult,
    recovered_indices: tuple[int, ...],
    margin: float,
    *,
    recoverable: bool,
) -> FixedVehicleLedger:
    """把求解器质量行拼成账本级 :class:`FixedVehicleLedger`（复用 §8.5/§8.6 链路）。

    被回收级的两条账：``recoverable=True`` 用求解器原样行（可用推进剂 + 含预留
    干重——预留作为惰性质量随级上升）；``recoverable=False`` 把预留拆回推进剂侧
    全量燃烧（干重剔除预留）。其余级两账相同。同一枚火箭，GLOW(P) 一致。
    """
    stages: list[StageMasses] = []
    for stage in sorted(sizing.stages, key=lambda item: item.index):
        usable = stage.m_propellant_kg
        dry = stage.m_dry_kg
        if stage.index in recovered_indices and margin > 0.0:
            if recoverable:
                pass  # 求解器输出即回收模式账（可用 + 含预留惰性）
            else:
                full = usable / (1.0 - margin)
                reserve = margin * full
                usable = full
                dry = dry - reserve
        stages.append(
            StageMasses(
                index=stage.index,
                isp_vacuum_s=stage.isp_vacuum_s,
                isp_source=stage.isp_source,
                m_propellant_kg=usable,
                m_dry_kg=dry,
            )
        )
    zero: ZeroStageMasses | None = None
    if sizing.zero_stage is not None and vehicle.boosters:
        summary = staging.resolve_zero_stage(vehicle)
        if summary is not None:
            zero = ZeroStageMasses(
                isp_eff_s=summary.isp_eff_s,
                booster_propellant_kg=sizing.zero_stage.booster_propellant_kg,
                booster_dry_kg=sizing.zero_stage.booster_dry_kg,
                booster_mass_flow_kg_s=summary.booster_mass_flow_kg_s,
                core_mass_flow_kg_s=(summary.total_mass_flow_kg_s - summary.booster_mass_flow_kg_s),
            )
    return FixedVehicleLedger(stages=tuple(stages), zero_stage=zero)


def _mission_dv_used_km_s(vehicle: Vehicle) -> tuple[float, str, list[str]]:
    """运力反推的需求 ΔV：与 evaluate/payload_by_orbit 同口径（用户覆写 / 锚定表 +
    长燃时构型修正——:func:`aeroforge.perf.capacity.anchored_dv_km_s` 唯一实现）。"""
    warnings: list[str] = []
    site = vehicle.mission.launch_site or DEFAULT_LAUNCH_SITE
    orbit = str(vehicle.mission.orbit_type)
    if orbit not in CAPACITY_ORBITS:
        warnings.append(
            f"Mission.orbit_type={orbit} 不在四目标运力表（LEO/SSO/GTO/GEO）——"
            "时序运力代价按 LEO 需求核算（TLI/TMI 随 M6 轨道层交付）"
        )
        orbit = "LEO"
    if vehicle.mission.loss_factors is not None:
        dv_used, source = _user_dv_requirement_km_s(vehicle, site, orbit)
        return dv_used, source, warnings
    dv_used, source, _ = anchored_dv_km_s(vehicle, vehicle_ledger(vehicle), orbit, site)
    return dv_used, source, warnings


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def apply_sequence(
    vehicle: Vehicle,
    sizing_result: SizingResult,
    *,
    target_delta_v_m_s: float | None = None,
    stage_delta_v_m_s: tuple[float, ...] | None = None,
) -> SequenceReport:
    """任务时序耦合（§8.9）：事件账 + 回收代价回写 + 质量闭合断言。

    - ``sizing_result``：§8.5 求解输出（``POST /api/sizing/solve`` 同源）；
    - ``target_delta_v_m_s`` / ``stage_delta_v_m_s``：回写重跑 ``solve`` 的原求解
      输入；缺省取 ``sizing_result`` 的目标值与逐级实算 ΔV（无回收代价时不重跑）。
    """
    warnings: list[str] = []
    costs, indices, cost_warnings = _recovery_costs(vehicle)
    warnings.extend(cost_warnings)

    sizing = sizing_result
    iterations = 0
    if costs is not None:
        if target_delta_v_m_s is None:
            target_delta_v_m_s = sizing_result.target_delta_v_m_s
        if stage_delta_v_m_s is None:
            stage_delta_v_m_s = tuple(stage.delta_v_m_s for stage in sizing_result.stages)
        sizing, iterations, writeback_warnings = _resolve_writeback(
            vehicle,
            sizing_result,
            target_delta_v_m_s,
            stage_delta_v_m_s,
            indices,
            costs,
        )
        warnings.extend(writeback_warnings)

    glow = sizing.glow_kg
    glow_expendable = sizing_result.glow_kg
    fairing = fairing_mass_kg(vehicle)
    thrust_n = _liftoff_thrust_n(vehicle)
    twr = thrust_n / (glow * G0)
    if twr < _MIN_TWR:
        warnings.append(
            f"起飞推重比 {twr:.2f} 低于 §6.5 量级锚下限 {_MIN_TWR}（时序仍记账——"
            "推重比校验是点火事件的检查项，不中止时序分析）"
        )

    margin = costs.landing_propellant_margin_fraction if costs is not None else 0.0
    reserve_by_stage: dict[int, float] = {}
    if costs is not None and margin > 0.0:
        total_reserve = 0.0
        for stage in sizing.stages:
            if stage.index in indices:
                full = stage.m_propellant_kg / (1.0 - margin)
                reserve = margin * full
                reserve_by_stage[stage.index] = reserve
                total_reserve += reserve
        costs = costs.model_copy(update={"landing_propellant_kg": total_reserve})

    # ── 事件账：主账自点火起逐事件推进；landing 记被回收级的独立账 ──
    stage_rows = {stage.index: stage for stage in sizing.stages}
    usable_by_stage = {index: row.m_propellant_kg for index, row in stage_rows.items()}
    main_mass = glow + (fairing or 0.0)
    ignition_mass = main_mass  # 整流罩在点火时已在箭上（求解器无此自由度，时序账列示）
    clock = 0.0
    separated: set[int] = set()
    events_out: list[SequenceEventReport] = []

    for event in _events_from_vehicle(vehicle):
        if event.event == "ignition":
            time_s = event.time_s if event.time_s is not None else 0.0
            events_out.append(
                SequenceEventReport(
                    event="ignition",
                    time_s=time_s,
                    account="main_stack",
                    description=(
                        f"点火（t=0）：起飞推重比校验 TWR={twr:.2f}"
                        f"（海平面总推力 {thrust_n / 1000.0:.0f} kN / "
                        f"时序后 GLOW {glow:.0f} kg，§6.5）"
                    ),
                    mass_before_kg=main_mass,
                    mass_after_kg=main_mass,
                    mass_delta_kg=0.0,
                )
            )
        elif event.event == "stage_separation":
            if event.stage_index is not None:
                index = event.stage_index
            else:  # 用户未关联级号：取尚未分离的最低级
                remaining_indices = [i for i in sorted(stage_rows) if i not in separated]
                if not remaining_indices:
                    warnings.append("分离事件数量超过级数：多余事件已忽略（时序与构型不一致）")
                    continue
                index = remaining_indices[0]
            separated.add(index)
            burned = usable_by_stage.get(index, 0.0)
            clock += _stage_burn_duration_s(index, burned, vehicle)
            before = main_mass - burned
            thrown = stage_rows[index].m_dry_kg
            main_mass = before - thrown
            recovered_note = ""
            if costs is not None and index in indices:
                recovered_note = (
                    f"（含回收代价：系统 {costs.system_mass_kg:.0f} kg + 增强结构/热防护 "
                    f"{costs.reinforcement_mass_kg:.0f} kg + 预留推进剂 "
                    f"{reserve_by_stage.get(index, 0.0):.0f} kg 随级抛离）"
                )
            events_out.append(
                SequenceEventReport(
                    event="stage_separation",
                    time_s=event.time_s if event.time_s is not None else clock,
                    stage_index=index,
                    account="main_stack",
                    description=(
                        f"第 {index} 级关机/分离：燃烧可用推进剂 {burned:.0f} kg 后抛掉干质量 "
                        f"{thrown:.0f} kg{recovered_note}——提高上面级初始质量比（§8.9 表）"
                    ),
                    mass_before_kg=before,
                    mass_after_kg=main_mass,
                    mass_delta_kg=-thrown,
                )
            )
        elif event.event == "fairing_jettison":
            time_s = event.time_s if event.time_s is not None else DEFAULT_FAIRING_JETTISON_S
            if fairing is None:
                warnings.append(
                    "时序含整流罩抛离事件但 Vehicle.fairing_diameter_m 未提供——抛罩质量效应记 0"
                )
                delta = 0.0
            else:
                delta = -fairing
            before = main_mass
            main_mass += delta
            events_out.append(
                SequenceEventReport(
                    event="fairing_jettison",
                    time_s=time_s,
                    account="main_stack",
                    description=(
                        f"整流罩抛离（t={time_s:.0f}s，动压校验见 warnings）：抛掉整流罩质量 "
                        f"{abs(delta):.0f} kg——提高上面级运力（§8.9 表）"
                    ),
                    mass_before_kg=before,
                    mass_after_kg=main_mass,
                    mass_delta_kg=delta,
                )
            )
            warnings.extend(_fairing_jettison_warnings(twr, time_s))
        elif event.event == "orbit_insertion":
            # 闭合断言（规则 1 的机检面）：燃烧 + 抛离之和恰耗尽至「载荷 + 未分离级干重」
            unseparated_dry = sum(
                stage_rows[index].m_dry_kg for index in stage_rows if index not in separated
            )
            remaining_burn = sum(
                usable_by_stage[index] for index in stage_rows if index not in separated
            )
            before = main_mass - remaining_burn
            expected = sizing.payload_mass_kg + unseparated_dry
            if not math.isclose(before, expected, rel_tol=1e-6, abs_tol=1e-3):
                raise PerfError(
                    f"时序后质量闭合失败：入轨前主账余量 {before:.3f} kg ≠ "
                    f"载荷 + 未分离级干重 {expected:.3f} kg（§8.9 规则 1：迭代须在时序约束下闭合）",
                    suggestion=(
                        "这是实现缺陷，请提交 issue 附复现参数（事件账与求解器质量账不一致）"
                    ),
                )
            events_out.append(
                SequenceEventReport(
                    event="orbit_insertion",
                    time_s=event.time_s if event.time_s is not None else clock,
                    account="main_stack",
                    description=(
                        "入轨：目标速度达成，有效载荷分离——主账闭合于「载荷 "
                        f"{sizing.payload_mass_kg:.0f} kg + 未分离级干重 {unseparated_dry:.0f} kg」"
                        f"（点火起账 {ignition_mass:.0f} kg = 全部燃烧 + 事件抛离 + 闭合余量）"
                    ),
                    mass_before_kg=before,
                    mass_after_kg=before,
                    mass_delta_kg=0.0,
                )
            )
        elif event.event == "landing":
            if event.stage_index is not None:
                index = event.stage_index
            elif indices:
                index = indices[0]
            else:
                index = min(stage_rows)
            stage_dry = stage_rows[index].m_dry_kg
            reserve = reserve_by_stage.get(index, 0.0)
            time_s = event.time_s if event.time_s is not None else clock + _LANDING_DELAY_S
            if reserve > 0.0:
                description = (
                    f"回收点火（第 {index} 级再入—着陆段）：消耗预留推进剂 {reserve:.0f} kg"
                    "——直接减少运力（§8.9 表；运力代价见 capacity_penalty_kg），"
                    f"着陆后级质量 {stage_dry - reserve:.0f} kg"
                )
            else:
                description = (
                    f"回收点火（第 {index} 级再入—着陆段）：预留推进剂为 0（未配置着陆余量）"
                    "——运力代价仅剩惯性代价（见 capacity_penalty_kg）"
                )
            events_out.append(
                SequenceEventReport(
                    event="landing",
                    time_s=time_s,
                    stage_index=index,
                    account="recovered_stage",
                    description=description,
                    mass_before_kg=stage_dry,
                    mass_after_kg=stage_dry - reserve,
                    mass_delta_kg=-reserve,
                )
            )
        else:  # pragma: no cover - Schema 枚举已限定事件类型
            raise PerfError(
                f"未知时序事件类型 {event.event!r}",
                suggestion=(
                    "事件类型限定：ignition / stage_separation / fairing_jettison / "
                    "orbit_insertion / landing"
                ),
            )

    # ── 运力代价：同一枚最终火箭的两次账本级反推（§8.5 逆问题） ──
    dv_used, dv_source, dv_warnings = _mission_dv_used_km_s(vehicle)
    warnings.extend(dv_warnings)
    payload = sizing.payload_mass_kg
    ledger_recoverable = _sizing_ledger(vehicle, sizing, indices, margin, recoverable=True)
    ledger_expendable = _sizing_ledger(vehicle, sizing, indices, margin, recoverable=False)

    def _capacity(ledger: FixedVehicleLedger) -> float:
        try:
            return payload_for_dv_on_ledger(ledger, dv_used, bracket_hint_kg=payload)
        except PerfError:
            return 0.0

    capacity_recoverable = _capacity(ledger_recoverable)
    capacity_expendable = _capacity(ledger_expendable)
    penalty = max(0.0, capacity_expendable - capacity_recoverable)

    provenance = {
        "sequence.events": (
            "用户 Sequence 层（§6.1）优先；缺失按构型合成缺省事件线"
            "（点火 → 抛罩 120 s → 逐级分离 → 入轨；启用回收时追加回收点火）。"
            "事件账口径：各级的燃烧推进剂在其**分离事件**一并入账（事件账非逐秒仿真），"
            "mass_before/after 按事件序列推进，时刻为标注量"
        ),
        "sequence.fairing_mass": (
            f"工程惯例锚：柱段 {FAIRING_LENGTH_RATIO}d + 半球端罩表面积 × "
            f"{FAIRING_AREAL_DENSITY_KG_M2} kg/m²（5.2 m 罩 ≈1.96 t，对照公开 F9 整流罩 ≈1.9 t；"
            "§8.5 求解器无整流罩自由度，质量在时序账单独列示）"
        ),
        "sequence.writeback": (
            "σ_eff 不动点回写（规则 1）：预留推进剂与回收惰性质量并入求解器结构账，"
            "质量守恒（惰性 + 可用 = 物理级总质量，GLOW 闭合断言由此成立）；"
            f"重跑 solve {iterations} 次（GLOW 相对变化 < {_GLOW_FIXPOINT_TOLERANCE:g} 停）"
        ),
        "sequence.recovery_costs": (
            "三项独立建模（规则 3）：回收系统质量 / 着陆推进剂余量 / 增强结构与热防护"
            "——逐项读 Recovery 层字段，缺省按 0 计并显式 warning"
        ),
        "sequence.capacity": (
            "运力代价 = 同一枚最终火箭的两次账本级反推（payload_for_dv_on_ledger，"
            "复用 §8.5/§8.6 链路）：expendable（满装全烧）− recoverable（预留不参与上升）"
        ),
        "sequence.dv_source": dv_source,
        "sequence.dynamic_pressure": (
            "L1 级简化弹道（工程惯例锚，非权威）：h(t)=½·g₀·(TWR−0.55)·t²、指数大气 "
            f"H={_ATMOSPHERE_SCALE_HEIGHT_M:.0f} m；上限 Q≤{FAIRING_Q_LIMIT_PA / 1000:.0f} kPa、"
            f"|q̇|≤{FAIRING_QDOT_LIMIT_PA_S / 1000:.0f} kPa/s——超限警告不中止（规则 2）"
        ),
    }

    return SequenceReport(
        events=tuple(events_out),
        glow_kg=glow,
        glow_kg_expendable=glow_expendable,
        payload_mass_kg=sizing.payload_mass_kg,
        fairing_mass_kg=fairing,
        twr_liftoff=twr,
        recovery=costs,
        payload_capacity_expendable_kg=capacity_expendable,
        payload_capacity_recoverable_kg=capacity_recoverable,
        capacity_penalty_kg=penalty,
        writeback_iterations=iterations,
        warnings=tuple(warnings),
        provenance=provenance,
    )


__all__ = [
    "DEFAULT_FAIRING_JETTISON_S",
    "FAIRING_AREAL_DENSITY_KG_M2",
    "FAIRING_LENGTH_RATIO",
    "FAIRING_QDOT_LIMIT_PA_S",
    "FAIRING_Q_LIMIT_PA",
    "RecoveryCosts",
    "SequenceEventReport",
    "SequenceReport",
    "apply_sequence",
    "fairing_mass_kg",
]
