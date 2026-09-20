"""0 级段派生（规格 §8.5「并联助推器的合并规则」/ OI-36 / OI-35 配对规则）。

覆盖：无助推器 → None；合成双发动机构型的 ``Isp_eff`` **手工对拍**（真空推力配
真空比冲，芯一级参与合并）；海平面/真空混用必须**显式报错**（绝不静默回落）；
推进剂/干重在参数层不可得时进 ``deferred``（禁止编造），可得时按两箱显式箱长
对拍（分段柱体近似，§5.9 的逆向使用）。
"""

from __future__ import annotations

import math

import pytest

from aeroforge.params import staging
from aeroforge.params.dag import G0
from aeroforge.params.schema import Booster, Engine, Vehicle


def _engine(thrust_vacuum_n: float, isp_vacuum_s: float) -> Engine:
    """造一台合法发动机（海平面值按 ṁ 与外推比冲给出，仅满足 Schema 必填）。"""
    flow = thrust_vacuum_n / (isp_vacuum_s * G0)
    return Engine(
        model=f"test-engine-{isp_vacuum_s:.0f}",
        cycle="gas_generator",
        chamber_pressure_pa=9.7e6,
        expansion_ratio=16.0,
        efficiency_factor=0.98,
        thrust_sea_level_n=flow * 280.0 * G0,
        thrust_vacuum_n=thrust_vacuum_n,
        isp_sea_level_s=280.0,
        isp_vacuum_s=isp_vacuum_s,
        mixture_ratio=2.36,
    )


def _boosted_vehicle(base: Vehicle, *, booster_count: int = 2) -> Vehicle:
    """合成双发动机构型：芯一级 2 台（F_vac=2 MN, Isp_vac=450 s），
    侧级 3 台（F_vac=1.5 MN, Isp_vac=300 s）× ``booster_count`` 枚。"""
    core_engine = _engine(2_000_000.0, 450.0)
    booster_engine = _engine(1_500_000.0, 300.0)
    core = base.stages[0].model_copy(update={"engine": core_engine, "engine_count": 2})
    side = base.stages[0].model_copy(
        update={
            "engine": booster_engine,
            "engine_count": 3,
            "diameter_m": 3.35,
            "length_m": 26.3,
        }
    )
    return base.model_copy(
        update={
            "stages": (core,),
            "boosters": [Booster(stage=side, count=booster_count, layout="radial_even")],
        }
    )


def test_no_boosters_returns_none(single_stage_vehicle: Vehicle) -> None:
    """无助推器 → 无 0 级段（§8.5 合并规则的前置分支）。"""
    assert staging.resolve_zero_stage(single_stage_vehicle) is None


def test_isp_eff_matches_hand_computed_vacuum_pairing(single_stage_vehicle: Vehicle) -> None:
    """合成双发动机构型：Isp_eff = ΣF_vac / Σṁ **手工对拍**（精确公式）。

    芯一级 2 台 × (2 MN, 450 s) + 侧级 2 枚 × 3 台 × (1.5 MN, 300 s)：
    ΣF_vac = 4e6 + 9e6 = 13e6 N；Σṁ = 4e6/(450·g₀) + 9e6/(300·g₀)。
    """
    vehicle = _boosted_vehicle(single_stage_vehicle)
    summary = staging.resolve_zero_stage(vehicle)
    assert summary is not None

    expected_thrust = 2 * 2_000_000.0 + 2 * 3 * 1_500_000.0
    expected_flow = 2 * 2_000_000.0 / (450.0 * G0) + 2 * 3 * 1_500_000.0 / (300.0 * G0)

    assert summary.count == 2
    assert summary.total_vacuum_thrust_n == pytest.approx(expected_thrust, rel=1e-12)
    assert summary.total_mass_flow_kg_s == pytest.approx(expected_flow, rel=1e-12)
    assert summary.isp_eff_s == pytest.approx(expected_thrust / expected_flow, rel=1e-12)
    # 配对正确性：真空口径结果必须**区别于**错误的海平面配对结果
    wrong_flow = 2 * 2_000_000.0 / (450.0 * G0) + 2 * 3 * 1_200_000.0 / (300.0 * G0)
    assert summary.total_mass_flow_kg_s != pytest.approx(wrong_flow, rel=1e-3)


