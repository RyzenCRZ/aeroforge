"""定尺求解（规格 §8.5 / §8.4 / §10.1）：质量层 + 迭代求解器 + 端点。

覆盖形态（任务口径逐条）
------------------------
- **闭式对拍**：单级 / 两级火箭给定 Isp/σ/载荷/ΔV 的 GLOW 手算闭式解 vs 求解器
  （1e-6 相对容差；实际达 ~1e-13）；
- **ΣΔV 闭合**（1e-6）+ 残差 / 迭代次数字段非空；
- **0 级段**（两助推器）：段末质量守恒（GLOW − 助推器推进剂 − 助推器干重）、
  芯一级跨段推进剂账闭合、Isp_eff 与 staging 对拍一致；
- **Lagrange 初值**：等 Isp 均分；不等 Isp 高比冲级分更多（定性）；
- **质量层**：柱 + 椭球封头容积 / 面积手算对拍（flatness_ratio 两档）、交叉校验
  >20% 出 warning、GCAT 回归真实库冒烟 + 样本不足合并路径（合成样本确定性覆盖）；
- **端点**：200 形状、非法输入 4xx、同步耗时 <500ms；
- **σ 越域警告**（不中止）与**不收敛报错带残差轨迹**（σ→1 极端）。
"""

from __future__ import annotations

import math
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.data.repository import CatalogRepository
from aeroforge.errors import SizingError
from aeroforge.params import staging
from aeroforge.params.dag import G0
from aeroforge.params.schema import Booster, Engine, Stage, Vehicle
from aeroforge.perf import mass as mass_module
from aeroforge.perf.mass import (
    MIN_BIN_SIZE,
    SigmaSample,
    classify_propellant,
    dome_height_m,
    dome_surface_area_m2,
    resolve_position,
    summarize_sigma_bins,
    tank_volume_m3,
)
from aeroforge.perf.solver import (
    CONVERGENCE_TOLERANCE,
    lagrange_initial_allocation,
    solve,
)
from tests.conftest import make_stage

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_DB = REPO_ROOT / "data" / "aeroforge.db"

_REL_TOL = 1e-6  # 任务口径的闭式对拍容差（实际收敛到 ~1e-13）


# ---------------------------------------------------------------------------
# 手算闭式解（与求解器**独立**推导：单级齐氏 + σ 账）
# ---------------------------------------------------------------------------


def _closed_form_stage(
    payload_kg: float, isp_vacuum_s: float, sigma: float, delta_v_m_s: float
) -> tuple[float, float]:
    """单级闭式：``λ = e^{ΔV/c}``，``m_prop = (λ−1)·P·(1−σ)/(1−λσ)``；返回 (m_prop, m_stage)。"""
    lam = math.exp(delta_v_m_s / (isp_vacuum_s * G0))
    m_prop = (lam - 1.0) * payload_kg * (1.0 - sigma) / (1.0 - lam * sigma)
    return m_prop, m_prop / (1.0 - sigma)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _booster_stage(length_m: float, *, explicit_tanks: bool) -> Stage:
    """造一枚助推器侧级（1 台 F_vac=1.4 MN / Isp_vac=300 s；级号 0）。"""
    engine = Engine(
        model="test-booster-engine",
        cycle="gas_generator",
        chamber_pressure_pa=9.7e6,
        expansion_ratio=16.0,
        efficiency_factor=0.98,
        thrust_sea_level_n=1_200_000.0,
        thrust_vacuum_n=1_400_000.0,
        isp_sea_level_s=280.0,
        isp_vacuum_s=300.0,
        mixture_ratio=2.36,
    )
    side = make_stage(1, length_m=length_m)
    side = side.model_copy(
        update={
            "engine": engine,
            "engine_count": 1,
            "diameter_m": 3.35,
            "index": 0,  # 侧级级号 0（GCAT 记法，§7.3 / OI-36）
        }
    )
    if explicit_tanks:
        geometry = side.geometry.model_copy(
            update={
                "oxidizer_tank": side.geometry.oxidizer_tank.model_copy(update={"length_m": 10.0}),
                "fuel_tank": side.geometry.fuel_tank.model_copy(update={"length_m": 6.0}),
            }
        )
        side = side.model_copy(update={"geometry": geometry})
    return side


