"""性能评估（规格 §8.6 / §8.8 / §10.1）：弹道损失 L1 + 运力表 + ΔV 瀑布 + 端点。

覆盖形态（任务口径逐条）
------------------------
- **趋势机检三条（OI-38 / FR-19）**：同构型 LEO ≥ GTO 运力；GTO（库鲁 5.2°）≥
  GTO（卡角 28.5°）；固定其余参数纬度 5°→28°→60° LEO 运力单调不增；
- **闭合**（OI-23）：``ideal + 四损失 − 自转加成 == total`` 容差 1e-6 km/s，
  逐项分账（锚定插值 / 自转加成 / 理想 ΔV）独立手算对拍；assumptions 非空；
- **四轨道表形状 + dv_source 标注**（量级锚定 / Mission 用户输入）；
- **损失单项**：重力损失随 TWR↑ 单调不增（越域显式 warning）；相容因子削减时
  转向损失**同向增加**（§8.6 约束 2 机检——同一失配量的两面）；背压单一字段
  （响应无 pressure_margin / drag_loss 双命名）；
- **运力二分**：ΣΔV 随载荷严格单调减；二分结果 vs 单级齐氏闭式手算对拍；
- **端点**：200 形状（point / delta_v_budget / warnings / provenance / cache_hit，
  无 interval——OI-25 归第四片）、缓存命中第二次 ``cache_hit=true``、422 路径、
  同步耗时；
- **F9 冒烟**：falcon-9 模板 LEO 运力 vs 公开 22.8 t——**只报数字不设硬阈值**
  （L1 粗损失模型，M4 验收 <15% 归第四片统一回归）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from itertools import pairwise

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.errors import PerfError
from aeroforge.params.schema import LaunchSite, LossFactors, Vehicle
from aeroforge.params.templates import falcon9_vehicle
from aeroforge.perf.budget import CLOSURE_TOLERANCE_KM_S, delta_v_budget
from aeroforge.perf.capacity import (
    payload_by_orbit,
    payload_for_dv,
    vehicle_ledger,
)
from aeroforge.perf.losses import (
    DV_SOURCE_ANCHORED,
    EARTH_EQUATOR_RADIUS_M,
    EARTH_GM_M3_S2,
    OMEGA_EARTH_RAD_S,
    back_pressure_loss,
    compatibility_factor,
    gravity_loss,
    ideal_orbit_dv_km_s,
    orbit_dv_requirement,
    rotation_assist_km_s,
    steering_loss,
)

#: 闭合容差（§8.8 OI-23：1e-6 km/s，测试钉死）。
_CLOSURE_TOL = CLOSURE_TOLERANCE_KM_S

_REL_TOL = 1e-9  # 手算对拍容差（二分收敛到 1e-9 kg 量级，留一个量级余量）


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _site(latitude_deg: float, azimuth_deg: float = 90.0, altitude_m: float = 3.0) -> LaunchSite:
    """造一个发射场（默认向东——自转加成取最大的标准剖面，§8.6）。"""
    return LaunchSite(
        name=f"测试发射场 {latitude_deg}°",
        latitude_deg=latitude_deg,
        altitude_m=altitude_m,
        azimuth_deg=azimuth_deg,
    )


def _at_site(vehicle: Vehicle, latitude_deg: float, azimuth_deg: float = 90.0) -> Vehicle:
    """克隆飞行器并改发射场纬度（固定其余参数——FR-19 趋势断言的口径）。"""
    return vehicle.model_copy(
        update={
            "mission": vehicle.mission.model_copy(
                update={"launch_site": _site(latitude_deg, azimuth_deg)}
            )
        }
    )


# ---------------------------------------------------------------------------
# 趋势机检三条（OI-38 / FR-19——禁止只在文档里声称）
# ---------------------------------------------------------------------------


def test_capacity_ordering_leo_geq_gto(two_stage_vehicle: Vehicle) -> None:
    """趋势一：同构型同发射场，LEO 运力 ≥ GTO 运力（LEO 需求更低）。"""
    table = payload_by_orbit(two_stage_vehicle, _site(28.5))
    assert table["LEO"].attainable and table["GTO"].attainable
    assert table["LEO"].payload_kg > table["GTO"].payload_kg


def test_capacity_gto_low_latitude_beats_high(two_stage_vehicle: Vehicle) -> None:
    """趋势二：GTO（低纬=库鲁 5.2°）运力 ≥ GTO（高纬=卡纳维拉尔 28.5°）。"""
    kourou = payload_by_orbit(_at_site(two_stage_vehicle, 5.2), _site(5.2))
    cape = payload_by_orbit(_at_site(two_stage_vehicle, 28.5), _site(28.5))
    assert kourou["GTO"].attainable and cape["GTO"].attainable
    assert kourou["GTO"].payload_kg > cape["GTO"].payload_kg
    # 需求侧对拍：库鲁 11.55（表中值）< 卡角 12.30（表中值），纬度惩罚显式可见
    assert kourou["GTO"].dv_used_km_s == pytest.approx(11.55, abs=1e-12)
    assert cape["GTO"].dv_used_km_s == pytest.approx(12.30, abs=1e-12)


def test_capacity_leo_monotone_nonincreasing_in_latitude(two_stage_vehicle: Vehicle) -> None:
    """趋势三（FR-19）：固定其余参数，纬度 5°→28°→60° LEO 运力单调不增。"""
    payloads = [
        payload_by_orbit(_at_site(two_stage_vehicle, lat), _site(lat))["LEO"].payload_kg
        for lat in (5.0, 28.0, 60.0)
    ]
    assert payloads[0] >= payloads[1] >= payloads[2]
    assert payloads[0] > payloads[2]  # 全程单调非平（5° 与 60° 必须严格分离）


# ---------------------------------------------------------------------------
# 需求表与理想 ΔV 的手算对拍（§8.6：量级锚定、非权威）
# ---------------------------------------------------------------------------


def test_orbit_dv_requirement_anchored_interpolation() -> None:
    """锚定表中值 + 纬度线性插值（两端 clamp）的独立手算对拍。"""
    # 锚点上：LEO 低纬 9.4 / 高纬 9.65；GTO 库鲁 11.55 / 卡角 12.30（表中值）
    assert orbit_dv_requirement("LEO", _site(5.2)).value_km_s == pytest.approx(9.40, abs=1e-12)
    assert orbit_dv_requirement("LEO", _site(28.5)).value_km_s == pytest.approx(9.65, abs=1e-12)
    assert orbit_dv_requirement("GTO", _site(5.2)).value_km_s == pytest.approx(11.55, abs=1e-12)
    assert orbit_dv_requirement("GEO", _site(5.2)).value_km_s == pytest.approx(14.65, abs=1e-12)
    # 区间内线性：LEO @16.85°（中点）= 9.525
    mid = orbit_dv_requirement("LEO", _site(16.85))
    assert mid.value_km_s == pytest.approx(9.525, abs=1e-9)
    assert mid.source == DV_SOURCE_ANCHORED
    # 两端 clamp：60° 与 28.5° 同值（高纬锚外不再外推）
    assert orbit_dv_requirement("LEO", _site(60.0)).value_km_s == pytest.approx(9.65, abs=1e-12)
    assert orbit_dv_requirement("LEO", _site(0.0)).value_km_s == pytest.approx(9.40, abs=1e-12)
    # SSO 高于 LEO（表：SSO 需较大转向损失）且任意纬度保持该序
    for lat in (5.2, 28.5, 60.0):
        assert (
            orbit_dv_requirement("SSO", _site(lat)).value_km_s
            > orbit_dv_requirement("LEO", _site(lat)).value_km_s
        )


def test_orbit_dv_requirement_rejects_unsupported_orbits() -> None:
    """TLI / TMI 不在四目标表内：显式拒绝（ΔV 须与 C3 成对，随 M6 交付）。"""
    with pytest.raises(PerfError, match="C3"):
        orbit_dv_requirement("TLI", _site(28.5))
    with pytest.raises(PerfError):
        orbit_dv_requirement("TMI", _site(28.5))


def test_ideal_orbit_dv_hand_computed(two_stage_vehicle: Vehicle) -> None:
    """理想 ΔV 手算对拍：LEO 200 km 圆轨道速度 √(μ/r) ≈ 7.784 km/s（WGS-84）。"""
    ideal = ideal_orbit_dv_km_s("LEO", two_stage_vehicle.mission)
    expected = math.sqrt(EARTH_GM_M3_S2 / (EARTH_EQUATOR_RADIUS_M + 200_000.0)) / 1000.0
    assert ideal == pytest.approx(expected, rel=1e-12)
    assert ideal == pytest.approx(7.784, abs=5e-3)
    # GTO 近地点速度：√(μ(2/r_p − 1/a)) 手算
    r_p = EARTH_EQUATOR_RADIUS_M + 200_000.0
    r_a = EARTH_EQUATOR_RADIUS_M + 35_786_000.0
    expected_gto = math.sqrt(EARTH_GM_M3_S2 * (2.0 / r_p - 2.0 / (r_p + r_a))) / 1000.0
    assert ideal_orbit_dv_km_s("GTO", two_stage_vehicle.mission) == pytest.approx(
        expected_gto, rel=1e-12
    )


# ---------------------------------------------------------------------------
# ΔV 瀑布（OI-23）：闭合 + 逐项分账手算对拍 + assumptions
# ---------------------------------------------------------------------------


def test_budget_closure_and_itemized_reconciliation(two_stage_vehicle: Vehicle) -> None:
    """闭合 1e-6（§8.8 OI-23）+ 逐项独立手算对拍（自转加成公式 / 相容因子）。"""
    vehicle = two_stage_vehicle
    site = vehicle.mission.launch_site
    assert site is not None
    budget, warnings = delta_v_budget(vehicle, site, vehicle.mission)

    # 闭合纪律：ideal + 四损失 − 自转加成 == total（容差 1e-6 km/s，钉死）
    residual = (
        budget.ideal_dv_km_s
        + budget.gravity_loss_km_s
        + budget.aero_loss_km_s
        + budget.steering_loss_km_s
        + budget.back_pressure_loss_km_s
        - budget.rotation_assist_km_s
        - budget.total_dv_km_s
    )
    assert abs(residual) <= _CLOSURE_TOL

    # 自转加成独立手算：v_rot·sin(罗盘方位角)·相容因子（向东取最大；兼容 §8.6
    # 公式的 cos(方位角) 以正东为 0° 基准——罗盘 90° 的东向分量是 sin=1）
    inclination = vehicle.mission.inclination_deg
    compat = compatibility_factor(site.latitude_deg, site.azimuth_deg, inclination)
    assert compat == pytest.approx(1.0)  # 倾角=纬度、向东：相容因子满值（§8.6）
    v_rot = (
        OMEGA_EARTH_RAD_S
        * (EARTH_EQUATOR_RADIUS_M + site.altitude_m)
        * math.cos(math.radians(site.latitude_deg))
    )
    assert budget.rotation_assist_km_s == pytest.approx(
        v_rot * math.sin(math.radians(site.azimuth_deg)) * compat / 1000.0, rel=1e-12
    )
    assert 0.3 < budget.rotation_assist_km_s < 0.5  # 卡角量级锚（§8.8 示例 0.39）

    # 四项损失都落在 §8.6 L1 表的典型量级内（粗界——模型允许轻微越出表区间）
    assert 0.8 < budget.gravity_loss_km_s < 1.8
    assert 0.05 <= budget.aero_loss_km_s <= 0.45
    assert 0.1 <= budget.steering_loss_km_s <= 0.5
    assert 0.1 <= budget.back_pressure_loss_km_s <= 0.2

    # assumptions 必填：每项损失的模型假设与系数来源文字（§8.8：允许简化但必须标明）
    assert budget.assumptions
    joined = "".join(budget.assumptions)
    for keyword in ("重力损失", "气动损失", "转向损失", "背压损失", "自转加成", "闭合口径"):
        assert keyword in joined
    assert "工程惯例" in joined  # L1 系数的非权威属性必须显式声明

    # 夹具带显式 Aero → 无「Aero 层缺失」默认 warning
    assert not any("Aero 层缺失" in w for w in warnings)


def test_budget_aero_default_warning_when_layer_absent(two_stage_vehicle: Vehicle) -> None:
    """Aero 层缺失：Cd 按默认 0.3 计并显式 warning（§6.1 口径）。"""
    vehicle = two_stage_vehicle.model_copy(update={"aero": None})
    site = vehicle.mission.launch_site or _site(28.5)
    budget, warnings = delta_v_budget(vehicle, site, vehicle.mission)
    assert any("Aero 层缺失" in w for w in warnings)
    assert budget.aero_loss_km_s > 0.0  # 默认值参与计算，不是 0 也不是报错


def test_budget_user_loss_factors_override(two_stage_vehicle: Vehicle) -> None:
    """Mission.loss_factors 用户覆写：四项 = 份额 × 理想 ΔV（0 项未计入）。"""
    factors = LossFactors(gravity=0.12, drag=0.02, steering=0.03, back_pressure=0.02)
    vehicle = two_stage_vehicle.model_copy(
        update={"mission": two_stage_vehicle.mission.model_copy(update={"loss_factors": factors})}
    )
    site = vehicle.mission.launch_site or _site(28.5)
    budget, _ = delta_v_budget(vehicle, site, vehicle.mission)
    ideal = budget.ideal_dv_km_s
    assert budget.gravity_loss_km_s == pytest.approx(0.12 * ideal, rel=1e-12)
    assert budget.aero_loss_km_s == pytest.approx(0.02 * ideal, rel=1e-12)
    assert budget.steering_loss_km_s == pytest.approx(0.03 * ideal, rel=1e-12)
    assert budget.back_pressure_loss_km_s == pytest.approx(0.02 * ideal, rel=1e-12)
    # 闭合在用户覆写下同样成立
    residual = (
        budget.ideal_dv_km_s
        + budget.gravity_loss_km_s
        + budget.aero_loss_km_s
        + budget.steering_loss_km_s
        + budget.back_pressure_loss_km_s
        - budget.rotation_assist_km_s
        - budget.total_dv_km_s
    )
    assert abs(residual) <= _CLOSURE_TOL
    assert any("用户覆写" in item for item in budget.assumptions)
    # 运力表的 dv_source 同步切到用户输入口径
    table = payload_by_orbit(vehicle, site)
    assert all("Mission 用户输入" in row.dv_source for row in table.values())


# ---------------------------------------------------------------------------
# 损失单项（§8.6 L1）
# ---------------------------------------------------------------------------


def test_gravity_loss_monotone_nonincreasing_in_twr() -> None:
    """重力损失随 TWR↑ 单调不增（属性机检）；越出 [1.2, 1.6] 标定域显式 warning。"""
    twrs = (1.1, 1.2, 1.35, 1.5, 1.6, 1.8)
    values = [gravity_loss(twr, 160.0).value_km_s for twr in twrs]
    for earlier, later in pairwise(values):
        assert earlier >= later, f"TWR↑ 但重力损失上升：{values}"
    # 标定域内无 warning；域外（1.1 / 1.8）必须显式 warning（不静默外推）
    assert gravity_loss(1.35, 160.0).warning is None
    for twr in (1.1, 1.8):
        item = gravity_loss(twr, 160.0)
        assert item.warning is not None
        assert "越出" in item.warning
    # 燃时拉长 → 重力损失增加（在重力场中烧得更久）
    assert gravity_loss(1.4, 240.0).value_km_s > gravity_loss(1.4, 120.0).value_km_s


def test_compat_factor_cut_and_steering_increase_are_two_sides(
    single_stage_vehicle: Vehicle,
) -> None:
    """§8.6 约束 2 机检：需大倾角而向东发射时，相容因子削减 ⟺ 转向损失同向增加。

    同一失配量的两面——不得只算加成不算损失：倾角 28.5°→60°→90° 序列上，
    compatibility_factor 与 rotation_assist 单调下降、steering_loss 单调上升。
    """
    latitude, azimuth = 28.5, 90.0  # 向东发射，可达倾角 = 纬度
    inclinations = (28.5, 60.0, 90.0)
    factors = [compatibility_factor(latitude, azimuth, inc) for inc in inclinations]
    steers = [steering_loss(latitude, azimuth, inc).value_km_s for inc in inclinations]
    assists = [rotation_assist_km_s(latitude, 3.0, azimuth, inc) for inc in inclinations]
    assert factors[0] == pytest.approx(1.0)  # 倾角=可达倾角：满值
    assert factors[0] > factors[1] > factors[2]  # 失配越大削减越多
    assert steers[0] < steers[1] < steers[2]  # 同时转向损失同向增加（同一件事）
    assert assists[0] > assists[1] > assists[2]  # 加成被相容因子削减
    # 向西发射：加成为负（逆行罚项），物理口径自检
    assert rotation_assist_km_s(5.2, 3.0, 270.0, 5.2) < 0.0


def test_back_pressure_loss_single_field_and_range() -> None:
    """背压损失：0.1–0.2 km/s 区间；字段名唯一（§8.8——即「压力/控制余量」）。"""
    assert back_pressure_loss(16.0).value_km_s == pytest.approx(0.15, abs=1e-12)
    for eps in (10.0, 16.0, 60.0, 200.0):
        value = back_pressure_loss(eps).value_km_s
        assert 0.1 <= value <= 0.2
    assert "压力/控制余量" in back_pressure_loss(16.0).assumption


# ---------------------------------------------------------------------------
# 运力二分（OI-38）
# ---------------------------------------------------------------------------


def test_total_delta_v_strictly_decreasing_in_payload(single_stage_vehicle: Vehicle) -> None:
    """单调性：固定火箭的 ΣΔV 随载荷严格单调减（二分唯一收敛的数学前提）。"""
    ledger = vehicle_ledger(single_stage_vehicle)
    payloads = (0.0, 1_000.0, 5_000.0, 10_000.0, 20_000.0)
    values = [ledger.total_delta_v_m_s(p) for p in payloads]
    for earlier, later in pairwise(values):
        assert earlier > later
    # 质量账自洽：GLOW(载荷) = 载荷 + 全部级质量之和（线性、斜率 1）
    base = ledger.glow_kg(0.0)
    assert ledger.glow_kg(10_000.0) == pytest.approx(base + 10_000.0, rel=1e-12)


def test_payload_bisection_matches_single_stage_closed_form(
    single_stage_vehicle: Vehicle,
) -> None:
    """二分结果 vs 单级齐氏闭式手算对拍：``P = λ·m_prop/(λ−1) − (m_prop+m_dry)``。"""
    ledger = vehicle_ledger(single_stage_vehicle)
    stage = ledger.stages[0]
    c = stage.isp_vacuum_s * 9.80665
    dv_req = 8_000.0  # m/s（低于零载荷上限 ~9.1 km/s，解存在）
    lam = math.exp(dv_req / c)
    closed_form = lam * stage.m_propellant_kg / (lam - 1.0) - (
        stage.m_propellant_kg + stage.m_dry_kg
    )
    assert closed_form > 0.0
    result = payload_for_dv(single_stage_vehicle, dv_req / 1000.0)
    assert result == pytest.approx(closed_form, rel=1e-7)
    # 反向验证：把该载荷代回正向链，ΣΔV 恰命中需求（收敛容差口径）
    assert ledger.total_delta_v_m_s(result) == pytest.approx(dv_req, rel=1e-8)


def test_payload_for_dv_rejects_infeasible_and_nonpositive(single_stage_vehicle: Vehicle) -> None:
    """需求非正 / 超出零载荷可达上限：显式 PerfError（不静默给 0 或半收敛值）。"""
    with pytest.raises(PerfError, match="必须为正"):
        payload_for_dv(single_stage_vehicle, 0.0)
    with pytest.raises(PerfError, match="可达上限"):
        payload_for_dv(single_stage_vehicle, 20.0)  # 单级上限 ~9.1 km/s


def test_payload_by_orbit_unattainable_rows_reported_not_dropped(
    single_stage_vehicle: Vehicle,
) -> None:
    """不可达目标：表保持四行齐备，该项 payload=0 且 attainable=False（不丢行）。"""
    table = payload_by_orbit(single_stage_vehicle, _site(28.5))
    assert set(table) == {"LEO", "SSO", "GTO", "GEO"}
    leo = table["LEO"]
    assert not leo.attainable  # 单级上限 ~9.1 km/s < LEO 需求 9.65：如实报告
    assert leo.payload_kg == 0.0
    assert leo.dv_used_km_s == pytest.approx(9.65, abs=1e-12)  # 需求值仍如实给出


# ---------------------------------------------------------------------------
# 端点（§10.1：本片纯点值——无 interval / interval_pending，OI-25 归第四片）
# ---------------------------------------------------------------------------


def _evaluate_body(vehicle: Vehicle) -> dict[str, object]:
    return {"vehicle": vehicle.model_dump(mode="json")}


def test_evaluate_endpoint_returns_full_shape(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """200 形状：point / delta_v_budget / warnings / provenance / cache_hit。"""
    vehicle = two_stage_vehicle.model_copy(update={"name": "形状测试箭"})
    response = client.post("/api/perf/evaluate", json=_evaluate_body(vehicle))
    assert response.status_code == 200
    body = response.json()

    assert set(body) == {"point", "delta_v_budget", "warnings", "provenance", "cache_hit"}
    assert body["cache_hit"] is False  # 本会话首次（conftest 隔离了数据目录）
    assert "interval" not in body and "interval_pending" not in body

    point = body["point"]
    assert set(point) == {"payload_by_orbit", "payload_mass_kg", "glow_kg", "c3_km2_s2"}
    assert point["payload_mass_kg"] == two_stage_vehicle.payload_mass_kg
    assert point["glow_kg"] > 0.0
    assert point["c3_km2_s2"] is not None and point["c3_km2_s2"] < 0.0  # LEO 束缚轨道

    table = point["payload_by_orbit"]
    assert set(table) == {"LEO", "SSO", "GTO", "GEO"}
    for row in table.values():
        assert set(row) == {"payload_kg", "dv_used_km_s", "dv_source", "attainable"}
        assert row["dv_source"] == DV_SOURCE_ANCHORED
    assert table["LEO"]["attainable"] and table["GTO"]["attainable"]
    assert table["LEO"]["payload_kg"] > table["GTO"]["payload_kg"]
    assert not table["GEO"]["attainable"]  # two_stage 上限 ~13.3 km/s < GEO 15.4
    assert any("可达上限" in w for w in body["warnings"])

    budget = body["delta_v_budget"]
    assert set(budget) == {
        "target_orbit",
        "ideal_dv_km_s",
        "gravity_loss_km_s",
        "aero_loss_km_s",
        "steering_loss_km_s",
        "back_pressure_loss_km_s",
        "rotation_assist_km_s",
        "total_dv_km_s",
        "assumptions",
    }
    # 背压单一字段：响应全文不得出现另一命名（pressure_margin / drag_loss 双命名）
    assert "pressure_margin" not in response.text
    assert "drag_loss" not in response.text
    # 闭合（1e-6）在响应链上复验
    residual = (
        budget["ideal_dv_km_s"]
        + budget["gravity_loss_km_s"]
        + budget["aero_loss_km_s"]
        + budget["steering_loss_km_s"]
        + budget["back_pressure_loss_km_s"]
        - budget["rotation_assist_km_s"]
        - budget["total_dv_km_s"]
    )
    assert abs(residual) <= _CLOSURE_TOL
    assert budget["assumptions"]
    assert body["provenance"]["payload_by_orbit.dv_source"]


def test_evaluate_endpoint_cache_hit_on_second_call(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """缓存：同一输入第二次请求 cache_hit=true 且数值与首次一致（§9.2）。"""
    # 独立命名保证缓存键不与同文件其他测试共享（fixture 内容相同会同键命中）
    vehicle = two_stage_vehicle.model_copy(update={"name": "缓存测试箭"})
    body = {"vehicle": vehicle.model_dump(mode="json")}
    first = client.post("/api/perf/evaluate", json=body)
    assert first.status_code == 200
    assert first.json()["cache_hit"] is False

    second = client.post("/api/perf/evaluate", json=body)
    assert second.status_code == 200
    assert second.json()["cache_hit"] is True
    # 命中结果与重算结果数值一致（除 cache_hit 标记）
    first_payload = first.json()["point"]["payload_by_orbit"]
    second_payload = second.json()["point"]["payload_by_orbit"]
    assert first_payload == second_payload
    assert first.json()["delta_v_budget"] == second.json()["delta_v_budget"]

    # 输入变一字节（改载荷）→ 键变 → 不命中（canonical JSON 键纪律）
    changed = vehicle.model_copy(update={"payload_mass_kg": 21_000.0})
    third = client.post("/api/perf/evaluate", json={"vehicle": changed.model_dump(mode="json")})
    assert third.status_code == 200
    assert third.json()["cache_hit"] is False


def test_evaluate_endpoint_latency_synchronous(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """同步耗时：首算与缓存命中都应远低于交互预算（实测数字随报告呈交主线）。"""
    vehicle = two_stage_vehicle.model_copy(update={"name": "耗时测试箭"})
    body = {"vehicle": vehicle.model_dump(mode="json")}
    started = time.perf_counter()
    first = client.post("/api/perf/evaluate", json=body)
    compute_ms = (time.perf_counter() - started) * 1000.0
    started = time.perf_counter()
    cached = client.post("/api/perf/evaluate", json=body)
    cache_ms = (time.perf_counter() - started) * 1000.0
    assert first.status_code == 200 and cached.status_code == 200
    print(f"\n[perf/evaluate 实测] 首算（未命中）{compute_ms:.1f} ms；缓存命中 {cache_ms:.1f} ms")
    assert compute_ms < 500.0, f"首算耗时 {compute_ms:.1f} ms 超出 500 ms 交互预算"


def test_evaluate_endpoint_rejects_schema_violation(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """σ 越出 Schema 值域：REQUEST_INVALID → 422（既有错误体系）。"""
    payload = two_stage_vehicle.model_dump(mode="json")
    payload["stages"][0]["structure_coefficient"] = 1.5
    response = client.post("/api/perf/evaluate", json={"vehicle": payload})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_INVALID"


def test_evaluate_endpoint_rejects_hard_constraint(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """isp_source=custom 缺值：硬约束拒绝（PARAMS_CONSTRAINT_VIOLATION → 422）。"""
    broken = two_stage_vehicle.model_copy(
        update={
            "stages": (
                two_stage_vehicle.stages[0].model_copy(update={"isp_source": "custom"}),
                two_stage_vehicle.stages[1],
            )
        }
    )
    response = client.post("/api/perf/evaluate", json=_evaluate_body(broken))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PARAMS_CONSTRAINT_VIOLATION"


def test_evaluate_endpoint_defaults_launch_site_with_warning(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """发射场走引用式（launch_site_id，本片不解析）：按默认场计算并 warning（§6.1 口径）。

    两字段全空是硬约束（HARD_LAUNCH_SITE_MISSING——纬度是 §8.6 的唯一输入，
    缺失即 422，不走静默默认）；本路径覆盖的是「launch_site_id 引用解析随后续片」
    的中间态：内联缺失但引用存在 → 默认场 + 显式 warning。
    """
    vehicle = two_stage_vehicle.model_copy(
        update={
            "name": "默认场测试箭",
            "mission": two_stage_vehicle.mission.model_copy(
                update={"launch_site": None, "launch_site_id": "ksc-lc39a"}
            ),
        }
    )
    response = client.post("/api/perf/evaluate", json=_evaluate_body(vehicle))
    assert response.status_code == 200
    body = response.json()
    assert any("launch_site 缺失" in w for w in body["warnings"])
    # 默认场纬度 28.5°：LEO 结果与显式卡角 28.5° 完全一致（同一输入同一结果）
    explicit = client.post(
        "/api/perf/evaluate",
        json=_evaluate_body(
            _at_site(two_stage_vehicle.model_copy(update={"name": "默认场对照箭"}), 28.5)
        ),
    ).json()
    assert (
        body["point"]["payload_by_orbit"]["LEO"]["payload_kg"]
        == explicit["point"]["payload_by_orbit"]["LEO"]["payload_kg"]
    )


# ---------------------------------------------------------------------------
# F9 冒烟（只报数字不设硬阈值——L1 粗损失模型，<15% 验收归第四片统一回归）
# ---------------------------------------------------------------------------


def test_falcon9_leo_capacity_smoke_reports_number() -> None:
    """falcon-9 模板：四轨道点值运力 vs 公开值——断言有限正数与序关系，数字随报告呈交。

    公开对照（§13.2 基准表同源）：LEO 22.8 t / GTO ~8.3 t（expendable 口径）。
    L1 粗损失模型下的偏差供主线预判 M4 验收（<15% 门槛归第四片统一回归）。
    """
    vehicle = falcon9_vehicle()
    site = vehicle.mission.launch_site
    assert site is not None
    table = payload_by_orbit(vehicle, site)
    leo = table["LEO"].payload_kg
    gto = table["GTO"].payload_kg
    geo = table["GEO"].payload_kg
    print(
        f"\n[F9 冒烟] LEO={leo:.0f} kg（公开 22,800，偏差 {100 * (leo / 22_800 - 1):+.1f}%）"
        f" | GTO={gto:.0f} kg（公开 ~8,300，偏差 {100 * (gto / 8_300 - 1):+.1f}%）"
        f" | GEO(直送)={geo:.0f} kg | SSO={table['SSO'].payload_kg:.0f} kg"
    )
    assert all(row.attainable for row in table.values())
    assert math.isfinite(leo) and leo > 0.0
    assert leo > gto > geo  # 轨道越远运力越低（点值序自检）
