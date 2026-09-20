"""ΔV 瀑布组装（OI-23 / 规格 §8.8 ``delta_v_budget``，M4 计算内核第三片）。

瀑布结构（§8.8 示例的**闭合方向**）
------------------------------------
::

    total_dv = ideal_dv + (重力 + 气动 + 转向 + 背压)四损失之和 − 自转加成

即「理想轨道速度增量 + 被吃掉的损失 − 地球白送的加成 = 火箭须提供的总 ΔV」
（§8.8 示例：7.81 + 1.24 + 0.18 + 0.22 + 0.15 − 0.39 = 9.21）。闭合纪律：
该等式按**同一表达式**构造并存入 ``total_dv_km_s``，测试以 1e-6 km/s 容差钉死
（§8.8 OI-23 约束：「ideal_dv 与各项之和必须与 total_dv 闭合」）。

⚠ 本函数的 ``total_dv`` 与 §8.6 需求表（运力反推的驱动量，量级锚定）是**两条
口径**：瀑布是「理论 ideal + L1 损失模型」的物理分解；需求表是含任务剖面经验
的锚定值。二者在 assumptions 与 :mod:`aeroforge.perf.capacity` 的 ``dv_source``
中各自标注来源，不得混用（单来源不得作为结论，§8.4 同族纪律）。

用户覆写（Mission.loss_factors）
--------------------------------
``loss_factors`` 非 None 时四项损失 = 份额 × 理想 ΔV（Schema 语义：缺省 0 即
「未计入」，不回落 L1 模型）；None 时全部取 L1 参数化经验模型（§8.6）。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from aeroforge.errors import PerfError
from aeroforge.params.dag import G0, propagate_vehicle
from aeroforge.params.schema import LaunchSite, Mission, Vehicle
from aeroforge.perf.capacity import vehicle_ledger
from aeroforge.perf.losses import (
    DEFAULT_DRAG_COEFFICIENT,
    aero_loss,
    back_pressure_loss,
    gravity_loss,
    ideal_orbit_dv_km_s,
    rotation_assist_km_s,
    steering_loss,
)

#: 闭合容差 [km/s]（§8.8 OI-23：ideal_dv 与各项之和必须与 total_dv 闭合）。
CLOSURE_TOLERANCE_KM_S = 1e-6


class DeltaVBudget(BaseModel):
    """ΔV 瀑布（OI-23 / §8.8 ``delta_v_budget``）：理想 ΔV → 各损失项 → 加成 → 总额。

    ``back_pressure_loss_km_s`` 即 §8.6 L1 表的「压力/控制余量」——**字段名唯一**
    （§8.8 警告：同一件事的两个名字，实现只保留一个字段，禁止两处各算一份）。
    """

    target_orbit: str = Field(description="目标轨道类型（Mission.orbit_type）")
    ideal_dv_km_s: float = Field(description="理想轨道速度增量（km/s，真空脉冲口径）")
    gravity_loss_km_s: float = Field(description="重力损失（km/s，L1 参数化经验模型）")
    aero_loss_km_s: float = Field(description="气动损失（km/s，L1 参数化经验模型）")
    steering_loss_km_s: float = Field(description="转向损失（km/s，L1 参数化经验模型）")
    back_pressure_loss_km_s: float = Field(
        description="背压损失（km/s）——即 §8.6「压力/控制余量」，字段名唯一（§8.8）"
    )
    rotation_assist_km_s: float = Field(description="自转加成（km/s，可为负——向西发射为逆向罚项）")
    total_dv_km_s: float = Field(description="总 ΔV 需求（km/s）= 理想 + 损失 − 加成")
    assumptions: tuple[str, ...] = Field(
        description="每项损失的模型假设与系数来源（必填——允许简化但必须标明假设）"
    )


def _first_stage_burn_time_s(vehicle: Vehicle, m_prop_first_kg: float) -> float:
    """一级燃时：显式 ``Stage.burn_time_s`` 优先，缺省按 m_prop/ṁ 派生（海平面口径）。"""
    first = sorted(vehicle.stages, key=lambda s: s.index)[0]
    if first.burn_time_s is not None:
        return first.burn_time_s
    engine = first.engine
    mass_flow = first.engine_count * engine.thrust_sea_level_n / (engine.isp_sea_level_s * G0)
    return m_prop_first_kg / mass_flow


def _user_losses(
    loss_factors_values: tuple[float, float, float, float], ideal: float
) -> tuple[float, float, float, float]:
    """用户覆写的四项损失（份额 × 理想 ΔV；0 项按 Schema 语义「未计入」）。"""
    gravity, drag, steering, back_pressure = loss_factors_values
    return gravity * ideal, drag * ideal, steering * ideal, back_pressure * ideal


def delta_v_budget(
    vehicle: Vehicle, site: LaunchSite, mission: Mission
) -> tuple[DeltaVBudget, tuple[str, ...]]:
    """组装 ΔV 瀑布（OI-23）；返回 ``(budget, warnings)``。

    输入组装口径：
    - TWR / 长径比 / 最大直径取 DAG 整箭派生量（``propagate_vehicle``，推进剂
      注入固定火箭的几何解析质量账）——与 §6.5 诊断同一来源，不另写一份；
    - ``Vehicle.aero`` 缺失时 Cd 按默认 0.3 并 warning（§6.1 口径），参考面积
      缺省取最大截面积（Schema ``Aero.reference_area_m2`` 的派生约定）；
    - 发射场纬度 / 海拔 / 方位角是自转加成与转向损失的唯一输入（§6.1）。
    """
    warnings: list[str] = []
    orbit = str(mission.orbit_type)
    ideal = ideal_orbit_dv_km_s(orbit, mission)

    ledger = vehicle_ledger(vehicle)
    dag = propagate_vehicle(vehicle, propellant_mass_kg=ledger.propellant_masses_by_index())
    twr = dag.values.get("vehicle.twr_liftoff")
    ld_ratio = dag.values.get("vehicle.length_to_diameter")
    max_diameter = dag.values.get("vehicle.max_diameter_m")
    if twr is None or ld_ratio is None or max_diameter is None:
        raise PerfError(
            "DAG 整箭派生量（TWR / 长径比 / 最大直径）不完整，无法组装 ΔV 瀑布",
            suggestion="检查各级参数齐备性（先跑 POST /api/params/diagnose）",
        )

    first = sorted(vehicle.stages, key=lambda s: s.index)[0]
    burn_time_s = _first_stage_burn_time_s(vehicle, ledger.stages[0].m_propellant_kg)

    if vehicle.aero is None:
        cd = DEFAULT_DRAG_COEFFICIENT
        warnings.append(
            "Aero 层缺失：气动损失按默认 Cd=0.3 计算（§6.1 口径，工程惯例值、"
            "非权威来源）——补齐 Vehicle.aero 可收窄该项"
        )
        a_ref = math.pi * max_diameter**2 / 4.0
    else:
        cd = vehicle.aero.drag_coefficient
        a_ref = vehicle.aero.reference_area_m2 or math.pi * max_diameter**2 / 4.0

    inclination = mission.inclination_deg
    assumptions: list[str] = [
        "理想脉冲近似：ideal_dv 为轨道力学理论速度增量（WGS-84 常数），"
        "不含有限推力损失（§8.6 L1 / §8.8）",
    ]

    factors = mission.loss_factors
    if factors is not None:
        gravity, aero, steering, back_pressure = _user_losses(
            (factors.gravity, factors.drag, factors.steering, factors.back_pressure), ideal
        )
        assumptions.append(
            "四项损失由 Mission.loss_factors 用户覆写（份额 × 理想 ΔV；"
            "0 值项按 Schema 语义「未计入」，§6.1）"
        )
    else:
        gravity_item = gravity_loss(twr, burn_time_s)
        aero_item = aero_loss(ld_ratio, cd, a_ref)
        steering_item = steering_loss(site.latitude_deg, site.azimuth_deg, inclination)
        back_pressure_item = back_pressure_loss(first.engine.expansion_ratio)
        gravity, aero, steering, back_pressure = (
            gravity_item.value_km_s,
            aero_item.value_km_s,
            steering_item.value_km_s,
            back_pressure_item.value_km_s,
        )
        for item in (gravity_item, aero_item, steering_item, back_pressure_item):
            assumptions.append(item.assumption)
            if item.warning is not None:
                warnings.append(item.warning)

    assist = rotation_assist_km_s(site.latitude_deg, site.altitude_m, site.azimuth_deg, inclination)
    assumptions.extend(
        (
            "自转加成：v_rot·cos(方位角)·相容因子，v_rot = ω·(R+海拔)·cos(纬度)"
            "（ω、R 取 WGS-84；相容因子与转向损失同一失配量驱动，§8.6 约束 2）",
            "闭合口径：total_dv = ideal_dv + 四项损失之和 − 自转加成"
            "（§8.8 示例方向；容差 1e-6 km/s 由测试钉死）",
            f"目标轨道 {orbit}：本瀑布为「理论 ideal + L1 损失模型」的物理分解；"
            "运力表（payload_by_orbit）另按 §8.6 需求表（量级锚定）取值——两条口径"
            "各自标注来源，不得混用",
        )
    )

    total = ideal + gravity + aero + steering + back_pressure - assist
    budget = DeltaVBudget(
        target_orbit=orbit,
        ideal_dv_km_s=ideal,
        gravity_loss_km_s=gravity,
        aero_loss_km_s=aero,
        steering_loss_km_s=steering,
        back_pressure_loss_km_s=back_pressure,
        rotation_assist_km_s=assist,
        total_dv_km_s=total,
        assumptions=tuple(assumptions),
    )
    return budget, tuple(warnings)


__all__ = [
    "CLOSURE_TOLERANCE_KM_S",
    "DeltaVBudget",
    "delta_v_budget",
]