# ---------------------------------------------------------------------------
# 闭式对拍（§8.5）
# ---------------------------------------------------------------------------


def test_single_stage_closed_form(single_stage_vehicle: Vehicle) -> None:
    """单级：手算 GLOW 闭式解 vs 求解器（1e-6 相对容差）。

    夹具：Isp_vac=311 s（DAG 解析自 engine）、σ=0.05、载荷 22.8 t、目标 9000 m/s。
    """
    payload = 22800.0
    target = 9000.0
    m_prop, m_stage = _closed_form_stage(payload, 311.0, 0.05, target)
    glow_closed = m_stage + payload

    result = solve(single_stage_vehicle, target)

    assert result.glow_kg == pytest.approx(glow_closed, rel=_REL_TOL)
    stage = result.stages[0]
    assert stage.m_propellant_kg == pytest.approx(m_prop, rel=_REL_TOL)
    assert stage.m_dry_kg == pytest.approx(m_prop * 0.05 / 0.95, rel=_REL_TOL)
    assert stage.m_total_kg == pytest.approx(m_stage, rel=_REL_TOL)
    assert stage.delta_v_m_s == pytest.approx(target, rel=_REL_TOL)
    # ΣΔV 闭合 + 报告字段
    assert result.achieved_delta_v_m_s == pytest.approx(target, rel=_REL_TOL)
    assert result.report.converged
    assert result.report.iterations >= 1
    assert result.report.residual_relative < CONVERGENCE_TOLERANCE
    assert result.report.residual_trail
    assert not result.report.hit_iteration_limit
    assert result.zero_stage is None
    # Isp 溯源（QA-1：default → engine）
    assert stage.isp_source == "engine"
    assert result.provenance["stage1.isp_vacuum_s"].startswith("engine")


def test_two_stage_closed_form_top_down(two_stage_vehicle: Vehicle) -> None:
    """两级：自上而下逐级手算（上面级总质量作下面级载荷）vs 求解器。"""
    payload = 22800.0
    target = 9000.0
    allocation = (5000.0, 4000.0)  # 一级 / 二级（两级 Isp 均 311 s、σ 均 0.05）

    m_prop2, m_stage2 = _closed_form_stage(payload, 311.0, 0.05, allocation[1])
    p1 = m_stage2 + payload  # 二级总质量 + 载荷 = 一级的上面 stacks
    m_prop1, m_stage1 = _closed_form_stage(p1, 311.0, 0.05, allocation[0])
    glow_closed = m_stage1 + p1

    result = solve(two_stage_vehicle, target, stage_delta_v_m_s=allocation)

    assert result.glow_kg == pytest.approx(glow_closed, rel=_REL_TOL)
    assert len(result.stages) == 2
    second, first = result.stages[1], result.stages[0]
    assert second.m_propellant_kg == pytest.approx(m_prop2, rel=_REL_TOL)
    assert second.m_total_kg == pytest.approx(m_stage2, rel=_REL_TOL)
    assert second.m_above_kg == pytest.approx(payload, rel=_REL_TOL)
    assert first.m_propellant_kg == pytest.approx(m_prop1, rel=_REL_TOL)
    assert first.m_above_kg == pytest.approx(p1, rel=_REL_TOL)
    # ΣΔV 闭合（1e-6）：逐级实算 ΔV 之和 == 目标
    assert sum(s.delta_v_m_s for s in result.stages) == pytest.approx(target, rel=_REL_TOL)
    assert result.achieved_delta_v_m_s == pytest.approx(target, rel=_REL_TOL)


