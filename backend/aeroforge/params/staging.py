"""0 级段派生（规格 §8.5「并联助推器的合并规则」/ §1.7.6 OI-36）。

口径
----
助推器与芯一级构成「0 级段」——串联链最底端再加一段。本模块给出该段的
**派生量汇总**（§8.5：``Isp_eff`` 是派生量，不是新输入）：

- 段比冲 = 并联组合**平均有效比冲** ``Isp_eff = ΣF_vac / Σṁ``；
- 真空口径配对（OI-35）：每台发动机 ``ṁ_i = F_vac_i / (Isp_vac_i · g₀)``——
  **禁止**拿海平面比冲配真空推力（显式报错，绝不静默回落）；
- 芯一级参与合并（0 级段的推力与流量 = 全部助推器 + 芯一级）；
- 助推器推进剂 / 干重按其 σ 与加注比例**独立核算**，不与芯级合并成"当量级"。

边界
----
本模块是**纯函数**（无 IO、无 SQL、无内核依赖），只做 M4 就能算的汇总；
质量账的完整求解（GLOW 迭代、ΔV 分配）仍归 M4 求解器。参数层**算不出**的
质量一律进 ``deferred`` 留痕（禁止编造，"没报错 ≠ 正确"）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aeroforge.params import propellants
from aeroforge.params.dag import G0, dry_to_prop_ratio
from aeroforge.params.schema import Engine, Stage, Vehicle

#: 派生量声明（§8.5）：随 summary 下发，防止它被误认为可手工填写的输入。
ISP_EFF_NOTE = "Isp_eff 为派生量（§8.5），禁止手工填写"

#: OI-35 配对规则被破坏时的报错文案锚点（测试用它锁定"显式报错"而非静默回落）。
VACUUM_PAIR_ERROR = (
    "缺少真空口径推力/比冲（OI-35 配对规则：ṁ 只用真空推力配真空比冲，禁止拿海平面值配真空口径）"
)


@dataclass(frozen=True, slots=True)
class ZeroStageSummary:
    """0 级段的派生量汇总（§8.5）。

    ``booster_propellant_kg`` / ``booster_dry_kg`` 在参数层无可得质量时为
    ``None``——缺什么写进 :attr:`deferred`，**不填**看着合理的数（§1.4-4）。
    """

    count: int
    """并联助推器总枚数（各组 count 之和）。"""

    total_vacuum_thrust_n: float
    """段真空总推力（N）= 全部助推器发动机 + 芯一级发动机的 F_vac 之和。"""

    total_mass_flow_kg_s: float
    """段总真空流量（kg/s）= Σ 各台发动机 F_vac/(Isp_vac·g₀)（OI-35 配对）。"""

    isp_eff_s: float
    """段平均有效比冲（s）= ΣF_vac / Σṁ（派生量，见 :data:`ISP_EFF_NOTE`）。"""

    booster_propellant_kg: float | None
    """全部助推器的推进剂量（kg）；参数层无可得质量时为 None（原因进 deferred）。"""

    booster_dry_kg: float | None
    """全部助推器的干重（kg，按各侧级 σ 派生）；推进剂量不可得时同为 None。"""

    deferred: tuple[str, ...] = ()
    """不可得项的显式说明（M4 求解器接入时补齐）；空 = 全部可得。"""

    note: str = ISP_EFF_NOTE


def _vacuum_pair(engine: Engine, where: str) -> tuple[float, float]:
    """取一台发动机的真空配对值 ``(F_vac, Isp_vac)``；缺失即**显式报错**（OI-35）。

    Schema 层已强制两字段必填且 > 0；本防御针对绕过校验构造的模型
    （``model_construct`` / ``model_copy`` 不校验）——最坏也要"响"，绝不拿
    海平面值凑数。
    """
    thrust = engine.thrust_vacuum_n
    isp = engine.isp_vacuum_s
    if not (thrust > 0.0) or not (isp > 0.0):
        raise ValueError(f"{where}{VACUUM_PAIR_ERROR}")
    return thrust, isp


def _tank_length_pair(stage: Stage) -> tuple[float, float] | None:
    """两侧贮箱的显式箱长；任一缺失即 ``None``（推进剂质量不可得的判据）。

    §5.9 的派生方向是「质量 → 容积 → 箱长」，故只有**用户显式给定**的箱长
    （``tank.length_m``）才能逆向支撑质量账；派生箱长参数层根本不存。
    """
    ox = stage.geometry.oxidizer_tank
    fuel = stage.geometry.fuel_tank
    if ox.length_m is None or fuel.length_m is None:
        return None
    return ox.length_m, fuel.length_m


def _booster_propellant_mass_kg(stage: Stage, length_ox: float, length_fuel: float) -> float:
    """单枚助推器侧级的推进剂质量（分段柱体近似，§5.9 派生规则 1 的逆向使用）。

    ``V = 截面积 × 箱长``（截面积取级直径），``m = V × ρ × 加注比例``——
    氧化剂箱 / 燃料箱各按本剂密度计（近似方式与 §5.9 同口径）。
    """
    area = math.pi * stage.diameter_m**2 / 4.0
    props = propellants.properties(stage.propellant)
    return (
        area * length_ox * props.density_ox_kg_m3 + area * length_fuel * props.density_fuel_kg_m3
    ) * stage.fill_fraction


def resolve_zero_stage(vehicle: Vehicle) -> ZeroStageSummary | None:
    """0 级段派生（§8.5）：无助推器返回 ``None``；有则返回段汇总。

    段 = 芯一级（``stages[0]``）+ 全部助推器（各组的 ``count × stage`` 并联合并）。
    推进剂/干重只在该侧级**两箱均给出显式箱长**时按几何反推；否则为 ``None``
    并写明 ``deferred``——由 M4 定尺求解补齐，禁止编造（§1.4-4）。
    """
    if not vehicle.boosters:
        return None
    if not vehicle.stages:
        raise ValueError(
            "存在助推器但没有任何串联级：0 级段必须挂在芯一级之下"
            "（§8.5 / HARD_BOOSTER_INVALID 同判据）"
        )

    core = vehicle.stages[0]
    total_thrust = 0.0
    total_flow = 0.0

    # 芯一级参与合并（§8.5）
    core_thrust, core_isp = _vacuum_pair(core.engine, "stages[0].engine：")
    total_thrust += core.engine_count * core_thrust
    total_flow += core.engine_count * core_thrust / (core_isp * G0)

    propellant_total = 0.0
    dry_total = 0.0
    count_total = 0
    propellant_available = True
    deferred: list[str] = []

    for position, booster in enumerate(vehicle.boosters):
        stage = booster.stage
        count_total += booster.count
        thrust, isp = _vacuum_pair(stage.engine, f"boosters[{position}].stage.engine：")
        total_thrust += booster.count * stage.engine_count * thrust
        total_flow += booster.count * stage.engine_count * thrust / (isp * G0)

        lengths = _tank_length_pair(stage)
        if lengths is None:
            propellant_available = False
            deferred.append(
                f"boosters[{position}]：两箱未同时给出显式箱长（tank.length_m），"
                "参数层暂无可得推进剂质量——M4 定尺求解接入时补齐（禁止编造，§1.4-4）"
            )
            continue
        mass = _booster_propellant_mass_kg(stage, *lengths)
        propellant_total += booster.count * mass
        # 干重按该侧级 σ 独立派生（σ 是存储权威，§6.1）：m_dry = m_prop · σ/(1−σ)
        dry_total += booster.count * mass * dry_to_prop_ratio(stage.structure_coefficient)

    return ZeroStageSummary(
        count=count_total,
        total_vacuum_thrust_n=total_thrust,
        total_mass_flow_kg_s=total_flow,
        isp_eff_s=total_thrust / total_flow,
        booster_propellant_kg=propellant_total if propellant_available else None,
        booster_dry_kg=dry_total if propellant_available else None,
        deferred=tuple(deferred),
    )
