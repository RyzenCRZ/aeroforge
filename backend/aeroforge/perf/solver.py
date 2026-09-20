"""多级质量迭代求解器（规格 §8.5，M4 计算内核第二片）。

问题与算法（§8.5 伪码逐行落地）
--------------------------------
给定各级 Isp 与结构系数 σ、载荷质量、目标总 ΔV，求各级质量分配。级间质量**强
耦合**（上面级总质量是下面级的有效载荷），故**外层对 GLOW 割线迭代、内层自上而
下正向计算**：

```text
外层：对起飞质量 GLOW 迭代（割线法）
  内层：自上而下逐级正向计算
         m_payload,i+1 = m_stage,i（上面级总质量）
         m_prop,i     由 ΔV_i 与 Isp_i 反解（齐氏方程仅级内）
         m_dry,i      = m_prop,i × σ_i / (1 − σ_i)
  校验：Σ ΔV_i == ΔV_target ?（残差 = Σ 实算 ΔV − 目标）
  未收敛 → 割线修正 GLOW → 重复
```

数学结构：给定 ΔV 分配后，第 N..2 级的质量链**与 GLOW 无关**（由载荷逐级下推
闭式解出）；唯一自由度是 GLOW——它同时决定芯一级质量（质量守恒倒推）与其**实算
ΔV**（含 0 级段）。残差是 GLOW 的单调光滑函数，割线法收敛到机器精度地板。

ΔV 级间分配（Lagrange 初值）
---------------------------
精确一阶条件（min GLOW s.t. ΣΔV = 目标；对各级增长因子 ``Φ_i = λ_i(1−σ_i)/(1−λ_iσ_i)``
取 Lagrange 条件）：``(1/c_i)/(1 − λ_i·σ_i) = const``，其中 ``c_i = Isp_i·g₀``、
``λ_i = exp(ΔV_i/c_i)``——σ 与 ΔV 在指数上耦合，无闭式解。工程初值取
**ΔV_i ∝ √Isp_i**（任务口径）：等 Isp 时严格退化为均分；不等 Isp 时高比冲级
分更多 ΔV——量级上平衡「比冲的线性收益」与「干重随质量比的指数增长」。
该分配**同时是求解采用的分配**（求解器对分配不重优化，只对 GLOW 迭代；用户显式
给各级 ΔV 则直接用）。

0 级段（OI-36 / §8.5 合并规则）
-------------------------------
有助推器时串联链最底端加一段，**并联不是并联求解**——外层结构不变：

- 段初质量 = GLOW；段比冲 = ``staging.resolve_zero_stage`` 的
  ``Isp_eff = ΣF_vac/Σṁ``（真空配对，OI-35）；
- 段末质量 = GLOW −（全部助推器推进剂）−（全部助推器干重）——**助推器账口径**
  （该段从飞行器上移除的全部助推器质量）；
- **芯一级推进剂跨段连续核算**：0 级段内助推器与芯一级同时燃烧，段时长
  ``t₀ = 助推器推进剂 / 仅助推器流量``，芯级在段内烧掉 ``ṁ_core·t₀``（共享同一
  贮箱账，段末由该账结转——芯级段初质量 = 段末 − 芯级段内烧量）；芯级段烧芯级
  余量，两段烧量之和恰为芯一级推进剂总量；
- 助推器推进剂 / 干重**独立核算**（§8.5）：优先取 staging 的显式箱长账（用户
  权威），缺失时用 :mod:`aeroforge.perf.mass` 的几何解析估算补齐（兑现 staging
  的 deferred 承诺，禁止编造）；干重一律按其 σ 派生（σ 是存储权威）。

Isp 口径（QA-1 唯一权威）
-------------------------
各级 Isp 取 **DAG 已解析的值**（:func:`aeroforge.params.dag.vehicle_inputs`）：
``isp_source=default`` → 发动机标称值；``custom`` → 级层值（缺失即拒绝）。Schema
的发动机比冲是**实际值**（已含效率），CEA 理想值进入质量估算前须经 §8.3 效率修正
（:func:`aeroforge.perf.efficiency.isp_actual`）——本求解器的输入不经过 CEA 表，
故不重复施加 η（二次打折即双重修正）。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from aeroforge.errors import SizingError
from aeroforge.params import staging
from aeroforge.params.dag import G0, vehicle_inputs
from aeroforge.params.schema import Stage, Vehicle
from aeroforge.perf import mass as mass_module
from aeroforge.perf.mass import SIGMA_INTERVALS

#: 迭代上限（§8.5 求解器报告项：是否命中上限）。
MAX_ITERATIONS = 200

#: 收敛判据（§8.5）：|ΣΔV 实算 − 目标| / 目标 < 1e-6。
CONVERGENCE_TOLERANCE = 1e-6

#: 内部推进地板：割线在光滑单调残差上可稳定到达机器精度量级，继续推进到
#: 1e-12 可保证闭式解对拍（GLOW 相对误差 ~1e-12，远优于 1e-6 容差）；
#: 报告口径仍按 CONVERGENCE_TOLERANCE 判定「已收敛」。
_INTERNAL_TOLERANCE = 1e-12

#: 割线法第二初值的外推系数（首点来自级质量粗估，第二点放大寻找割线斜率）。
_SECOND_POINT_FACTOR = 1.5

#: GLOW 迭代的 safeguard 上界（防发散外溢；真解远小于该值）。
_GLOW_MAX_FACTOR = 1e9


# ---------------------------------------------------------------------------
# 结果模型（纯 pydantic 基本类型——响应链上不得出现任何 OCCT 依赖）
# ---------------------------------------------------------------------------


class CrossCheckOutcome(BaseModel):
    """§8.4 一致性校验结果（几何解析 vs σ 推算干重）。"""

    m_dry_geometric_kg: float = Field(description="几何解析干重（湿面积 × 面密度，kg）")
    m_dry_sigma_kg: float = Field(description="σ 推算干重（kg；σ 是存储权威，对照基准）")
    relative_deviation: float = Field(description="相对偏差（|几何 − σ| / σ）")
    exceeds_threshold: bool = Field(description="是否超过 §8.4 的 20% 阈值")


class StageSizing(BaseModel):
    """单级（芯级串联链）的求解结果。"""

    index: int = Field(description="级序号（1 = 第一级，自下而上）")
    isp_vacuum_s: float = Field(description="该级真空比冲（s；DAG 解析值，QA-1）")
    isp_source: str = Field(description="比冲来源：engine（default）/ stage（custom 覆写）")
    structure_coefficient: float = Field(description="结构系数 σ（用户存储权威）")
    m_propellant_kg: float = Field(description="推进剂质量（kg）")
    m_dry_kg: float = Field(description="干质量（kg）= m_prop × σ/(1−σ)")
    m_total_kg: float = Field(description="级总质量（kg）= 推进剂 + 干重")
    delta_v_m_s: float = Field(description="该级实算 ΔV（m/s；一级为芯级段，不含 0 级段）")
    m_above_kg: float = Field(description="该级之上 stacks（载荷 + 上面级总质量，kg）")
    cross_check: CrossCheckOutcome = Field(description="§8.4 双来源交叉校验")


class ZeroStageSizing(BaseModel):
    """0 级段明细（§8.5 合并规则 / OI-36）。"""

    booster_count: int = Field(description="并联助推器总枚数")
    isp_eff_s: float = Field(description="段平均有效比冲 Isp_eff = ΣF_vac/Σṁ（s，派生量）")
    m_start_kg: float = Field(description="段初质量（kg）= GLOW")
    m_end_kg: float = Field(
        description="段末质量（kg）= GLOW − 助推器推进剂 − 助推器干重（助推器账口径）"
    )
    booster_propellant_kg: float = Field(description="全部助推器推进剂（kg，独立核算）")
    booster_dry_kg: float = Field(description="全部助推器干重（kg，按各自 σ 派生）")
    core_propellant_burned_kg: float = Field(
        description="芯一级在 0 级段内烧掉的推进剂（kg，跨段账的段内份额）"
    )
    delta_v_m_s: float = Field(description="0 级段实算 ΔV（m/s）")
    propellant_source: str = Field(
        description="助推器推进剂来源：explicit_tank_lengths / geometric_estimate"
    )


class SolveReport(BaseModel):
    """§8.5 求解报告：迭代次数、残差、上限命中、σ 有效域。"""

    iterations: int = Field(description="割线迭代次数（内层求值次数，≥ 1）")
    residual_relative: float = Field(description="最终残差 |ΣΔV − 目标| / 目标")
    converged: bool = Field(description="是否满足 §8.5 收敛判据（1e-6）")
    hit_iteration_limit: bool = Field(description="是否命中 200 次迭代上限")
    residual_trail: tuple[float, ...] = Field(
        description="逐次残差轨迹（相对值；不收敛时用于诊断，不静默给半收敛结果）"
    )
    sigma_domain_warnings: tuple[str, ...] = Field(
        description="σ 越出 §8.4 有效域的逐级警告（越域警告不中止求解）"
    )


class SizingResult(BaseModel):
    """``POST /api/sizing/solve`` 的响应体（§8.5 / §10.1：同步、纯数值）。"""

    glow_kg: float = Field(description="起飞质量 GLOW（kg）")
    payload_mass_kg: float = Field(description="有效载荷质量（kg）")
    target_delta_v_m_s: float = Field(description="目标总 ΔV（m/s）")
    achieved_delta_v_m_s: float = Field(description="实算总 ΔV（m/s，含 0 级段）")
    stages: tuple[StageSizing, ...] = Field(description="芯级串联链逐级结果（自下而上）")
    zero_stage: ZeroStageSizing | None = Field(
        default=None, description="0 级段明细（无助推器为 null）"
    )
    report: SolveReport = Field(description="求解报告（§8.5）")
    warnings: tuple[str, ...] = Field(description="全部警告（交叉校验 >20%、σ 越域、跨段账封顶等）")
    provenance: dict[str, str] = Field(
        description="溯源账目：Isp 来源 / σ 来源 / 双来源分歧 / 0 级段口径"
    )


# ---------------------------------------------------------------------------
# 求解器内部结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StageInput:
    """一级的求解输入（DAG 解析后）。"""

    index: int
    isp_vacuum_s: float
    sigma: float
    isp_source: str  # "engine" | "stage"


@dataclass(frozen=True, slots=True)
class _ZeroStageInput:
    """0 级段求解输入（staging 汇总 + 助推器质量账）。"""

    count: int
    isp_eff_s: float
    booster_propellant_kg: float
    booster_dry_kg: float
    booster_mass_flow_kg_s: float
    core_mass_flow_kg_s: float
    propellant_source: str  # "explicit_tank_lengths" | "geometric_estimate"


@dataclass(frozen=True, slots=True)
class _StageRow:
    """一级的质量行（一级行由内层求值给出，上面级行由闭式链给出）。"""

    index: int
    m_propellant_kg: float
    m_dry_kg: float
    m_total_kg: float
    m_above_kg: float


@dataclass(frozen=True, slots=True)
class _BoosterLedger:
    """助推器质量账（§8.5 独立核算）。"""

    propellant_kg: float
    dry_kg: float
    propellant_source: str


@dataclass(frozen=True, slots=True)
class _InnerState:
    """给定 GLOW 的内层正向计算结果。"""

    glow_kg: float
    first: _StageRow
    zero_delta_v_m_s: float
    core_propellant_burned_kg: float
    core_burn_capped: bool
    residual_m_s: float


def _booster_ledger(vehicle: Vehicle, summary: staging.ZeroStageSummary) -> _BoosterLedger:
    """助推器推进剂 / 干重（§8.5：优先 staging 显式箱长账，缺则几何解析补齐）。

    staging 只有在**全部**侧级两箱都给出显式箱长时才给质量（分段柱体近似，
    §5.9 逆向）；否则按 :mod:`aeroforge.perf.mass` 的几何解析（含椭球封头，
    显式箱长仍优先）逐组估算——这是 staging ``deferred`` 里承诺的 M4 补齐路径，
    不是编造。干重一律按侧级 σ 派生（σ 是存储权威，与 staging 同式）。
    """
    if summary.booster_propellant_kg is not None and summary.booster_dry_kg is not None:
        return _BoosterLedger(
            propellant_kg=summary.booster_propellant_kg,
            dry_kg=summary.booster_dry_kg,
            propellant_source="explicit_tank_lengths",
        )
    propellant = 0.0
    dry = 0.0
    for booster in vehicle.boosters:
        per_stage = mass_module.propellant_mass_kg(booster.stage)
        propellant += booster.count * per_stage
        sigma = booster.stage.structure_coefficient
        dry += booster.count * per_stage * sigma / (1.0 - sigma)
    return _BoosterLedger(
        propellant_kg=propellant,
        dry_kg=dry,
        propellant_source="geometric_estimate",
    )


def _sigma_domain_warnings(vehicle: Vehicle) -> tuple[str, ...]:
    """σ 越出 §8.4 有效域的逐级警告（越域**警告不中止**，§8.5 报告项）。

    位置口径：芯级 index 1 = 一级、≥ 2 = 上面级；助推器侧级按一级口径对照
    （§8.4 未单列助推器）。相态取 Engine.propellant_phase（OI-30 唯一判据）；
    hybrid 无规格区间，如实不判定。
    """
    warnings: list[str] = []

    def _check(sigma: float, position: str, phase: str, label: str) -> None:
        if phase == "hybrid":
            return  # §8.4 无 hybrid 区间——不臆造判定
        propellant_class = "solid" if phase == "solid" else "liquid"
        interval = SIGMA_INTERVALS.get((position, propellant_class))
        if interval is None:
            return
        if not (interval[0] <= sigma <= interval[1]):
            warnings.append(
                f"{label}：σ={sigma} 越出 §8.4 有效域 [{interval[0]}, {interval[1]}]"
                f"（{position}/{propellant_class}）——求解继续，结果须对照 GCAT 回归"
                "与几何解析双来源复核（单来源不得作为结论）"
            )

    for stage in vehicle.stages:
        position = "first" if stage.index == 1 else "upper"
        _check(
            stage.structure_coefficient,
            position,
            stage.engine.propellant_phase,
            f"第 {stage.index} 级",
        )
    for booster_index, booster in enumerate(vehicle.boosters):
        _check(
            booster.stage.structure_coefficient,
            "booster",
            booster.stage.engine.propellant_phase,
            f"boosters[{booster_index}].stage",
        )
    return tuple(warnings)


def lagrange_initial_allocation(
    isps_vacuum_s: Sequence[float], target_delta_v_m_s: float
) -> tuple[float, ...]:
    """ΔV 级间分配初值（§8.5）：**ΔV_i ∝ √Isp_i**，归一化到目标。

    等比冲时严格退化为均分；不等比冲时高比冲级分更多（定性验收项）。推导与
    采用理由见模块 docstring（精确 Lagrange 条件无闭式解，√Isp 为工程解析初值）。
    """
    weights = [math.sqrt(isp) for isp in isps_vacuum_s]
    total_weight = sum(weights)
    if total_weight <= 0.0:
        msg = "比冲非正，无法构造 ΔV 分配初值"
        raise SizingError(msg, suggestion="检查各级发动机的真空比冲（必须 > 0）")
    return tuple(target_delta_v_m_s * weight / total_weight for weight in weights)


def _stage_inputs(vehicle: Vehicle) -> tuple[_StageInput, ...]:
    """从 DAG 取每级已解析的 Isp（QA-1：不在此处另写一套来源判定）。"""
    try:
        resolved = vehicle_inputs(vehicle)
    except ValueError as exc:
        raise SizingError(
            f"Isp 解析失败：{exc}",
            suggestion="先跑 POST /api/params/diagnose 修正比冲来源配置（custom 必填值）",
        ) from exc
    inputs: list[_StageInput] = []
    for stage in sorted(vehicle.stages, key=lambda s: s.index):
        sigma = stage.structure_coefficient
        if not (0.0 < sigma < 1.0):
            msg = f"第 {stage.index} 级结构系数 σ={sigma} 不在 (0,1) 开区间"
            raise SizingError(msg, suggestion="σ = m_dry/(m_dry+m_prop) 必须严格介于 0 与 1 之间")
        key = f"stage{stage.index}.isp_vacuum_s"
        if key not in resolved:
            msg = f"第 {stage.index} 级的 Isp 未出现在 DAG 解析结果中（{key}）"
            raise SizingError(
                msg, suggestion="检查该级 isp_source 配置：default 取发动机值，custom 须填值"
            )
        inputs.append(
            _StageInput(
                index=stage.index,
                isp_vacuum_s=resolved[key],
                sigma=sigma,
                isp_source="stage" if stage.isp_source == "custom" else "engine",
            )
        )
    return tuple(inputs)


def _top_down_upper_stages(
    payload_mass_kg: float,
    upper: Sequence[_StageInput],
    allocation_upper: Sequence[float],
) -> tuple[list[_StageRow], float]:
    """自上而下解第 N..2 级（与 GLOW 无关的闭式链）。

    每级：``λ = exp(ΔV_i/c_i)``，``m_prop = (λ−1)·P·(1−σ)/(1−λσ)``，
    ``m_dry = m_prop·σ/(1−σ)``，``m_stage = m_prop/(1−σ)``；上面级总质量成为
    下面级的载荷（``m_payload,i = m_stage,i+1`` 链）。``λσ ≥ 1`` 意味着该级
    Isp/σ 配比对分配到的 ΔV **无物理解**（干重增长吞掉全部收益）——立即拒绝。
    """
    rows: list[_StageRow] = []
    above = payload_mass_kg
    pairs = list(
        zip(reversed(upper), reversed(list(allocation_upper)), strict=True)
    )  # 自上而下：最末级先行
    for stage, delta_v in pairs:
        c = stage.isp_vacuum_s * G0
        lam = math.exp(delta_v / c)
        if lam * stage.sigma >= 1.0:
            max_dv = c * math.log(1.0 / stage.sigma)
            msg = (
                f"第 {stage.index} 级分配 ΔV={delta_v:.1f} m/s 超出该级 Isp/σ 可达上限 "
                f"{max_dv:.1f} m/s（Isp·g₀·ln(1/σ)，λσ ≥ 1 无物理解）"
            )
            raise SizingError(
                msg,
                suggestion="调低目标 ΔV、给该级更高比冲的发动机，或调低其结构系数 σ",
            )
        m_prop = (lam - 1.0) * above * (1.0 - stage.sigma) / (1.0 - lam * stage.sigma)
        m_dry = m_prop * stage.sigma / (1.0 - stage.sigma)
        rows.append(
            _StageRow(
                index=stage.index,
                m_propellant_kg=m_prop,
                m_dry_kg=m_dry,
                m_total_kg=m_prop + m_dry,
                m_above_kg=above,
            )
        )
        above += m_prop + m_dry
    rows.reverse()  # 呈现顺序改回自下而上
    return rows, above  # above = 一级之上 stacks（载荷 + 上面级总质量）


def _make_inner(
    first: _StageInput,
    m_above_kg: float,
    budget_first_m_s: float,
    zero_stage: _ZeroStageInput | None,
    target_delta_v_m_s: float,
) -> Callable[[float], _InnerState]:
    """构造内层正向计算（闭包捕获常量，外层只换 GLOW）。

    ``budget_first_m_s``：一级的 ΔV 预算（含 0 级段份额——助推器构型下 0 级段
    与芯级段的 ΔV 之和须命中该预算，两段的**拆分**由物理（流量比）决定）。
    """

    def evaluate(glow_kg: float) -> _InnerState:
        m_stage_1 = (
            glow_kg
            - m_above_kg
            - (
                zero_stage.booster_propellant_kg + zero_stage.booster_dry_kg
                if zero_stage is not None
                else 0.0
            )
        )
        if m_stage_1 <= 0.0:
            # GLOW 低于可行下限：给一个大负残差驱动割线向上（不抛错——收敛归外层）
            return _InnerState(
                glow_kg=glow_kg,
                first=_StageRow(first.index, 0.0, 0.0, 0.0, m_above_kg),
                zero_delta_v_m_s=0.0,
                core_propellant_burned_kg=0.0,
                core_burn_capped=False,
                residual_m_s=-target_delta_v_m_s,
            )
        m_prop_1 = (1.0 - first.sigma) * m_stage_1
        m_dry_1 = first.sigma * m_stage_1
        c_1 = first.isp_vacuum_s * G0
        first_row = _StageRow(first.index, m_prop_1, m_dry_1, m_stage_1, m_above_kg)

        if zero_stage is None:
            # 无助推器：一级段即从 GLOW 烧到 (GLOW − m_prop)（= 干重 + 上面 stacks）
            delta_v_1 = c_1 * math.log(glow_kg / (glow_kg - m_prop_1))
            return _InnerState(
                glow_kg=glow_kg,
                first=first_row,
                zero_delta_v_m_s=0.0,
                core_propellant_burned_kg=0.0,
                core_burn_capped=False,
                residual_m_s=delta_v_1 - budget_first_m_s,
            )

        # 0 级段（§8.5 跨段连续核算）：段时长 = 助推器推进剂 / 仅助推器流量；
        # 芯级在段内烧 ṁ_core·t₀（共享贮箱账），封顶于芯一级推进剂总量
        burn_duration = zero_stage.booster_propellant_kg / zero_stage.booster_mass_flow_kg_s
        core_burn = zero_stage.core_mass_flow_kg_s * burn_duration
        capped = core_burn >= m_prop_1
        core_burned = min(core_burn, m_prop_1)
        burnout = glow_kg - zero_stage.booster_propellant_kg - core_burned
        delta_v_0 = zero_stage.isp_eff_s * G0 * math.log(glow_kg / burnout)
        m_start_1 = burnout - zero_stage.booster_dry_kg  # 芯级段初 = 段末账 − 段内烧量
        m_end_1 = m_start_1 - (m_prop_1 - core_burned)  # = 一级干重 + 上面 stacks
        delta_v_1 = c_1 * math.log(m_start_1 / m_end_1)
        return _InnerState(
            glow_kg=glow_kg,
            first=first_row,
            zero_delta_v_m_s=delta_v_0,
            core_propellant_burned_kg=core_burned,
            core_burn_capped=capped,
            residual_m_s=(delta_v_0 + delta_v_1) - budget_first_m_s,
        )

    return evaluate


def _rough_glow_kg(
    payload_mass_kg: float,
    stages: Sequence[_StageInput],
    allocation: Sequence[float],
    glow_extra_kg: float,
) -> float:
    """GLOW 初值 = Σ级质量的粗估（§8.5：初值 = 级质量的粗估）。

    逐级用**理想**齐氏反解推进剂（不计干重耦合），干重按 σ 补上——系统性低估
    真解（精确解的分母 1−λσ < 1），残差自负侧逼近单调增函数，割线收敛稳健。
    """
    total = payload_mass_kg
    for stage, delta_v in zip(stages, allocation, strict=True):
        lam = math.exp(delta_v / (stage.isp_vacuum_s * G0))
        total += total * (lam - 1.0) / (1.0 - stage.sigma)
    return total + glow_extra_kg


def _stage_delta_v_m_s(
    stage: _StageInput,
    first_index: int,
    allocation_by_index: Mapping[int, float],
    state: _InnerState,
    has_zero_stage: bool,
) -> float:
    """该级的实算 ΔV：一级 = 预算 − 残差 −（0 级段 ΔV）；上面级 = 分配值。

    一级的芯级段 ΔV 由内层实算（残差收敛后 = 预算 − 0 级段 ΔV，数值上直接用
    ``预算 + 残差 − ΔV₀`` 保证 Σ 与目标的闭合精确到收敛精度）。
    """
    if stage.index != first_index:
        return allocation_by_index[stage.index]
    core_segment = allocation_by_index[first_index] + state.residual_m_s
    if has_zero_stage:
        core_segment -= state.zero_delta_v_m_s
    return core_segment


def solve(
    vehicle: Vehicle,
    target_delta_v_m_s: float,
    *,
    payload_mass_kg: float | None = None,
    stage_delta_v_m_s: Sequence[float] | None = None,
) -> SizingResult:
    """多级质量迭代求解（§8.5）。

    - ``target_delta_v_m_s``：目标总 ΔV（m/s，真空口径；§8.6 损失属另片）。
    - ``payload_mass_kg``：缺省取 ``Vehicle.payload_mass_kg``。
    - ``stage_delta_v_m_s``：用户显式给各级 ΔV（逐芯级、自下而上；一级的预算在
      助推器构型下 = 0 级段 + 芯级段之和）。须与目标总 ΔV 一致（1e-9 容差）。
    """
    if not (target_delta_v_m_s > 0.0):
        msg = f"目标总 ΔV 必须为正，收到 {target_delta_v_m_s}"
        raise SizingError(msg, suggestion="目标 ΔV 是求解的驱动量（如 LEO 约 9300–9500 m/s）")
    payload = vehicle.payload_mass_kg if payload_mass_kg is None else payload_mass_kg
    if not (payload > 0.0):
        msg = f"有效载荷质量必须为正，收到 {payload}"
        raise SizingError(msg, suggestion="零载荷的定尺求解没有意义（各级质量会全部退化为 0）")

    stages = _stage_inputs(vehicle)
    if not stages:
        msg = "飞行器没有任何串联级"
        raise SizingError(msg, suggestion="至少提供一级芯级（Stage.index = 1）")
    first = stages[0]
    upper = stages[1:]

    # ΔV 分配：用户显式（校验 Σ == 目标）或 Lagrange √Isp 初值
    if stage_delta_v_m_s is not None:
        allocation = tuple(float(value) for value in stage_delta_v_m_s)
        if len(allocation) != len(stages):
            msg = f"显式 ΔV 分配给了 {len(allocation)} 级，但飞行器有 {len(stages)} 个芯级"
            raise SizingError(
                msg, suggestion="按芯级数（自下而上）逐级给出 ΔV；0 级段不是独立分配项"
            )
        if any(value <= 0.0 for value in allocation):
            msg = "显式 ΔV 分配中存在非正项"
            raise SizingError(msg, suggestion="每级分配的 ΔV 必须为正")
        if abs(sum(allocation) - target_delta_v_m_s) > 1e-9 * target_delta_v_m_s:
            msg = (
                f"显式 ΔV 分配之和 {sum(allocation):.6f} m/s 与目标总 ΔV "
                f"{target_delta_v_m_s:.6f} m/s 不一致"
            )
            raise SizingError(
                msg, suggestion="修正分配使其求和等于目标（分配是总量的拆分，不是独立输入）"
            )
    else:
        allocation = lagrange_initial_allocation(
            [stage.isp_vacuum_s for stage in stages], target_delta_v_m_s
        )

    # 0 级段输入（有助推器时；Isp_eff / 流量取 staging，质量账独立核算）
    zero_input: _ZeroStageInput | None = None
    glow_extra_kg = 0.0
    if vehicle.boosters:
        summary = staging.resolve_zero_stage(vehicle)
        if summary is None:  # pragma: no cover - 有助推器时 resolve 必不返回 None
            msg = "存在助推器但 0 级段派生返回空"
            raise SizingError(msg, suggestion="检查 boosters 配置（§8.5 / OI-36）")
        ledger = _booster_ledger(vehicle, summary)
        if ledger.propellant_kg <= 0.0 or ledger.dry_kg <= 0.0:
            msg = "助推器推进剂 / 干重质量非正——无法构造 0 级段质量账"
            raise SizingError(
                msg,
                suggestion="检查助推器侧级的贮箱几何（显式箱长或级长/发动机高度）与加注比例",
            )
        zero_input = _ZeroStageInput(
            count=summary.count,
            isp_eff_s=summary.isp_eff_s,
            booster_propellant_kg=ledger.propellant_kg,
            booster_dry_kg=ledger.dry_kg,
            booster_mass_flow_kg_s=summary.booster_mass_flow_kg_s,
            core_mass_flow_kg_s=summary.total_mass_flow_kg_s - summary.booster_mass_flow_kg_s,
            propellant_source=ledger.propellant_source,
        )
        glow_extra_kg = ledger.propellant_kg + ledger.dry_kg

    # 内层：第 N..2 级闭式链（与 GLOW 无关）→ 一级之上的 stacks
    upper_rows, m_above_kg = _top_down_upper_stages(payload, upper, allocation[1:])
    glow_floor_kg = m_above_kg + glow_extra_kg  # GLOW 可行下限（一级质量 → 0⁺）

    evaluate = _make_inner(
        first=first,
        m_above_kg=m_above_kg,
        budget_first_m_s=allocation[0],
        zero_stage=zero_input,
        target_delta_v_m_s=target_delta_v_m_s,
    )

    # 外层：GLOW 割线迭代（初值 = 级质量粗估，第二点放大 1.5 倍取斜率）
    glow_0 = _rough_glow_kg(payload, stages, allocation, glow_extra_kg)
    glow_cap = glow_0 * _GLOW_MAX_FACTOR
    trail: list[float] = []

    state = evaluate(glow_0)
    trail.append(state.residual_m_s / target_delta_v_m_s)
    prev_glow, prev_residual = glow_0, state.residual_m_s
    glow = glow_0 * _SECOND_POINT_FACTOR
    state = evaluate(glow)
    trail.append(state.residual_m_s / target_delta_v_m_s)
    iterations = 2
    hit_limit = False

    while abs(state.residual_m_s) > _INTERNAL_TOLERANCE * target_delta_v_m_s:
        if iterations >= MAX_ITERATIONS:
            hit_limit = True
            break
        denominator = state.residual_m_s - prev_residual
        if denominator == 0.0 or not math.isfinite(denominator):
            break  # 割线停滞（残差平台 / 非有限值）——交由收敛判据裁决
        next_glow = glow - state.residual_m_s * (glow - prev_glow) / denominator
        if not math.isfinite(next_glow):
            break
        # safeguard：夹在可行下限与防外溢上界之间（最坏情形不越界发散）
        next_glow = min(max(next_glow, glow_floor_kg * (1.0 + 1e-9)), glow_cap)
        prev_glow, prev_residual = glow, state.residual_m_s
        glow = next_glow
        state = evaluate(glow)
        trail.append(state.residual_m_s / target_delta_v_m_s)
        iterations += 1

    residual_relative = abs(state.residual_m_s) / target_delta_v_m_s
    converged = residual_relative < CONVERGENCE_TOLERANCE
    if not converged:
        recent = " → ".join(f"{value:+.3e}" for value in trail[-8:])
        msg = (
            f"质量迭代不收敛：{iterations} 次求值后残差仍为 {residual_relative:.3e}"
            f"（判据 {CONVERGENCE_TOLERANCE:g}）。最近残差轨迹（相对值）：{recent}。"
            "常见原因：目标 ΔV 超出该构型 Isp/σ 的可达上限（单级上限 = Isp·g₀·ln(1/σ)），"
            "或 σ 接近 1 使干重吞掉全部质量收益"
        )
        raise SizingError(
            msg,
            suggestion="调低目标 ΔV、提高比冲或调低结构系数 σ；助推器构型另查 0 级段账",
            details={
                "iterations": iterations,
                "residual_relative": residual_relative,
                "residual_trail": trail[-10:],
                "hit_iteration_limit": hit_limit,
            },
            code="SIZING_NO_CONVERGENCE",
        )

    # ── 组装结果 ─────────────────────────────────────────────────────────────
    warnings: list[str] = []
    provenance: dict[str, str] = {}

    zero_detail: ZeroStageSizing | None = None
    if zero_input is not None:
        zero_detail = ZeroStageSizing(
            booster_count=zero_input.count,
            isp_eff_s=zero_input.isp_eff_s,
            m_start_kg=state.glow_kg,
            m_end_kg=state.glow_kg - zero_input.booster_propellant_kg - zero_input.booster_dry_kg,
            booster_propellant_kg=zero_input.booster_propellant_kg,
            booster_dry_kg=zero_input.booster_dry_kg,
            core_propellant_burned_kg=state.core_propellant_burned_kg,
            delta_v_m_s=state.zero_delta_v_m_s,
            propellant_source=zero_input.propellant_source,
        )
        provenance["zero_stage.isp_eff_s"] = (
            "staging.resolve_zero_stage（ΣF_vac/Σṁ，OI-35 真空配对；派生量禁止手填）"
        )
        provenance["zero_stage.propellant_source"] = (
            "explicit_tank_lengths（staging 显式箱长账，§5.9 分段柱体近似）"
            if zero_input.propellant_source == "explicit_tank_lengths"
            else "geometric_estimate（perf.mass 几何解析：柱段 + 椭球封头，显式箱长优先）"
        )
        if state.core_burn_capped:
            warnings.append(
                "0 级段时长内芯一级贮箱先于助推器烧空（跨段账封顶）：芯级段 ΔV 为 0，"
                "该构型的助推器流量相对芯级贮箱过大（§8.5 跨段连续核算的封顶分支）"
            )

    schema_stages: list[Stage] = sorted(vehicle.stages, key=lambda s: s.index)
    allocation_by_index = {
        stage.index: value for stage, value in zip(stages, allocation, strict=True)
    }
    stage_rows = [state.first, *upper_rows]
    stage_sizings: list[StageSizing] = []
    for row, stage, schema_stage in zip(stage_rows, stages, schema_stages, strict=True):
        check = mass_module.cross_check(schema_stage)
        if check.warning is not None:
            warnings.append(check.warning)
        stage_sizings.append(
            StageSizing(
                index=stage.index,
                isp_vacuum_s=stage.isp_vacuum_s,
                isp_source=stage.isp_source,
                structure_coefficient=stage.sigma,
                m_propellant_kg=row.m_propellant_kg,
                m_dry_kg=row.m_dry_kg,
                m_total_kg=row.m_total_kg,
                delta_v_m_s=_stage_delta_v_m_s(
                    stage, first.index, allocation_by_index, state, zero_input is not None
                ),
                m_above_kg=row.m_above_kg,
                cross_check=CrossCheckOutcome(
                    m_dry_geometric_kg=check.m_dry_geometric_kg,
                    m_dry_sigma_kg=check.m_dry_sigma_kg,
                    relative_deviation=check.relative_deviation,
                    exceeds_threshold=check.exceeds_threshold,
                ),
            )
        )
        provenance[f"stage{stage.index}.isp_vacuum_s"] = (
            f"{stage.isp_source}（"
            + (
                "engine.isp_vacuum_s，isp_source=default，QA-1"
                if stage.isp_source == "engine"
                else "stage.isp_vacuum_s，isp_source=custom 用户覆写"
            )
            + "）"
        )
        provenance[f"stage{stage.index}.sigma"] = "user（存储权威，§6.1；回归值仅对照）"
        provenance[f"stage{stage.index}.dual_source"] = (
            f"几何解析干重 vs σ 推算干重偏差 {check.relative_deviation:.1%}"
            f"（阈值 {mass_module.CROSS_CHECK_THRESHOLD:.0%}，"
            f"{'超限报警' if check.exceeds_threshold else '未超限'}）"
        )

    sigma_warnings = _sigma_domain_warnings(vehicle)
    warnings.extend(sigma_warnings)
    achieved = state.zero_delta_v_m_s + sum(item.delta_v_m_s for item in stage_sizings)

    return SizingResult(
        glow_kg=state.glow_kg,
        payload_mass_kg=payload,
        target_delta_v_m_s=target_delta_v_m_s,
        achieved_delta_v_m_s=achieved,
        stages=tuple(stage_sizings),
        zero_stage=zero_detail,
        report=SolveReport(
            iterations=iterations,
            residual_relative=residual_relative,
            converged=converged,
            hit_iteration_limit=hit_limit,
            residual_trail=tuple(trail),
            sigma_domain_warnings=sigma_warnings,
        ),
        warnings=tuple(warnings),
        provenance=provenance,
    )


__all__ = [
    "CONVERGENCE_TOLERANCE",
    "MAX_ITERATIONS",
    "CrossCheckOutcome",
    "SizingResult",
    "SolveReport",
    "StageSizing",
    "ZeroStageSizing",
    "lagrange_initial_allocation",
    "solve",
]