def test_two_stage_lagrange_allocation_closes(two_stage_vehicle: Vehicle) -> None:
    """两级 + Lagrange 初值（无显式分配）：等 Isp → 均分，ΣΔV 仍闭合。"""
    result = solve(two_stage_vehicle, 9000.0)
    assert result.stages[0].delta_v_m_s == pytest.approx(4500.0, rel=_REL_TOL)
    assert result.stages[1].delta_v_m_s == pytest.approx(4500.0, rel=_REL_TOL)
    assert result.achieved_delta_v_m_s == pytest.approx(9000.0, rel=_REL_TOL)


# ---------------------------------------------------------------------------
# Lagrange 初值（§8.5：等 Isp 均分 / 不等 Isp 高比冲级分更多）
# ---------------------------------------------------------------------------


def test_lagrange_initial_allocation_equal_isp_splits_evenly() -> None:
    assert lagrange_initial_allocation([300.0, 300.0], 9000.0) == (4500.0, 4500.0)


def test_lagrange_initial_allocation_favors_higher_isp() -> None:
    """不等 Isp（定性验收）：高比冲级分到更多 ΔV，且总和守恒。"""
    allocation = lagrange_initial_allocation([300.0, 450.0], 9000.0)
    assert allocation[1] > allocation[0]
    assert sum(allocation) == pytest.approx(9000.0)
    # √Isp 权重的手算锚点：4500·√450/(√300+√450) 与 4500·√300/(√300+√450)
    w1, w2 = math.sqrt(300.0), math.sqrt(450.0)
    assert allocation[0] == pytest.approx(9000.0 * w1 / (w1 + w2), rel=1e-12)
    assert allocation[1] == pytest.approx(9000.0 * w2 / (w1 + w2), rel=1e-12)


# ---------------------------------------------------------------------------
# 0 级段（§8.5 合并规则 / OI-36）
# ---------------------------------------------------------------------------


def test_zero_stage_mass_conservation_and_cross_segment_account(
    single_stage_vehicle: Vehicle,
) -> None:
    """两助推器构型：段末守恒、芯一级跨段推进剂账闭合、Isp_eff 与 staging 对拍。"""
    side = _booster_stage(26.0, explicit_tanks=True)
    vehicle = single_stage_vehicle.model_copy(update={"boosters": [Booster(stage=side, count=2)]})
    target = 9000.0
    summary = staging.resolve_zero_stage(vehicle)
    assert summary is not None
    assert summary.booster_propellant_kg is not None  # 显式箱长 → staging 账可得

    result = solve(vehicle, target)
    zs = result.zero_stage
    assert zs is not None

    # 段初 / 段末质量守恒（§8.5：段末 = GLOW − 助推器推进剂 − 助推器干重）
    assert zs.m_start_kg == pytest.approx(result.glow_kg, rel=1e-12)
    assert zs.m_end_kg == pytest.approx(
        result.glow_kg - zs.booster_propellant_kg - zs.booster_dry_kg, rel=1e-12
    )
    # 助推器账与 staging 对拍（显式箱长 → 分段柱体近似同源）
    assert zs.booster_propellant_kg == pytest.approx(summary.booster_propellant_kg, rel=1e-12)
    assert zs.booster_dry_kg == pytest.approx(summary.booster_dry_kg, rel=1e-12)
    assert zs.propellant_source == "explicit_tank_lengths"
    # Isp_eff 与 staging 对拍一致
    assert zs.isp_eff_s == pytest.approx(summary.isp_eff_s, rel=1e-12)

    # 芯一级跨段推进剂账闭合：0 级段烧量 + 芯级段烧量 == 一级推进剂总量
    core_total = result.stages[0].m_propellant_kg
    assert 0.0 < zs.core_propellant_burned_kg < core_total  # 未封顶：两段各烧一部分
    assert zs.core_propellant_burned_kg + (core_total - zs.core_propellant_burned_kg) == (
        pytest.approx(core_total, rel=1e-12)
    )
    # 段内烧量 = 芯级流量 × 段时长（t₀ = 助推器推进剂 / 仅助推器流量）
    t0 = zs.booster_propellant_kg / summary.booster_mass_flow_kg_s
    core_flow = summary.total_mass_flow_kg_s - summary.booster_mass_flow_kg_s
    assert zs.core_propellant_burned_kg == pytest.approx(core_flow * t0, rel=1e-9)

    # 总账：GLOW = 一级总质量 + 上面 stacks + 助推器推进剂 + 助推器干重
    assert result.glow_kg - result.stages[0].m_total_kg - result.stages[0].m_above_kg - (
        zs.booster_propellant_kg + zs.booster_dry_kg
    ) == pytest.approx(0.0, abs=1e-6)

    # Σ 段 ΔV（0 级段 + 芯级段 + 上面级）== 目标
    total_dv = zs.delta_v_m_s + sum(s.delta_v_m_s for s in result.stages)
    assert total_dv == pytest.approx(target, rel=_REL_TOL)
    assert result.achieved_delta_v_m_s == pytest.approx(target, rel=_REL_TOL)