def test_summary_declares_isp_eff_is_derived(single_stage_vehicle: Vehicle) -> None:
    """§8.5：Isp_eff 是派生量——summary 必须随声明下发，防止被当输入手填。"""
    vehicle = _boosted_vehicle(single_stage_vehicle)
    summary = staging.resolve_zero_stage(vehicle)
    assert summary is not None
    assert summary.note == staging.ISP_EFF_NOTE
    assert "禁止手工填写" in summary.note


def test_deferred_when_booster_tank_lengths_are_absent(
    single_stage_vehicle: Vehicle,
) -> None:
    """两箱无显式箱长 → 推进剂/干重为 None + deferred 说明（禁止编造，§1.4-4）。"""
    vehicle = _boosted_vehicle(single_stage_vehicle)
    summary = staging.resolve_zero_stage(vehicle)
    assert summary is not None
    assert summary.booster_propellant_kg is None
    assert summary.booster_dry_kg is None
    assert summary.deferred
    assert "tank.length_m" in summary.deferred[0]
    # 推力/流量派生不受影响
    assert summary.isp_eff_s > 0.0


def test_booster_masses_match_hand_computation(single_stage_vehicle: Vehicle) -> None:
    """两箱显式箱长 → 按分段柱体近似对拍（3 枚助推器，σ=0.05 派生干重）。"""
    vehicle = _boosted_vehicle(single_stage_vehicle, booster_count=3)
    side_tanks = vehicle.boosters[0].stage.geometry
    # make_stage 夹具：级直径 3.7 → 覆写侧级后两箱显式给长（直径默认继承级直径）
    side_tanks = side_tanks.model_copy(
        update={
            "oxidizer_tank": side_tanks.oxidizer_tank.model_copy(update={"length_m": 10.0}),
            "fuel_tank": side_tanks.fuel_tank.model_copy(update={"length_m": 6.0}),
        }
    )
    side = vehicle.boosters[0].stage.model_copy(update={"geometry": side_tanks})
    vehicle = vehicle.model_copy(
        update={"boosters": [Booster(stage=side, count=3, layout="radial_even")]}
    )

    summary = staging.resolve_zero_stage(vehicle)
    assert summary is not None
    assert summary.deferred == ()

    # 手工对拍：面积取级直径 3.35；LOX/RP-1 ρ_ox=1141、ρ_fuel=810；加注比例 0.95
    area = math.pi * 3.35**2 / 4.0
    per_booster = (area * 10.0 * 1141.0 + area * 6.0 * 810.0) * 0.95
    assert summary.booster_propellant_kg == pytest.approx(3 * per_booster, rel=1e-12)
    # 干重按该侧级 σ（夹具默认 0.05）独立派生：m_dry = m_prop · σ/(1−σ)
    assert summary.booster_dry_kg == pytest.approx(3 * per_booster * (0.05 / 0.95), rel=1e-12)


def test_sea_level_only_engine_is_rejected_not_silently_paired(
    single_stage_vehicle: Vehicle,
) -> None:
    """OI-35：真空值缺失（绕过校验构造）必须**显式报错**，绝不拿海平面值配真空。"""
    vehicle = _boosted_vehicle(single_stage_vehicle)
    broken_stage = vehicle.boosters[0].stage.model_copy(
        update={
            "engine": vehicle.boosters[0].stage.engine.model_copy(
                update={"thrust_vacuum_n": 0.0, "isp_vacuum_s": 0.0}
            )
        }
    )
    broken = vehicle.model_copy(
        update={
            "boosters": [
                vehicle.boosters[0].model_copy(update={"stage": broken_stage}),
            ]
        }
    )
    with pytest.raises(ValueError, match="真空"):
        staging.resolve_zero_stage(broken)


def test_boosters_without_stages_are_rejected(single_stage_vehicle: Vehicle) -> None:
    """0 级段必须挂在芯一级之下：绕过校验构造的空 stages → 显式拒绝（§8.5）。"""
    vehicle = _boosted_vehicle(single_stage_vehicle)
    orphaned = Vehicle.model_construct(
        name=vehicle.name,
        stages=(),
        boosters=vehicle.boosters,
        payload_mass_kg=vehicle.payload_mass_kg,
        material=vehicle.material,
        propellant=vehicle.propellant,
        mission=vehicle.mission,
    )
    with pytest.raises(ValueError, match="串联级"):
        staging.resolve_zero_stage(orphaned)
