"""L2 简化上升弹道积分测试（规格 §8.6 L2，M6 轨道层第二片）。

覆盖形态（任务口径逐条）
------------------------
- **闭合**（增广积分恒等式）：``ideal_dv = dv_gained + Σ四损失``——RK4 增广
  状态同步积分的自检量，残差 < 1e-6 m/s（数值积分精度，非 §8.8 的 1e-6 km/s
  瀑布闭合——两者不同层次的量）；
- **L1 vs L2 对照（F9 / CZ-5 / SV 三构型）**：四项损失各自与 L1（§8.6 经验
  模型，:func:`delta_v_budget`）输出对照。带宽依据如实报告：L1 是经验区间
  模型、L2 是简化物理，不要求吻合只要求不脱谱——
  * 重力 / 背压两项比值 ∈ [0.5, 2.0]（带内断言）；
  * 气动：L2 系统性低于 L1（L1 的 √(A_ref/A_F9) 标度对大截面 / 重型构型高估——
    SV 的 L1 气动 0.45 km/s 被夹到上限，而公开物理估计 ≈0.05 km/s 量级、L2
    常数 Cd=0.3 积分给 0.02–0.04）——断言方向 ``0 < L2 < L1`` 与量级域；
  * 转向：口径不同——L1 是方位角/倾角失配的**面外经验罚项**（三基准模板均
    向东 + 自然倾角发射，L1 全部取 0.1 下限），L2 是程序转弯的**面内攻角
    积分**（推力未对准速度的分量赤字，固定简单剖面未优化，量级 0.3–0.7）。
    带宽对单项不适用，改为「重力+转向」合计比值 ∈ [0.5, 2.0] 的**不脱谱锚**
    （同一"程序质量亏损"总账在两层模型间对照）；
- **总损失方向一致**：``ideal(LEO) + ΣL2损失 − 自转加成`` 与 §8.6 锚定总 ΔV
  （9.3–9.5 LEO 含损失）方向一致（∈ [8.9, 10.5]，实际值随断言消息输出）；
- **趋势机检（FR-19 同型）**：TWR ↑ ⟹ 重力损失 ↓（同构型改推力采样）；
  Cd ↑ ⟹ 气动损失 ↑——显式断言，禁止只在文档声称；
- **ISA 1976 大气**（§3.1 强制口径）：海平面值、层边界连续、密度单调递减；
- **报错路径**：程序参数荒谬 / TWR ≤ 0 / 触地（报错带最后状态）/ 超第二宇宙
  速度——全部 :class:`PerfError`；
- **端点**（POST /api/perf/trajectory，异步）：受理回执 → GET /api/jobs/{id}
  轮询终态，metrics 契约（losses_km_s / burnout / provenance / stage_timeline）
  + 程序覆盖生效 + 程序参数荒谬 → 作业 FAILED（TRAJECTORY_FAILED）+ 值域 422。
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from itertools import pairwise
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.errors import PerfError
from aeroforge.params.schema import Aero, Engine, Vehicle
from aeroforge.params.templates import cz5_vehicle, falcon9_vehicle, saturnv_vehicle
from aeroforge.perf.budget import delta_v_budget
from aeroforge.perf.capacity import vehicle_ledger
from aeroforge.perf.evaluate import resolve_site
from aeroforge.perf.losses import ideal_orbit_dv_km_s
from aeroforge.perf.trajectory import (
    ISA_DENSITY_SEA_LEVEL_KG_M3,
    ISA_PRESSURE_SEA_LEVEL_PA,
    TrajectoryProgram,
    integrate_ascent,
    isa_density_pressure,
)

#: 闭合残差容差 [m/s]（RK4 增广积分的机器精度量级，实测 ~1e-11）。
_CLOSURE_TOL_M_S = 1e-6

#: L1↔L2 同量级对照带（任务口径：不要求吻合只要求不脱谱）。
_BAND_LOW, _BAND_HIGH = 0.5, 2.0

#: 总 ΔV 方向一致性带 [km/s]（§8.6 锚定 9.3–9.5 的工程宽容域，实际值随消息输出）。
_TOTAL_DV_WINDOW = (8.9, 10.5)

#: 端点作业轮询上限（L2 单发积分百毫秒级，留足 CI 冷启动余量）。
_JOB_TIMEOUT_S = 60.0


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _await_job(client: TestClient, job_id: str) -> dict[str, Any]:
    """轮询作业直到终态（§10.2 降级路径；与 test_mc_and_sequence 同型）。"""
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"作业 {job_id} 在 {_JOB_TIMEOUT_S}s 内未达终态")


# ---------------------------------------------------------------------------
# ISA 1976 分层指数大气（§3.1 强制口径）
# ---------------------------------------------------------------------------


def test_isa_sea_level_and_layer_continuity() -> None:
    """海平面值 = ISA 1976 表值；层边界两侧连续；密度随高度单调递减。"""
    rho0, p0 = isa_density_pressure(0.0)
    # ρ₀ 由 p₀/(R·T₀) 递推——与表值 1.225（ISA 原文舍入值）一致到舍入精度
    assert rho0 == pytest.approx(ISA_DENSITY_SEA_LEVEL_KG_M3, rel=1e-7)
    assert p0 == pytest.approx(ISA_PRESSURE_SEA_LEVEL_PA, rel=1e-12)

    # 层边界（11 / 32 / 51 km）两侧采样连续（层底值由 ISA 精确式递推，两端衔接）
    for boundary in (11_000.0, 32_000.0, 51_000.0):
        rho_below, p_below = isa_density_pressure(boundary - 1.0)
        rho_above, p_above = isa_density_pressure(boundary + 1.0)
        assert rho_above == pytest.approx(rho_below, rel=2e-3)
        assert p_above == pytest.approx(p_below, rel=2e-3)

    # 单调递减（0–86 km 采样步 1 km）
    densities = [isa_density_pressure(float(h))[0] for h in range(0, 86_000, 1_000)]
    assert all(a > b for a, b in pairwise(densities))


# ---------------------------------------------------------------------------
# 闭合与燃尽状态（F9 冒烟 + 恒等式）
# ---------------------------------------------------------------------------


def test_f9_closure_and_burnout_sanity() -> None:
    """增广积分恒等式闭合 + F9 燃尽状态落在 LEO 量级域（损失分解的载体可信）。"""
    result = integrate_ascent(falcon9_vehicle())
    assert abs(result.closure_residual_m_s) < _CLOSURE_TOL_M_S

    losses = result.losses_km_s
    assert losses.total_km_s == pytest.approx(
        losses.gravity_km_s + losses.aero_km_s + losses.steering_km_s + losses.back_pressure_km_s,
        abs=1e-9,
    )
    # 燃尽状态量级（固定程序剖面的 LEO 量级上升，非入轨求解——报告口径）
    assert 80_000.0 < result.burnout.altitude_m < 400_000.0
    assert 6_500.0 < result.burnout.velocity_m_s < 8_500.0
    assert abs(result.burnout.flight_path_angle_deg) < 8.0
    # 燃尽质量 = 载荷 + 末级干重（质量账派生值——模板公开数 4.0 t 为舍入锚）
    ledger = vehicle_ledger(falcon9_vehicle())
    expected_burnout_mass = falcon9_vehicle().payload_mass_kg + ledger.stages[-1].m_dry_kg
    assert result.burnout.mass_kg == pytest.approx(expected_burnout_mass, rel=1e-9)
    # 自转加成信用（卡角 28.56°、向东）：面内分量 ω(R+h)cosφ·sinA_z ≈ 409 m/s
    assert result.rotation_assist_m_s == pytest.approx(409.0, rel=0.02)
    # 级段时间线：两段（F9 无助推器），段末抛离 = 各级干重（末段 0）
    assert [seg.segment for seg in result.stage_timeline] == ["stage_1", "stage_2"]
    assert result.stage_timeline[0].jettison_kg > 0.0
    assert result.stage_timeline[-1].jettison_kg == 0.0


# ---------------------------------------------------------------------------
# L1 vs L2 对照（F9 / CZ-5 / SV 三构型——任务核心口径，带宽依据见模块 docstring）
# ---------------------------------------------------------------------------


def _benchmark_vehicles() -> dict[str, Vehicle]:
    return {"F9": falcon9_vehicle(), "CZ-5": cz5_vehicle(), "SV": saturnv_vehicle()}


#: 程序参数（可调自由度）——F9 / CZ-5 用缺省剖面；SV 的 S-IVB TWR≈0.41 无法
#: 跟踪缺省剖面的末段拉平（触地发散——程序-构型失配是物理事实，报错口径），
#: 按构型放缓标高（真实工程的上升程序本就逐构型调，§8.6「程序参数=可调自由度」）。
_BENCHMARK_PROGRAMS: dict[str, TrajectoryProgram] = {
    "F9": TrajectoryProgram(),
    "CZ-5": TrajectoryProgram(),
    "SV": TrajectoryProgram(scale_height_m=80_000.0),
}


@pytest.mark.parametrize("name", ["F9", "CZ-5", "SV"])
def test_l1_vs_l2_loss_comparison(name: str) -> None:
    """三构型四项损失 L1↔L2 对照：带内项 + 口径差异项的如实断言（见模块 docstring）。"""
    vehicle = _benchmark_vehicles()[name]
    site = resolve_site(vehicle, [])
    budget, _ = delta_v_budget(vehicle, site, vehicle.mission)
    result = integrate_ascent(vehicle, _BENCHMARK_PROGRAMS[name])
    l2 = result.losses_km_s

    # 重力：比值带内（同量级）
    r_gravity = l2.gravity_km_s / budget.gravity_loss_km_s
    assert _BAND_LOW <= r_gravity <= _BAND_HIGH, (
        f"{name} 重力损失比值 {r_gravity:.2f} 脱带（L2={l2.gravity_km_s:.3f} vs "
        f"L1={budget.gravity_loss_km_s:.3f}）"
    )
    # 背压：比值带内（同源真空账本口径）
    r_bp = l2.back_pressure_km_s / budget.back_pressure_loss_km_s
    assert _BAND_LOW <= r_bp <= _BAND_HIGH

    # 气动：L2 系统性低于 L1（L1 √A_ref 标度高估大截面构型——方向断言 + 小项量级域）
    assert 0.0 < l2.aero_km_s < budget.aero_loss_km_s
    assert l2.aero_km_s < 0.15

    # 转向：口径不同（L1 面外经验罚项 / L2 面内攻角积分）——单项不设带，
    # 「重力+转向」合计（同一"程序质量亏损"总账）比值 ∈ 带内 = 不脱谱锚
    assert 0.0 < l2.steering_km_s < 1.0
    r_combined = (l2.gravity_km_s + l2.steering_km_s) / (
        budget.gravity_loss_km_s + budget.steering_loss_km_s
    )
    assert _BAND_LOW <= r_combined <= _BAND_HIGH, (
        f"{name} 重力+转向合计比值 {r_combined:.2f} 脱带（L2 合计="
        f"{l2.gravity_km_s + l2.steering_km_s:.3f} vs L1 合计="
        f"{budget.gravity_loss_km_s + budget.steering_loss_km_s:.3f}）"
    )

    # 总损失方向一致：ideal(LEO) + ΣL2 − 自转加成 与 §8.6 锚定 9.3–9.5 方向一致
    total = (
        ideal_orbit_dv_km_s(vehicle.mission.orbit_type, vehicle.mission)
        + l2.total_km_s
        - budget.rotation_assist_km_s
    )
    assert _TOTAL_DV_WINDOW[0] <= total <= _TOTAL_DV_WINDOW[1], (
        f"{name} 总 ΔV（L2 损失口径）{total:.2f} km/s 偏离 §8.6 锚定 9.3–9.5 方向"
    )


# ---------------------------------------------------------------------------
# 趋势机检（FR-19 同型：显式断言，禁止只在文档声称）
# ---------------------------------------------------------------------------


def _scale_thrust(vehicle: Vehicle, factor: float) -> Vehicle:
    """全发动机推力 ×factor（海平面/真空同比——A_e 反推口径不变，TWR 随之变化）。"""

    def _scale_engine(engine: Engine) -> Engine:
        return engine.model_copy(
            update={
                "thrust_vacuum_n": engine.thrust_vacuum_n * factor,
                "thrust_sea_level_n": engine.thrust_sea_level_n * factor,
            }
        )

    stages = tuple(
        stage.model_copy(update={"engine": _scale_engine(stage.engine)}) for stage in vehicle.stages
    )
    boosters = [
        booster.model_copy(
            update={
                "stage": booster.stage.model_copy(
                    update={"engine": _scale_engine(booster.stage.engine)}
                )
            }
        )
        for booster in vehicle.boosters
    ]
    return vehicle.model_copy(update={"stages": stages, "boosters": boosters})


def test_trend_twr_up_reduces_gravity_loss() -> None:
    """趋势一：TWR ↑ ⟹ 重力损失 ↓（同构型改 TWR 采样——v(h) 更高、爬升更省时）。"""
    base = integrate_ascent(falcon9_vehicle())
    boosted = integrate_ascent(_scale_thrust(falcon9_vehicle(), 1.12))
    assert boosted.liftoff_twr > base.liftoff_twr
    assert boosted.losses_km_s.gravity_km_s < base.losses_km_s.gravity_km_s


def test_trend_cd_up_increases_aero_loss() -> None:
    """趋势二：Cd ↑ ⟹ 气动损失 ↑（其余全部固定——§6.1 Aero 层口径）。"""
    base = integrate_ascent(falcon9_vehicle())
    draggier = integrate_ascent(
        falcon9_vehicle().model_copy(
            update={"aero": Aero(drag_coefficient=0.6, reference_area_m2=None)}
        )
    )
    assert draggier.losses_km_s.aero_km_s > base.losses_km_s.aero_km_s


# ---------------------------------------------------------------------------
# 报错路径（§8.6：输入验证 + 积分发散报错带最后状态）
# ---------------------------------------------------------------------------


def test_program_absurd_rejected() -> None:
    """程序参数荒谬域（垂直段过长 / 标高越界 / 目标倾角荒谬 / 步长过粗）→ 报错。"""
    vehicle = falcon9_vehicle()
    cases = [
        (TrajectoryProgram(vertical_rise_s=100.0), 0.1),
        (TrajectoryProgram(scale_height_m=2_000_000.0), 0.1),
        (TrajectoryProgram(final_pitch_deg=90.0), 0.1),
        (TrajectoryProgram(), 5.0),
    ]
    for program, dt in cases:
        with pytest.raises(PerfError):
            integrate_ascent(vehicle, program, dt_s=dt)


def test_zero_twr_rejected() -> None:
    """TWR ≤ 0（绕过 Schema 校验构造的零推力发动机）→ 入口报错。"""
    vehicle = falcon9_vehicle()
    dead_engine = vehicle.stages[0].engine.model_copy(update={"thrust_sea_level_n": 0.0})
    stages = [
        vehicle.stages[0].model_copy(update={"engine": dead_engine}),
        vehicle.stages[1],
    ]
    with pytest.raises(PerfError, match="推重比"):
        integrate_ascent(vehicle.model_copy(update={"stages": tuple(stages)}))


def test_ground_impact_reports_last_state() -> None:
    """TWR < 1 ⟹ 离台即坠——报错携带最后状态（t / h / v / γ / m 全量）。"""
    vehicle = _scale_thrust(falcon9_vehicle(), 0.70)  # TWR ≈ 0.95，托不住自重
    with pytest.raises(PerfError, match="最后状态") as excinfo:
        integrate_ascent(vehicle)
    assert "触地" in str(excinfo.value)


def test_escape_velocity_rejected() -> None:
    """比冲被异常放大 ⟹ 速度超第二宇宙速度——报错（发散防护口径）。"""
    vehicle = falcon9_vehicle()
    stages = tuple(
        stage.model_copy(
            update={
                "engine": stage.engine.model_copy(
                    update={"isp_vacuum_s": 2_000.0, "isp_sea_level_s": 2_000.0}
                )
            }
        )
        for stage in vehicle.stages
    )
    with pytest.raises(PerfError, match="第二宇宙速度"):
        integrate_ascent(vehicle.model_copy(update={"stages": stages}))


def test_burn_time_deviation_warns() -> None:
    """显式燃时与质量账派生燃时偏差 > 20% ⟹ 显式 warning（不静默采信/丢弃）。"""
    vehicle = falcon9_vehicle()
    stages = (
        vehicle.stages[0].model_copy(update={"burn_time_s": 300.0}),  # 派生 ≈ 162 s
        vehicle.stages[1],
    )
    result = integrate_ascent(vehicle.model_copy(update={"stages": stages}))
    assert any("燃时" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 端点（POST /api/perf/trajectory：异步受理 + 作业通道取回）
# ---------------------------------------------------------------------------


def test_endpoint_submits_and_returns_metrics(client: TestClient) -> None:
    """端点全链：受理（同步段微秒级）→ 轮询终态 → metrics 契约齐备。"""
    started = time.perf_counter()
    response = client.post(
        "/api/perf/trajectory", json={"vehicle": falcon9_vehicle().model_dump(mode="json")}
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    # 同步段不做积分（实测百毫秒级的积分在作业线程）——受理本身须远快于积分
    assert elapsed_ms < 1_000.0

    body = _await_job(client, job_id)
    assert body["status"] == "succeeded"
    metrics = body["metrics"]
    assert isinstance(metrics, dict)

    losses = metrics["losses_km_s"]
    for key in ("gravity_km_s", "aero_km_s", "steering_km_s", "back_pressure_km_s", "total_km_s"):
        assert key in losses
    burnout = metrics["burnout"]
    for key in (
        "time_s",
        "altitude_m",
        "velocity_m_s",
        "flight_path_angle_deg",
        "downrange_km",
        "mass_kg",
    ):
        assert key in burnout
    provenance = metrics["provenance"]
    for key in (
        "earth_model",
        "atmosphere",
        "integrator",
        "program",
        "back_pressure",
        "steering",
        "mass_ledger",
    ):
        assert key in provenance
    assert len(metrics["stage_timeline"]) == 2
    assert metrics["compute_ms"] > 0.0
    assert abs(metrics["closure_residual_m_s"]) < _CLOSURE_TOL_M_S


def test_endpoint_program_override_takes_effect(client: TestClient) -> None:
    """程序参数覆盖经端点生效（缺省剖面 vs 加大标高给出不同结果，且都成功）。"""
    vehicle_json = falcon9_vehicle().model_dump(mode="json")
    first = client.post("/api/perf/trajectory", json={"vehicle": vehicle_json})
    assert first.status_code == 200
    second = client.post(
        "/api/perf/trajectory",
        json={"vehicle": vehicle_json, "program": {"scale_height_m": 60_000.0}},
    )
    assert second.status_code == 200

    body_a = _await_job(client, first.json()["job_id"])
    body_b = _await_job(client, second.json()["job_id"])
    assert body_a["status"] == "succeeded" and body_b["status"] == "succeeded"
    assert body_a["metrics"] is not None and body_b["metrics"] is not None
    assert (
        body_a["metrics"]["losses_km_s"]["gravity_km_s"]
        != body_b["metrics"]["losses_km_s"]["gravity_km_s"]
    )


def test_endpoint_absurd_program_fails_job(client: TestClient) -> None:
    """程序参数荒谬（过 pydantic、越荒谬域）⟹ 作业 FAILED（TRAJECTORY_FAILED → 422 语义）。"""
    response = client.post(
        "/api/perf/trajectory",
        json={
            "vehicle": falcon9_vehicle().model_dump(mode="json"),
            "program": {"vertical_rise_s": 100.0},
        },
    )
    assert response.status_code == 200
    body = _await_job(client, response.json()["job_id"])
    assert body["status"] == "failed"
    error = body["error"]
    assert error is not None
    assert error["code"] == "TRAJECTORY_FAILED"
    assert "垂直上升段" in error["message"]


def test_endpoint_rejects_out_of_domain_request(client: TestClient) -> None:
    """步长越值域 → 422；硬约束违反（超装且未声明）→ 422 参数域拒绝口径。"""
    vehicle_json = falcon9_vehicle().model_dump(mode="json")
    assert (
        client.post(
            "/api/perf/trajectory",
            json={"vehicle": vehicle_json, "dt_s": 5.0},
        ).status_code
        == 422
    )

    overfilled = falcon9_vehicle()
    stages = (
        overfilled.stages[0].model_copy(update={"fill_fraction": 1.2}),
        overfilled.stages[1],
    )
    response = client.post(
        "/api/perf/trajectory",
        json={"vehicle": overfilled.model_copy(update={"stages": stages}).model_dump(mode="json")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PARAMS_CONSTRAINT_VIOLATION"


# ---------------------------------------------------------------------------
# 静默防回归：mypy/ruff 之外的手工对拍锚（量纲自检）
# ---------------------------------------------------------------------------


def test_ideal_dv_matches_rocket_equation_bookkeeping() -> None:
    """L2 理想 ΔV（∫F_vac/m dt）与逐级齐氏账（质量账口径）一致——同源对拍。"""
    vehicle = falcon9_vehicle()
    result = integrate_ascent(vehicle)
    # F9 两级：S1 = Isp·g₀·ln(m0/(m0−m_prop))（级内），S2 同式——总账手工重构
    from aeroforge.params.dag import G0

    ledger = vehicle_ledger(vehicle)
    glow = ledger.glow_kg(vehicle.payload_mass_kg)
    s1, s2 = ledger.stages
    m_after_s1 = glow - s1.m_propellant_kg
    expected = s1.isp_vacuum_s * G0 * math.log(glow / m_after_s1)
    m_s2_ignition = m_after_s1 - s1.m_dry_kg
    expected += (
        s2.isp_vacuum_s * G0 * math.log(m_s2_ignition / (m_s2_ignition - s2.m_propellant_kg))
    )
    assert result.ideal_dv_km_s == pytest.approx(expected / 1000.0, rel=1e-9)