def test_zero_stage_geometric_fallback_when_tank_lengths_absent(
    single_stage_vehicle: Vehicle,
) -> None:
    """无显式箱长：助推器质量由 perf.mass 几何解析补齐（staging deferred 的 M4 兑现）。"""
    side = _booster_stage(26.0, explicit_tanks=False)
    vehicle = single_stage_vehicle.model_copy(update={"boosters": [Booster(stage=side, count=2)]})
    result = solve(vehicle, 9000.0)
    zs = result.zero_stage
    assert zs is not None
    assert zs.propellant_source == "geometric_estimate"
    per_side = mass_module.propellant_mass_kg(side)
    assert zs.booster_propellant_kg == pytest.approx(2 * per_side, rel=1e-12)
    # 干重仍按侧级 σ 派生（σ 是存储权威）
    assert zs.booster_dry_kg == pytest.approx(2 * per_side * 0.05 / 0.95, rel=1e-12)
    assert result.report.converged


def test_zero_stage_core_burn_cap_emits_warning(single_stage_vehicle: Vehicle) -> None:
    """跨段账封顶分支：助推器烧得比芯一级贮箱久 → 封顶 + 警告（不中止）。"""
    side = _booster_stage(26.0, explicit_tanks=False)  # 大几何贮箱（无显式箱长）
    vehicle = single_stage_vehicle.model_copy(update={"boosters": [Booster(stage=side, count=2)]})
    result = solve(vehicle, 8000.0)  # 低目标：解落在封顶区
    assert result.report.converged
    assert any("封顶" in w for w in result.warnings)
    zs = result.zero_stage
    assert zs is not None
    assert zs.core_propellant_burned_kg == pytest.approx(
        result.stages[0].m_propellant_kg, rel=1e-12
    )
    assert result.stages[0].delta_v_m_s == pytest.approx(0.0, abs=1e-9)  # 芯级段无余量可烧
    assert result.achieved_delta_v_m_s == pytest.approx(8000.0, rel=_REL_TOL)


# ---------------------------------------------------------------------------
# σ 越域警告 + 不收敛（§8.5 报告项）
# ---------------------------------------------------------------------------


def test_sigma_out_of_domain_warns_but_solves(single_stage_vehicle: Vehicle) -> None:
    """σ=0.2 越出一级液体 [0.04,0.08]：警告（不中止），求解照常收敛。"""
    vehicle = single_stage_vehicle.model_copy(
        update={
            "stages": (
                single_stage_vehicle.stages[0].model_copy(update={"structure_coefficient": 0.2}),
            )
        }
    )
    result = solve(vehicle, 4000.0)
    assert result.report.converged
    assert result.report.sigma_domain_warnings
    assert any("越出 §8.4 有效域" in w for w in result.warnings)
    # 交叉校验与 σ 越域是两类独立警告，都在 warnings 汇总里
    assert result.achieved_delta_v_m_s == pytest.approx(4000.0, rel=_REL_TOL)


