"""轨道精算层（规格 §8.10，M6 轨道层第一片）+ C3 双向一致 + 纬度曲线 + 端点。

覆盖形态（任务口径 / §16 M6 验收判据逐条）
------------------------------------------
- **Hohmann 两脉冲闭式对拍**：独立手算公式（活力公式）逐项对拍 + LEO→GEO
  公开量级锚（≈3.93 km/s）+ 下降转移的负脉冲口径；
- **复合机动矢量合成机检**（§8.10 约束 2）：Δi>0 时矢量 < 标量和（严格），
  Δi=0 时退化相等；GTO→GEO@28.5° 的差值量级对照（标量相加高估 ~2.75 km/s）；
- **平面变更 / 圆化单脉冲**：公式逐项手算；
- **C3↔ΔV 双向一致**（M6 验收判据）：正反 round-trip 机器精度（C3 方向与 ΔV
  方向各一）+ **独立书写能量方程对拍两侧实现**（两侧任一处硬编码即失配）
  + 定义域拒绝（C3 ≤ −μ/r_p、ΔV ≤ 0）；
- **TLI / TMI / 逃逸**（OI-22）：TLI 输出 C3 < 0 且 Δv≈3.15（§8.6 表注「≈3.1」锚）；
  TMI 的 ``window_assumption`` **必填非空**（§8.10 约束 4——M6 验收判据）且
  C3 ∈ [8, 15] 惯例区间；逃逸由 v∞² 反推（抛物线 3.22）；
- **常量标注机检**（§8.10 约束 3）：每个结果的 assumption 文案含 μ（WGS-84）
  与 r_p 数值，禁止裸数值；
- **七目标运力表**（OI-38 + §8.10）：TLI/TMI 行带 C3、TMI 带窗口假设、
  dv_source 记录「上升段锚定 + 轨道解析」拼合；趋势机检：**TLI ≤ LEO 运力**
  （ΔV 更大）、**GEO（GTO+圆化）≥ GEO 直送运力**（解析总 ΔV 13.94 低于直送
  锚定 15.40——直送锚定含直送剖面经验惩罚，GTO+圆化多一次点火的本征损失
  属有限推力域、不在理想脉冲口径内，§8.10 约束 1：以计算为准如实断言）；
- **运力—纬度曲线**（OI-23，M6 验收判据）：LEO / SSO 各一——固定其余参数，
  纬度 ↑ 运力单调不增（SSO 用南向发射剖面：极轨自转加成为零，需求随纬度
  单调增来自锚定插值的方向定标，见 losses._DV_ANCHORS_KM_S 注）；
- **端点**：``POST /api/orbits/transfer``（全项解析 / C3 成对 / TMI 窗口必填 /
  422 路径 / 同步耗时）与 ``POST /api/perf/latitude-curve``（单调判据 /
  payload_key 形态 / compute_ms 实测）。
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
from aeroforge.params.schema import Vehicle
from aeroforge.params.templates import falcon9_vehicle
from aeroforge.perf.capacity import (
    payload_by_orbit,
    payload_for_orbit,
    payload_latitude_curve,
    vehicle_ledger,
)
from aeroforge.perf.losses import (
    EARTH_EQUATOR_RADIUS_M,
    GEO_ALTITUDE_M,
    MOON_DISTANCE_M,
    TLI_C3_TYPICAL_KM2_S2,
    TMI_C3_TYPICAL_KM2_S2,
)
from aeroforge.perf.orbits import (
    MU_EARTH_M3_S2,
    TMI_WINDOW_ASSUMPTION_DEFAULT,
    apogee_composite_km_s,
    c3_from_delta_v_km2_s2,
    circularization_km_s,
    delta_v_from_c3_km_s,
    geo_via_gto_km_s,
    hohmann_transfer_km_s,
    parking_injection_km_s,
    plane_change_km_s,
)

#: μ 与参考半径（WGS-84，测试内独立书写——约束 3 的「禁止裸数值」由对拍验证）。
_MU = 3.986004418e14
_R_EQ = 6_378_137.0

#: 停泊轨道 200 km 圆轨道的半径 / 速度（测试内独立书写）。
_R_PARKING_M = _R_EQ + 200_000.0
_V_PARKING_KM_S = math.sqrt(_MU / _R_PARKING_M) / 1000.0

_R_GEO_M = _R_EQ + GEO_ALTITUDE_M


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


@pytest.fixture
def falcon9() -> Vehicle:
    """Falcon 9 模板（自带卡角发射场——七行表趋势断言的载体）。"""
    return falcon9_vehicle()


# ---------------------------------------------------------------------------
# Hohmann 两脉冲（§8.10 公式原文 + 独立手算对拍）
# ---------------------------------------------------------------------------


def test_hohmann_matches_independent_hand_computation() -> None:
    """逐项独立手算对拍：活力公式书写转移速度，闭式两侧必须同源一致。"""
    r1, r2 = _R_PARKING_M, _R_GEO_M
    a_t = 0.5 * (r1 + r2)
    v1 = math.sqrt(_MU / r1)
    v2 = math.sqrt(_MU / r2)
    v_peri = math.sqrt(_MU * (2.0 / r1 - 1.0 / a_t))  # 转移椭圆近地点速度
    v_apo = math.sqrt(_MU * (2.0 / r2 - 1.0 / a_t))  # 转移椭圆远地点速度
    expected_dv1 = (v_peri - v1) / 1000.0
    expected_dv2 = (v2 - v_apo) / 1000.0

    transfer = hohmann_transfer_km_s(r1, r2)
    assert transfer.dv1_km_s == pytest.approx(expected_dv1, rel=1e-12)
    assert transfer.dv2_km_s == pytest.approx(expected_dv2, rel=1e-12)
    assert transfer.total_km_s == pytest.approx(expected_dv1 + expected_dv2, rel=1e-12)
    assert transfer.semi_major_m == pytest.approx(a_t, rel=1e-12)
    # 公式原文交叉（§8.10 原式的另一书写形态）
    assert transfer.dv1_km_s == pytest.approx(
        v1 * (math.sqrt(2.0 * r2 / (r1 + r2)) - 1.0) / 1000.0, rel=1e-12
    )
    assert transfer.dv2_km_s == pytest.approx(
        v2 * (1.0 - math.sqrt(2.0 * r1 / (r1 + r2))) / 1000.0, rel=1e-12
    )


def test_hohmann_leo_to_geo_anchored_magnitude() -> None:
    """LEO(200 km)→GEO 公开量级锚：Hohmann 总 ΔV ≈ 3.93 km/s（工程惯例值）。"""
    transfer = hohmann_transfer_km_s(_R_PARKING_M, _R_GEO_M)
    assert transfer.total_km_s == pytest.approx(3.932, abs=5e-3)
    assert transfer.dv1_km_s == pytest.approx(2.455, abs=5e-3)
    assert transfer.dv2_km_s == pytest.approx(1.477, abs=5e-3)


def test_hohmann_downward_transfer_negative_brakes() -> None:
    """下降转移（r2 < r1）：两脉冲为负（制动），代数和口径不变。"""
    up = hohmann_transfer_km_s(_R_PARKING_M, _R_GEO_M)
    down = hohmann_transfer_km_s(_R_GEO_M, _R_PARKING_M)
    assert down.dv1_km_s < 0.0 and down.dv2_km_s < 0.0
    assert down.dv1_km_s == pytest.approx(-up.dv2_km_s, rel=1e-9)
    assert down.dv2_km_s == pytest.approx(-up.dv1_km_s, rel=1e-9)
    assert down.total_km_s == pytest.approx(-up.total_km_s, rel=1e-9)


def test_hohmann_rejects_degenerate_radii() -> None:
    """非正半径 / 同半径：显式拒绝（不静默给 0）。"""
    with pytest.raises(PerfError, match="必须为正"):
        hohmann_transfer_km_s(0.0, _R_GEO_M)
    with pytest.raises(PerfError, match="r1 ≠ r2"):
        hohmann_transfer_km_s(_R_PARKING_M, _R_PARKING_M)


# ---------------------------------------------------------------------------
# 平面变更 / 圆化 / 复合机动（§8.10 表 + 约束 2 矢量合成机检）
# ---------------------------------------------------------------------------


def test_plane_change_formula() -> None:
    """平面变更 ``2v·sin(Δi/2)`` 独立手算；Δi=180° 时恰为 2v（速度反转）。"""
    v = 3.0747  # GEO 圆轨道速度量级
    for delta_i in (0.0, 10.0, 28.5, 90.0, 180.0):
        expected = 2.0 * v * math.sin(math.radians(delta_i) / 2.0)
        assert plane_change_km_s(v, delta_i) == pytest.approx(expected, rel=1e-12)
    assert plane_change_km_s(v, 180.0) == pytest.approx(2.0 * v, rel=1e-12)
    with pytest.raises(PerfError):
        plane_change_km_s(v, 200.0)


def test_circularization_single_impulse() -> None:
    """远地点圆化 = 圆轨道速度 − 转移椭圆远地点速度（独立手算对拍）。"""
    r_p, r_a = _R_PARKING_M, _R_GEO_M
    a_t = 0.5 * (r_p + r_a)
    v_apo = math.sqrt(_MU * (2.0 / r_a - 1.0 / a_t)) / 1000.0
    v_circ_geo = math.sqrt(_MU / r_a) / 1000.0
    assert circularization_km_s(r_p, r_a) == pytest.approx(v_circ_geo - v_apo, rel=1e-12)
    # 近地点圆化（制动）为负值
    v_peri = math.sqrt(_MU * (2.0 / r_p - 1.0 / a_t)) / 1000.0
    v_circ_leo = math.sqrt(_MU / r_p) / 1000.0
    assert circularization_km_s(r_p, r_a, at_apogee=False) == pytest.approx(
        v_circ_leo - v_peri, rel=1e-12
    )
    with pytest.raises(PerfError, match="大于近地点"):
        circularization_km_s(r_a, r_p)


def test_composite_vector_less_than_scalar_sum() -> None:
    """§8.10 约束 2 机检：Δi>0 时矢量合成**严格小于**标量相加；Δi=0 退化相等。"""
    r_p, r_a = _R_PARKING_M, _R_GEO_M
    zero = apogee_composite_km_s(r_p, r_a, 0.0)
    assert zero.vector_km_s == pytest.approx(zero.circularization_km_s, rel=1e-12)
    assert zero.vector_km_s == pytest.approx(zero.scalar_sum_km_s, rel=1e-12)

    for delta_i in (5.0, 28.5, 60.0):
        comp = apogee_composite_km_s(r_p, r_a, delta_i)
        assert comp.vector_km_s < comp.scalar_sum_km_s, (
            f"Δi={delta_i}°：矢量合成必须小于标量相加（约束 2 的反例即标量相加）"
        )
        # 矢量合成的独立手算（余弦定理形态）
        a_t = 0.5 * (r_p + r_a)
        v_e = math.sqrt(_MU * (2.0 / r_a - 1.0 / a_t)) / 1000.0
        v_c = math.sqrt(_MU / r_a) / 1000.0
        expected = math.sqrt(v_c**2 + v_e**2 - 2.0 * v_c * v_e * math.cos(math.radians(delta_i)))
        assert comp.vector_km_s == pytest.approx(expected, rel=1e-12)

    # GTO→GEO @28.5°：标量相加高估的量级对照（记录实测，供报告引证）
    comp = apogee_composite_km_s(r_p, r_a, 28.5)
    print(
        f"\n[复合机动实测] Δi=28.5°：矢量 {comp.vector_km_s:.4f} vs 标量和 "
        f"{comp.scalar_sum_km_s:.4f} km/s（标量高估 {comp.scalar_sum_km_s - comp.vector_km_s:.4f}）"
    )
    assert comp.vector_km_s == pytest.approx(1.836, abs=5e-3)
    assert comp.scalar_sum_km_s - comp.vector_km_s > 1.0  # 标量相加的失真量级（实测 ~1.15）


def test_geo_via_gto_total_and_assumption_annotation() -> None:
    """GEO（GTO+圆化）路线：两脉冲之和 + 常量标注（约束 3）。"""
    geo = geo_via_gto_km_s(_R_PARKING_M, 28.5)
    assert geo.perigee_kick_km_s == pytest.approx(
        hohmann_transfer_km_s(_R_PARKING_M, _R_GEO_M).dv1_km_s, rel=1e-12
    )
    assert geo.total_km_s == pytest.approx(
        geo.perigee_kick_km_s + geo.apogee.vector_km_s, rel=1e-12
    )
    assert geo.total_km_s == pytest.approx(4.291, abs=5e-3)  # 公开量级锚
    for text in (geo.assumption, geo.apogee.assumption):
        assert "3.986004418e+14" in text  # μ 标注（WGS-84）
        assert "理想脉冲" in text  # 约束 1 声明


# ---------------------------------------------------------------------------
# C3 ↔ ΔV 双向一致（OI-23 / §8.10 约束 2——M6 验收判据）
# ---------------------------------------------------------------------------


_C3_GRID = (-2.0, -1.65, -0.5, 0.0, 1.0, 8.0, 11.5, 15.0, 40.0)
_RP_GRID_M = (_R_EQ + 185_000.0, _R_PARKING_M, _R_EQ + 300_000.0, _R_EQ + 500_000.0)


def test_c3_delta_v_round_trip_machine_precision() -> None:
    """正反 round-trip 机器精度：C3→ΔV→C3 与 ΔV→C3→ΔV（同一组 μ/r_p）。"""
    for r_p in _RP_GRID_M:
        for c3 in _C3_GRID:
            if c3 <= -_MU / r_p / 1e6:
                continue  # 定义域外（低于停泊轨道能量）
            dv = delta_v_from_c3_km_s(c3, r_p)
            back = c3_from_delta_v_km2_s2(dv, r_p)
            assert back == pytest.approx(c3, rel=1e-12, abs=1e-12), (c3, r_p, back)
            dv_back = delta_v_from_c3_km_s(c3_from_delta_v_km2_s2(dv, r_p), r_p)
            assert dv_back == pytest.approx(dv, rel=1e-12)


def test_c3_delta_v_no_hardcoding_independent_energy_equation() -> None:
    """独立书写能量方程对拍两侧实现——任一侧硬编码即失配（约束 2 的回归形态）。

    能量口径：``v_p² = 2μ/r_p + C3``（活力公式）与 ``v_p = v_circ + Δv``（脉冲
    定义）联立；两侧闭式都必须从这条方程独立推出，测试内**重新书写**而非复用
    实现内部表达式。
    """
    for r_p in _RP_GRID_M:
        v_circ = math.sqrt(_MU / r_p)  # m/s
        for c3 in _C3_GRID:
            if c3 <= -_MU / r_p / 1e6:
                continue
            c3_m2 = c3 * 1e6
            v_p = math.sqrt(2.0 * _MU / r_p + c3_m2)  # 能量方程：C3 → v_p
            expected_dv = (v_p - v_circ) / 1000.0
            assert delta_v_from_c3_km_s(c3, r_p) == pytest.approx(expected_dv, rel=1e-12)
            # 反向：Δv → v_p → C3（同一方程的另一方向）
            expected_c3 = ((v_circ + expected_dv * 1000.0) ** 2 - 2.0 * _MU / r_p) / 1e6
            assert c3_from_delta_v_km2_s2(expected_dv, r_p) == pytest.approx(expected_c3, rel=1e-12)


def test_c3_delta_v_domain_rejections() -> None:
    """定义域拒绝：C3 < −2μ/r_p（v_p 无实解）、C3 = −μ/r_p（Δv=0）、ΔV ≤ 0。"""
    energy_floor = -2.0 * _MU / _R_PARKING_M / 1e6  # ≈ −121.2 km²/s²
    with pytest.raises(PerfError, match="无实解"):
        delta_v_from_c3_km_s(energy_floor - 1.0, _R_PARKING_M)
    zero_margin = -_MU / _R_PARKING_M / 1e6  # ≈ −60.6：恰在 Δv=0 边界
    with pytest.raises(PerfError, match="不产生正向射入脉冲"):
        delta_v_from_c3_km_s(zero_margin, _R_PARKING_M)
    with pytest.raises(PerfError, match="必须为正"):
        c3_from_delta_v_km2_s2(0.0, _R_PARKING_M)
    with pytest.raises(PerfError, match="必须为正"):
        c3_from_delta_v_km2_s2(-1.0, _R_PARKING_M)


def test_c3_delta_v_constants_annotation() -> None:
    """约束 3：μ / r_p 全部标注——assumption 文案携带 WGS-84 μ 与 r_p 数值。"""
    inj = parking_injection_km_s("TMI", _R_PARKING_M)
    assert "3.986004418e+14" in inj.assumption
    assert f"r_p={_R_PARKING_M:.1f} m" in inj.assumption
    assert "理想脉冲" in inj.assumption  # 约束 1：不含有限推力损失
    assert inj.assumption.count("C3=") == 1  # C3 与 ΔV 同段标注（同组 μ/r_p 口径）


# ---------------------------------------------------------------------------
# TLI / TMI / 逃逸（OI-22：C3 与 ΔV 成对；§8.10 约束 4 窗口假设）
# ---------------------------------------------------------------------------


def test_tli_outputs_negative_c3_and_anchored_delta_v() -> None:
    """TLI：C3 < 0（地心束缚）输出、Δv ≈ 3.15 km/s（§8.6 表注「≈3.1」锚）。"""
    tli = parking_injection_km_s("TLI", _R_PARKING_M)
    assert tli.c3_km2_s2 == pytest.approx(TLI_C3_TYPICAL_KM2_S2)
    assert tli.c3_km2_s2 < 0.0
    assert pytest.approx(TLI_C3_TYPICAL_KM2_S2, abs=1e-12) == -1.65  # 惯例区间 [-2.0, -1.3] 中值
    assert tli.dv_km_s == pytest.approx(3.15, abs=5e-3)
    assert tli.window_assumption is None  # TLI 无窗口必填口径
    # 最小能量转移闭式对拍：远地点 = 地月距离时 C3 ≈ −2.04（区间下端的来源）
    a_min = 0.5 * (_R_PARKING_M + MOON_DISTANCE_M)
    c3_min_energy = -_MU / a_min / 1e6
    assert c3_min_energy == pytest.approx(-2.04, abs=5e-3)
    assert c3_min_energy <= TLI_C3_TYPICAL_KM2_S2  # 实际任务能量 ≥ 最小能量


def test_tli_rejects_positive_c3() -> None:
    """TLI 的 C3 必须 < 0：正 C3 显式拒绝（那是逃逸/行星转移的能量域）。"""
    with pytest.raises(PerfError, match="必须为负"):
        parking_injection_km_s("TLI", _R_PARKING_M, c3_km2_s2=5.0)


def test_tmi_window_assumption_mandatory() -> None:
    """TMI：window_assumption 必填非空（§8.10 约束 4——无窗口假设即不可复现）。"""
    tmi = parking_injection_km_s("TMI", _R_PARKING_M)
    assert tmi.window_assumption is not None
    assert len(tmi.window_assumption) > 20
    assert "窗口" in tmi.window_assumption and "C3" in tmi.window_assumption
    assert tmi.c3_km2_s2 == pytest.approx(TMI_C3_TYPICAL_KM2_S2)
    assert 8.0 <= tmi.c3_km2_s2 <= 15.0  # §8.6 表注典型区间
    # 区间中值口径的 ΔV 量级（记录实测）：C3=11.5 → Δv ≈ 3.73 km/s
    print(f"\n[TMI 实测] C3={tmi.c3_km2_s2} → Δv={tmi.dv_km_s:.4f} km/s（200 km 停泊）")
    assert tmi.dv_km_s == pytest.approx(3.735, abs=5e-3)
    # 空串窗口假设 → 回落缺省文案（禁止空窗假设输出）
    fallback = parking_injection_km_s("TMI", _R_PARKING_M, window_assumption="")
    assert fallback.window_assumption == TMI_WINDOW_ASSUMPTION_DEFAULT
    # 用户覆写窗口假设原样保留
    custom = parking_injection_km_s(
        "TMI", _R_PARKING_M, window_assumption="2028 年 11 月窗口，相位角 30°"
    )
    assert custom.window_assumption == "2028 年 11 月窗口，相位角 30°"
    # 指定窗口的 C3 覆写：ΔV 随 C3 单调（8 → 15 区间两端的手算对拍）
    low = parking_injection_km_s("TMI", _R_PARKING_M, c3_km2_s2=8.0)
    high = parking_injection_km_s("TMI", _R_PARKING_M, c3_km2_s2=15.0)
    assert low.dv_km_s < tmi.dv_km_s < high.dv_km_s
    assert high.dv_km_s == pytest.approx(
        math.sqrt(2.0 * _MU / _R_PARKING_M + 15.0e6) / 1000.0 - _V_PARKING_KM_S, rel=1e-12
    )


def test_escape_from_v_inf() -> None:
    """逃逸：由 v∞²（C3）反推；抛物线（v∞=0）Δv ≈ 3.224 km/s，v∞ 越大脉冲越大。"""
    parabolic = parking_injection_km_s("escape", _R_PARKING_M)
    assert parabolic.c3_km2_s2 == 0.0
    expected = math.sqrt(2.0 * _MU / _R_PARKING_M) / 1000.0 - _V_PARKING_KM_S
    assert parabolic.dv_km_s == pytest.approx(expected, rel=1e-12)
    assert parabolic.dv_km_s == pytest.approx(3.224, abs=5e-3)
    hyperbolic = parking_injection_km_s("escape", _R_PARKING_M, c3_km2_s2=4.0)  # v∞=2 km/s
    assert hyperbolic.c3_km2_s2 == 4.0
    assert hyperbolic.dv_km_s > parabolic.dv_km_s
    with pytest.raises(PerfError, match="必须非负"):
        parking_injection_km_s("escape", _R_PARKING_M, c3_km2_s2=-1.0)


# ---------------------------------------------------------------------------
# 七目标运力表（OI-38 + §8.10 复合行）与趋势机检
# ---------------------------------------------------------------------------


def test_payload_table_seven_rows_with_c3_and_window(falcon9: Vehicle) -> None:
    """七行齐备；TLI/TMI 行带 C3（成对输出），TMI 行带窗口假设；dv_source 记录拼合。"""
    vehicle = falcon9
    site = vehicle.mission.launch_site
    assert site is not None
    table = payload_by_orbit(vehicle, site)
    assert set(table) == {"LEO", "SSO", "GTO", "GEO", "TLI", "TMI", "GEO_GTO_CIRC"}
    for orbit in ("TLI", "TMI", "GEO_GTO_CIRC"):
        row = table[orbit]
        assert row.attainable
        assert "上升段" in row.dv_source and "§8.10 轨道解析" in row.dv_source
    assert table["TLI"].c3_km2_s2 is not None and table["TLI"].c3_km2_s2 < 0.0
    assert table["TMI"].c3_km2_s2 is not None and 8.0 <= table["TMI"].c3_km2_s2 <= 15.0
    assert table["TMI"].window_assumption
    assert table["GEO_GTO_CIRC"].c3_km2_s2 is None
    assert table["GEO_GTO_CIRC"].window_assumption is None
    # 拼合口径：复合行需求 = LEO 行需求 + 解析机动（逐项复算对拍；倾角取
    # Mission.inclination_deg=28.5——复合行转角的表内口径）
    r_p = EARTH_EQUATOR_RADIUS_M + 200_000.0
    geo = geo_via_gto_km_s(r_p, 28.5)
    assert table["GEO_GTO_CIRC"].dv_used_km_s == pytest.approx(
        table["LEO"].dv_used_km_s + geo.total_km_s, rel=1e-9
    )
    tli = parking_injection_km_s("TLI", r_p)
    assert table["TLI"].dv_used_km_s == pytest.approx(
        table["LEO"].dv_used_km_s + tli.dv_km_s, rel=1e-9
    )


def test_payload_table_trend_tli_leq_leo(falcon9: Vehicle) -> None:
    """趋势机检：TLI ≤ LEO 运力（TLI 需求 = LEO + 射入 3.15 km/s，更大）。"""
    vehicle = falcon9
    site = vehicle.mission.launch_site
    assert site is not None
    table = payload_by_orbit(vehicle, site)
    assert table["TLI"].payload_kg < table["LEO"].payload_kg
    assert table["TMI"].payload_kg < table["TLI"].payload_kg  # TMI C3 更高、需求更大


def test_payload_table_trend_geo_via_gto_beats_geo_direct(falcon9: Vehicle) -> None:
    """趋势机检（先手算锚定方向再断言）：GEO（GTO+圆化）≥ GEO 直送运力。

    方向推导（报告口径）：GTO+圆化的解析总 ΔV ≈ 13.94 km/s（上升锚定 9.65 +
    解析 4.29），**低于** GEO 直送的锚定值 15.40（§8.6 表：直送剖面经验惩罚 +
    高纬外推）——需求更低 ⟹ 运力更高。「多一次点火的本征损失」属有限推力域，
    理想脉冲口径不计入（§8.10 约束 1）；若未来两侧口径统一为同一损失模型，
    本趋势须按当时的计算重判。
    """
    vehicle = falcon9
    site = vehicle.mission.launch_site
    assert site is not None
    table = payload_by_orbit(vehicle, site)
    assert table["GEO_GTO_CIRC"].dv_used_km_s < table["GEO"].dv_used_km_s
    assert table["GEO_GTO_CIRC"].payload_kg > table["GEO"].payload_kg
    print(
        f"\n[GEO 两路线实测] 直送（锚定）ΔV={table['GEO'].dv_used_km_s:.2f} → "
        f"{table['GEO'].payload_kg:.0f} kg；GTO+圆化（解析）ΔV="
        f"{table['GEO_GTO_CIRC'].dv_used_km_s:.2f} → {table['GEO_GTO_CIRC'].payload_kg:.0f} kg"
    )


def test_composite_row_leo_addition_uses_leo_row_latitude(falcon9: Vehicle) -> None:
    """复合行上升段与 LEO 行同口径（同纬度插值 + 长燃时修正）——不同纬度下保持拼合。"""
    vehicle = falcon9
    for lat in (5.2, 28.56, 45.0):
        site = vehicle.mission.launch_site
        assert site is not None
        moved = site.model_copy(update={"latitude_deg": lat})
        ledger = vehicle_ledger(vehicle)
        leo = payload_for_orbit(vehicle, moved, "LEO", ledger=ledger)
        tli = payload_for_orbit(vehicle, moved, "TLI", ledger=ledger)
        r_p = EARTH_EQUATOR_RADIUS_M + 200_000.0
        tli_dv = parking_injection_km_s("TLI", r_p).dv_km_s
        assert tli.dv_used_km_s == pytest.approx(leo.dv_used_km_s + tli_dv, rel=1e-9)


# ---------------------------------------------------------------------------
# 运力—纬度曲线（OI-23——M6 验收判据：对纬度单调不增）
# ---------------------------------------------------------------------------


def _sso_polar_vehicle(two_stage_vehicle: Vehicle) -> Vehicle:
    """SSO 极轨剖面夹具：700 km 太阳同步轨道、南向发射（极轨自转加成为零）。"""
    mission = two_stage_vehicle.mission
    site = mission.launch_site
    assert site is not None
    return two_stage_vehicle.model_copy(
        update={
            "mission": mission.model_copy(
                update={
                    "orbit_type": "SSO",
                    "altitude_m": 700_000.0,
                    "inclination_deg": 98.2,
                    "launch_site": site.model_copy(update={"azimuth_deg": 180.0}),
                }
            )
        }
    )


def test_latitude_curve_leo_monotone_nonincreasing(two_stage_vehicle: Vehicle) -> None:
    """M6 验收判据（LEO）：固定其余参数，纬度 0→90° 运力单调不增。"""
    curve = payload_latitude_curve(two_stage_vehicle, "LEO")
    assert curve.payload_key == "payload_leo_kg"
    lats = [p.lat_deg for p in curve.points]
    payloads = [p.payload_kg for p in curve.points]
    assert lats == [0.0, 9.0, 18.0, 27.0, 36.0, 45.0, 54.0, 63.0, 72.0, 81.0, 90.0]
    for earlier, later in pairwise(payloads):
        assert earlier >= later, f"纬度 ↑ 运力 ↑（判据破坏）：{payloads}"
    assert payloads[0] > payloads[2]  # 低纬端非平（趋势真实存在）
    print(f"\n[LEO 曲线] {[f'{p:.0f}' for p in payloads]}")
    assert all(p.attainable for p in curve.points)


def test_latitude_curve_sso_monotone_nonincreasing(two_stage_vehicle: Vehicle) -> None:
    """M6 验收判据（SSO）：南向极轨剖面，纬度 ↑ 运力单调不增。

    极轨的自转加成为零（南向方位角的东向分量为 0），需求的纬度依赖来自锚定
    插值的方向定标（低纬 9.75 → 高纬 9.90，见 losses._DV_ANCHORS_KM_S 注）——
    需求随纬度单调增 ⟹ 运力单调不增。
    """
    vehicle = _sso_polar_vehicle(two_stage_vehicle)
    curve = payload_latitude_curve(vehicle, "SSO")
    assert curve.payload_key == "payload_sso_kg"
    payloads = [p.payload_kg for p in curve.points]
    for earlier, later in pairwise(payloads):
        assert earlier >= later, f"SSO 纬度 ↑ 运力 ↑（判据破坏）：{payloads}"
    print(f"\n[SSO 曲线] {[f'{p:.0f}' for p in payloads]}")


def test_latitude_curve_unknown_orbit_rejected(two_stage_vehicle: Vehicle) -> None:
    """表外轨道（escape/custom）：显式拒绝（单点解析走 /api/orbits/transfer）。"""
    with pytest.raises(PerfError, match="PAYLOAD_ORBITS"):
        payload_latitude_curve(two_stage_vehicle, "escape")


# ---------------------------------------------------------------------------
# 端点：POST /api/orbits/transfer（§8.10 全项解析；同步毫秒级）
# ---------------------------------------------------------------------------


def test_transfer_endpoint_geo_path_full_shape(client: TestClient) -> None:
    """GEO 路径：Hohmann + 圆化 + 平面变更 + 复合矢量合成 + μ 标注。"""
    response = client.post(
        "/api/orbits/transfer",
        json={
            "r_p_km": 6578.137,
            "target": "GEO",
            "inclination_deg": 28.5,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "r_p_km",
        "r_a_km",
        "mu_m3_s2",
        "parking_velocity_km_s",
        "hohmann",
        "circularization_km_s",
        "plane_change_km_s",
        "composite",
        "injection",
        "assumptions",
    }
    assert body["mu_m3_s2"] == pytest.approx(MU_EARTH_M3_S2)
    assert body["r_a_km"] == pytest.approx((_R_EQ + GEO_ALTITUDE_M) / 1000.0)
    assert body["parking_velocity_km_s"] == pytest.approx(_V_PARKING_KM_S, rel=1e-9)
    assert body["hohmann"]["total_km_s"] == pytest.approx(3.932, abs=5e-3)
    assert body["circularization_km_s"] == pytest.approx(1.477, abs=5e-3)
    assert body["plane_change_km_s"] == pytest.approx(1.514, abs=5e-3)
    composite = body["composite"]
    assert composite["vector_km_s"] < composite["scalar_sum_km_s"]  # 约束 2 机检
    assert composite["vector_minus_scalar_km_s"] < 0.0
    assert composite["vector_km_s"] == pytest.approx(1.836, abs=5e-3)
    assert body["injection"] is None
    assert any("3.986004418e+14" in item for item in body["assumptions"])


def test_transfer_endpoint_tli_tmi_escape_pairs_c3(client: TestClient) -> None:
    """TLI/TMI/escape 路径：C3 与 ΔV 成对；TMI 必带窗口假设；TLI 的 C3 < 0。"""
    tli = client.post("/api/orbits/transfer", json={"r_p_km": 6578.137, "target": "TLI"})
    assert tli.status_code == 200
    tli_body = tli.json()
    assert tli_body["injection"]["c3_km2_s2"] < 0.0
    assert tli_body["injection"]["window_assumption"] is None
    assert tli_body["injection"]["dv_km_s"] == pytest.approx(3.149, abs=5e-3)

    tmi = client.post("/api/orbits/transfer", json={"r_p_km": 6578.137, "target": "TMI"})
    assert tmi.status_code == 200
    tmi_body = tmi.json()
    injection = tmi_body["injection"]
    assert injection["window_assumption"]
    assert "窗口" in injection["window_assumption"]
    assert 8.0 <= injection["c3_km2_s2"] <= 15.0
    # 双向一致在响应链上复验：C3 反算 ΔV 与响应一致
    recomputed = delta_v_from_c3_km_s(injection["c3_km2_s2"], _R_PARKING_M)
    assert injection["dv_km_s"] == pytest.approx(recomputed, rel=1e-12)

    escape = client.post("/api/orbits/transfer", json={"r_p_km": 6578.137, "target": "escape"})
    assert escape.status_code == 200
    assert escape.json()["injection"]["c3_km2_s2"] == 0.0

    # 自定义 r_a 路径（与 target 互斥的另一半）
    custom = client.post(
        "/api/orbits/transfer",
        json={
            "r_p_km": 6578.137,
            "r_a_km": (_R_EQ + MOON_DISTANCE_M) / 1000.0,
            "inclination_deg": 0.0,
        },
    )
    assert custom.status_code == 200
    assert custom.json()["hohmann"] is not None
    assert custom.json()["composite"] is not None


def test_transfer_endpoint_validation_and_latency(client: TestClient) -> None:
    """请求自相矛盾（target 与 r_a_km 同给/全缺）→ 422；纯解析同步耗时实测。"""
    both = client.post(
        "/api/orbits/transfer", json={"r_p_km": 6578.137, "r_a_km": 42164.137, "target": "GEO"}
    )
    assert both.status_code == 422
    assert both.json()["error"]["code"] == "PERF_EVALUATE_FAILED"
    neither = client.post("/api/orbits/transfer", json={"r_p_km": 6578.137})
    assert neither.status_code == 422
    negative_c3 = client.post(
        "/api/orbits/transfer",
        json={"r_p_km": 6578.137, "target": "escape", "c3_km2_s2": -2.0},
    )
    assert negative_c3.status_code == 422

    started = time.perf_counter()
    for _ in range(20):
        ok = client.post("/api/orbits/transfer", json={"r_p_km": 6578.137, "target": "GEO"})
        assert ok.status_code == 200
    elapsed_ms = (time.perf_counter() - started) * 1000.0 / 20.0
    print(f"\n[orbits/transfer 实测] 单次平均 {elapsed_ms:.2f} ms（20 次均值）")
    assert elapsed_ms < 50.0, f"解析端点单次 {elapsed_ms:.2f} ms 超出 50 ms 交互预算"


# ---------------------------------------------------------------------------
# 端点：POST /api/perf/latitude-curve（OI-23 曲线）
# ---------------------------------------------------------------------------


def test_latitude_curve_endpoint_monotone_and_shape(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """曲线端点：payload_key 形态、单调不增（M6 判据）、compute_ms 实测。"""
    body = {"vehicle": two_stage_vehicle.model_dump(mode="json"), "orbit": "LEO"}
    response = client.post("/api/perf/latitude-curve", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["payload_key"] == "payload_leo_kg"
    assert data["orbit"] == "LEO"
    assert len(data["points"]) == 11
    payloads = [p["payload_kg"] for p in data["points"]]
    for earlier, later in pairwise(payloads):
        assert earlier >= later
    print(f"\n[latitude-curve 实测] compute_ms={data['compute_ms']:.1f}（11 点 LEO）")
    assert data["compute_ms"] < 1000.0  # 同步交互预算内（实测毫秒级）

    # orbit 缺省 = Mission.orbit_type（夹具为 LEO）——与显式指定同结果
    default_response = client.post(
        "/api/perf/latitude-curve", json={"vehicle": two_stage_vehicle.model_dump(mode="json")}
    )
    assert default_response.status_code == 200
    assert default_response.json()["orbit"] == "LEO"
    assert [p["payload_kg"] for p in default_response.json()["points"]] == payloads


def test_latitude_curve_endpoint_sso_monotone(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """曲线端点（SSO 南向剖面）：单调不增——M6 验收判据的端点级复验。"""
    body = {
        "vehicle": _sso_polar_vehicle(two_stage_vehicle).model_dump(mode="json"),
        "orbit": "SSO",
    }
    response = client.post("/api/perf/latitude-curve", json=body)
    assert response.status_code == 200
    payloads = [p["payload_kg"] for p in response.json()["points"]]
    for earlier, later in pairwise(payloads):
        assert earlier >= later


def test_latitude_curve_endpoint_rejects_unknown_orbit(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """表外轨道 → 422（PerfError 映射）。"""
    response = client.post(
        "/api/perf/latitude-curve",
        json={"vehicle": two_stage_vehicle.model_dump(mode="json"), "orbit": "custom"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PERF_EVALUATE_FAILED"