def test_non_convergence_raises_with_residual_trail(single_stage_vehicle: Vehicle) -> None:
    """σ→1 极端（0.9999）：单级可达上限 Isp·g₀·ln(1/σ) ≈ 0.3 m/s << 9000 → 不收敛，
    必须报错并带最后几次残差轨迹（不静默给半收敛结果）。"""
    vehicle = single_stage_vehicle.model_copy(
        update={
            "stages": (
                single_stage_vehicle.stages[0].model_copy(update={"structure_coefficient": 0.9999}),
            )
        }
    )
    with pytest.raises(SizingError) as excinfo:
        solve(vehicle, 9000.0)
    assert excinfo.value.code == "SIZING_NO_CONVERGENCE"
    trail = excinfo.value.details["residual_trail"]
    assert isinstance(trail, list) and trail
    assert excinfo.value.details["iterations"] >= 1
    assert "残差轨迹" in excinfo.value.message


def test_infeasible_upper_stage_allocation_rejected(two_stage_vehicle: Vehicle) -> None:
    """上面级分配超上限（λσ ≥ 1 无物理解）：立即拒绝并给出可达上限。"""
    # 二级 Isp=311 s、σ=0.6 → 上限 = 311·g₀·ln(1/0.6) ≈ 1558 m/s；给二级分配 3000 m/s
    heavy_upper = two_stage_vehicle.model_copy(
        update={
            "stages": (
                two_stage_vehicle.stages[0],
                two_stage_vehicle.stages[1].model_copy(update={"structure_coefficient": 0.6}),
            )
        }
    )
    with pytest.raises(SizingError, match="可达上限"):
        solve(heavy_upper, 9000.0, stage_delta_v_m_s=(6000.0, 3000.0))


def test_allocation_sum_mismatch_rejected(two_stage_vehicle: Vehicle) -> None:
    with pytest.raises(SizingError, match="不一致"):
        solve(two_stage_vehicle, 9000.0, stage_delta_v_m_s=(5000.0, 3000.0))


# ---------------------------------------------------------------------------
# 质量层：几何解析手算对拍（§8.4）
# ---------------------------------------------------------------------------


def test_dome_geometry_hand_computed_two_flatness_values() -> None:
    """封头矢高 / 容积手算对拍：f=0.5（2:1 封头，h=d/4）与 f=0.25 两档。"""
    # 矢高：h = f·d/2（f=0.5、d=4 → h=1；f=0.25 → h=0.5；None 兜底 0.5）
    assert dome_height_m(4.0, 0.5) == pytest.approx(1.0)
    assert dome_height_m(4.0, 0.25) == pytest.approx(0.5)
    assert dome_height_m(4.0, None) == pytest.approx(1.0)
    assert dome_height_m(4.0, 1.0) == pytest.approx(2.0)  # 半球退化

    # 半球封头表面积精确解：2π(d/2)²（Knud Thomsen 在 a=b 时严格成立）
    assert dome_surface_area_m2(4.0, 2.0) == pytest.approx(2.0 * math.pi * 4.0, rel=1e-12)

    # 容积 = 柱段 + 2·(2/3)π(d/2)²h（闭式精确，两档手算）
    for flatness in (0.5, 0.25):
        h = flatness * 4.0 / 2.0
        expected = math.pi * 4.0**2 / 4.0 * 10.0 + 2.0 * (2.0 / 3.0) * math.pi * 4.0 * h
        assert tank_volume_m3(4.0, 10.0, h) == pytest.approx(expected, rel=1e-12)


def test_propellant_mass_with_explicit_tank_lengths(single_stage_vehicle: Vehicle) -> None:
    """显式箱长（用户权威）：推进剂质量 = Σ(柱段容积 × 本剂密度) × 加注比例。"""
    side = _booster_stage(26.0, explicit_tanks=True)
    area = math.pi * 3.35**2 / 4.0
    h = 0.5 * 3.35 / 2.0  # 扁度缺省 0.5 → 封头矢高
    dome_v = (2.0 / 3.0) * math.pi * (3.35 / 2.0) ** 2 * h
    v_ox = area * 10.0 + 2.0 * dome_v
    v_fuel = area * 6.0 + 2.0 * dome_v
    expected = (v_ox * 1141.0 + v_fuel * 810.0) * side.fill_fraction

    assert mass_module.propellant_mass_kg(side) == pytest.approx(expected, rel=1e-12)

    ox, fuel = mass_module.resolve_tank_geometry(side)
    assert ox.length_source == "user" and fuel.length_source == "user"
    assert ox.cylinder_length_m == pytest.approx(10.0)
    assert fuel.cylinder_length_m == pytest.approx(6.0)


def test_tank_length_split_by_volumetric_ratio(single_stage_vehicle: Vehicle) -> None:
    """无显式箱长：按 §5.9 容积比 V_ox/V_fuel=(O/F)·(ρ_fuel/ρ_ox) 分配可用长度。"""
    stage = single_stage_vehicle.stages[0]
    available = stage.length_m - stage.engine_height_m
    ratio = 2.36 * 810.0 / 1141.0  # LOX/RP-1 @ O/F=2.36
    expected_fuel = available / (1.0 + ratio)
    expected_ox = available - expected_fuel

    ox, fuel = mass_module.resolve_tank_geometry(stage)
    assert ox.length_source == "derived" and fuel.length_source == "derived"
    assert ox.cylinder_length_m == pytest.approx(expected_ox, rel=1e-12)
    assert fuel.cylinder_length_m == pytest.approx(expected_fuel, rel=1e-12)


def test_cross_check_threshold_warning(single_stage_vehicle: Vehicle) -> None:
    """交叉校验：默认夹具（细长级）几何干重 vs σ 推算干重偏差远超 20% → warning。"""
    stage = single_stage_vehicle.stages[0]
    outcome = mass_module.cross_check(stage)
    assert outcome.exceeds_threshold
    assert outcome.warning is not None
    assert outcome.relative_deviation > 0.2

    result = solve(single_stage_vehicle, 9000.0)
    assert any("交叉校验" in w for w in result.warnings)
    assert result.stages[0].cross_check.exceeds_threshold

    # 反例：σ 与几何口径一致（σ≈0.0068 → σ 推算干重 ≈ 几何干重）→ 无交叉校验警告
    consistent = single_stage_vehicle.model_copy(
        update={
            "stages": (
                single_stage_vehicle.stages[0].model_copy(update={"structure_coefficient": 0.0068}),
            )
        }
    )
    outcome2 = mass_module.cross_check(consistent.stages[0])
    assert not outcome2.exceeds_threshold
    assert outcome2.warning is None


# ---------------------------------------------------------------------------
# 统计回归（§8.4：合成确定性覆盖 + 真实库冒烟）
# ---------------------------------------------------------------------------


def test_summarize_bins_merges_small_bins_into_nearest_same_class() -> None:
    """样本 <5 的箱并入**同级**（同推进剂大类）最近大箱，合并必须留痕。"""
    samples = [
        # (first, liquid) 6 个大箱样本（σ 在一级液体域内）
        *[SigmaSample(f"s{i}", "first", "liquid", 0.06) for i in range(6)],
        # (booster, liquid) 2 个小箱样本 → 应并入 (first, liquid)
        SigmaSample("b0", "booster", "liquid", 0.05),
        SigmaSample("b1", "booster", "liquid", 0.07),
    ]
    summary = summarize_sigma_bins(samples)
    keys = {(b.position, b.propellant_class) for b in summary.bins}
    assert ("booster", "liquid") not in keys
    merged = next(
        b for b in summary.bins if (b.position, b.propellant_class) == ("first", "liquid")
    )
    assert merged.sample_count == 8
    assert any("并入同级最近箱" in note for note in summary.merge_notes)


def test_summarize_bins_pooled_fallback_without_same_class_big_bin() -> None:
    """同级无大箱 → 并入混合兜底箱（pooled/mixed）。"""
    samples = [
        SigmaSample("u0", "upper", "unknown", 0.2),
        SigmaSample("u1", "upper", "unknown", 0.3),
    ]
    summary = summarize_sigma_bins(samples)
    pooled = [b for b in summary.bins if b.position == "pooled"]
    assert len(pooled) == 1
    assert pooled[0].propellant_class == "mixed"
    assert pooled[0].sample_count == 2
    assert pooled[0].interval is None  # 兜底箱无规格区间，只报告不判定
    assert any("混合兜底箱" in note for note in summary.merge_notes)


def test_summarize_bins_out_of_interval_median_warns() -> None:
    """箱中位数越出 §8.4 有效域（一级液体 [0.04,0.08]）→ 显式警告，不静默外推。"""
    samples = [SigmaSample(f"s{i}", "first", "liquid", 0.2) for i in range(6)]
    summary = summarize_sigma_bins(samples)
    assert summary.bins[0].out_of_interval
    assert summary.warnings
    assert "越出 §8.4 有效域" in summary.warnings[0]


def test_resolve_position_majority_vote() -> None:
    from collections import Counter

    assert resolve_position(Counter({"first": 3, "upper": 1})) == "first"
    assert resolve_position(Counter({"upper": 2, "booster": 5})) == "booster"
    assert resolve_position(Counter()) == "unknown"
    assert resolve_position(Counter({"first": 2, "booster": 2})) == "first"  # 并列取一级优先


def test_classify_propellant_by_oxidizer_presence() -> None:
    assert classify_propellant(None) == "unknown"


@pytest.mark.skipif(not REAL_DB.is_file(), reason=f"真实目录库不存在：{REAL_DB}")
def test_gcat_regression_smoke_on_real_db(tmp_path: Path) -> None:
    """真实库回归冒烟：样本数如实 >0，每箱计数 ≥ 合并下限（不足者已并入）。

    隔离纪律（§13.8 / test_gcat_db 同口径）：把仓库 ``data/aeroforge.db``
    **复制**进 pytest 临时区再开库——绝不触碰开发机库本体。
    """
    dst = tmp_path / "aeroforge.db"
    shutil.copy(REAL_DB, dst)
    with CatalogRepository(dst) as repository:
        report = mass_module.regress_structure_coefficients(repository)

    assert report.total_stage_rows > 0
    assert report.dual_non_missing_rows > 0
    assert 0 < report.valid_rows <= report.dual_non_missing_rows <= report.total_stage_rows
    assert report.total_samples == report.valid_rows
    assert report.bins, "真实库回归必须产出至少一个箱"
    for b in report.bins:
        assert b.sample_count >= MIN_BIN_SIZE or b.position == "pooled"
    # 一级液体箱必须存在且中位数落在 §8.4 域内（真实库锚点，防全库性退化）
    first_liquid = next(
        (b for b in report.bins if (b.position, b.propellant_class) == ("first", "liquid")),
        None,
    )
    assert first_liquid is not None
    assert first_liquid.sample_count > 50
    assert 0.04 <= first_liquid.sigma_median <= 0.08


# ---------------------------------------------------------------------------
# 端点（§10.1：同步、纯数值）
# ---------------------------------------------------------------------------


def _solve_body(vehicle: Vehicle, target: float = 9000.0) -> dict[str, object]:
    return {
        "vehicle": vehicle.model_dump(mode="json"),
        "target_delta_v_m_s": target,
    }


def test_sizing_solve_endpoint_returns_full_shape(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """200 形状：GLOW / 逐级 / 报告字段 / 0 级段 null / 溯源账目。"""
    response = client.post("/api/sizing/solve", json=_solve_body(two_stage_vehicle))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "glow_kg",
        "payload_mass_kg",
        "target_delta_v_m_s",
        "achieved_delta_v_m_s",
        "stages",
        "zero_stage",
        "report",
        "warnings",
        "provenance",
    }
    assert body["glow_kg"] > 0.0
    assert len(body["stages"]) == 2
    for stage in body["stages"]:
        assert {"m_propellant_kg", "m_dry_kg", "m_total_kg", "delta_v_m_s", "cross_check"} <= set(
            stage
        )
    assert body["zero_stage"] is None
    report = body["report"]
    assert {
        "iterations",
        "residual_relative",
        "converged",
        "hit_iteration_limit",
        "residual_trail",
        "sigma_domain_warnings",
    } <= set(report)
    assert report["converged"] is True
    assert report["iterations"] >= 1
    assert body["achieved_delta_v_m_s"] == pytest.approx(9000.0, rel=_REL_TOL)
    assert "stage1.isp_vacuum_s" in body["provenance"]


def test_sizing_solve_endpoint_latency(client: TestClient, single_stage_vehicle: Vehicle) -> None:
    """同步耗时（纯数值，单次请求 < 500 ms）。"""
    started = time.perf_counter()
    response = client.post("/api/sizing/solve", json=_solve_body(single_stage_vehicle))
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert response.status_code == 200
    assert elapsed_ms < 500.0, f"定尺求解耗时 {elapsed_ms:.1f} ms 超出 500 ms 预算"


def test_sizing_solve_endpoint_rejects_sigma_out_of_schema(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """σ ≤ 0：Schema 层拒绝（REQUEST_INVALID → 422，既有错误体系）。"""
    broken = single_stage_vehicle.model_copy(
        update={
            "stages": (
                single_stage_vehicle.stages[0].model_copy(update={"structure_coefficient": 0.0}),
            )
        }
    )
    # frozen 模型的 model_copy 不复验——构造原始 dict 走 pydantic 校验路径
    payload = broken.model_dump(mode="json")
    payload["stages"][0]["structure_coefficient"] = -0.1
    response = client.post(
        "/api/sizing/solve", json={"vehicle": payload, "target_delta_v_m_s": 9000.0}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_INVALID"


def test_sizing_solve_endpoint_rejects_custom_isp_without_value(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """isp_source=custom 但缺值：硬约束拒绝（PARAMS_CONSTRAINT_VIOLATION → 422）。"""
    broken = single_stage_vehicle.model_copy(
        update={
            "stages": (single_stage_vehicle.stages[0].model_copy(update={"isp_source": "custom"}),)
        }
    )
    response = client.post("/api/sizing/solve", json=_solve_body(broken))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PARAMS_CONSTRAINT_VIOLATION"


def test_sizing_solve_endpoint_reports_non_convergence(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """不收敛（σ→1 极端）：422 + SIZING_NO_CONVERGENCE + details 带残差轨迹。"""
    broken = single_stage_vehicle.model_copy(
        update={
            "stages": (
                single_stage_vehicle.stages[0].model_copy(update={"structure_coefficient": 0.9999}),
            )
        }
    )
    response = client.post("/api/sizing/solve", json=_solve_body(broken))
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "SIZING_NO_CONVERGENCE"
    assert error["details"]["residual_trail"]


def test_sizing_solve_endpoint_rejects_bad_target(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    response = client.post("/api/sizing/solve", json=_solve_body(single_stage_vehicle, target=-1.0))
    assert response.status_code == 422
    response = client.post(
        "/api/sizing/solve",
        json={
            **_solve_body(single_stage_vehicle),
            "stage_delta_v_m_s": [5000.0, 3000.0],  # 两级箭给两项但和 ≠ 目标
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SIZING_SOLVE_FAILED"
