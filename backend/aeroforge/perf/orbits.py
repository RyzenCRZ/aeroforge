"""轨道精算层（规格 §8.10，M6 轨道层第一片；OI-22 / OI-23）。

全项解析闭式 + C3↔ΔV 双向一致
------------------------------
§8.6 损失层覆盖「上升段」的 ΔV 需求（含损失、含纬度依赖）；本模块是 §8.10 的
**轨道机动解析闭式**：Hohmann 两脉冲、轨道圆化、平面变更、复合机动（圆化 + 平面
变更的矢量合成）、TLI / TMI / 逃逸的单脉冲射入——全部是**停泊轨道上的真空脉冲
机动**，与上升段需求拼合后进入运力表复合行（:mod:`aeroforge.perf.capacity`）。

约束对齐（§8.10 约束 1–4，逐条落点）
------------------------------------
1. **理想脉冲近似**：每个结果的 ``assumption`` 文案显式声明不含有限推力损失；
   有限推力损失由 §8.6 损失预算（上升段）覆盖，本层不重复计入；
2. **复合机动矢量合成**：远地点「圆化 + 平面变更」按
   ``Δv = √(v₁² + v₂² − 2·v₁·v₂·cos Δi)`` 合成（:func:`apogee_composite_km_s`），
   **禁止标量相加**——响应同时给出标量和与矢量差值供机检对照；
3. **常量全部标注**：μ、r_p、停泊轨道高度随每个结果文案输出（WGS-84 常量自
   :mod:`aeroforge.perf.losses` 单点导入，本模块不复制第二份）；
4. **不涉及长期轨道演化**：输入输出只有 ΔV / C3 / 半径（§1.3 非目标）。

C3↔ΔV 双向一致（OI-23，M6 验收判据）
------------------------------------
同一组 μ / r_p 下 :func:`delta_v_from_c3_km_s`（C3 → ΔV）与
:func:`c3_from_delta_v_km2_s2`（ΔV → C3）互逆，闭式来自同一条能量方程：

.. code-block:: text

    C3  = v∞²                          ← 束缚轨道为负（TLI），逃逸为正
    v_p = √(2μ/r_p + C3)               ← 转移轨道在停泊半径 r_p 处的速度
    Δv  = v_p − √(μ/r_p)               ← 从 r_p 圆轨道射入的单脉冲

回归测试（test_orbits.py）以机器精度 round-trip 钉死，并以**独立书写的能量方程
对拍两侧公式**断言无硬编码。TMI 结果必带 ``window_assumption``（约束 4：未记录
窗口假设的 TMI 结果视为不可复现，CON-04 同口径）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from aeroforge.errors import PerfError
from aeroforge.perf.losses import (
    EARTH_EQUATOR_RADIUS_M,
    EARTH_GM_M3_S2,
    GEO_ALTITUDE_M,
    TLI_C3_TYPICAL_KM2_S2,
    TMI_C3_TYPICAL_KM2_S2,
)

# ---------------------------------------------------------------------------
# 常量（单点来源：WGS-84 地球常量与轨道要素自 losses 导入并再导出——本模块
# 是 §8.10 的消费门面，禁止复制第二份数值；出处见 losses.py 常量区注释）
# ---------------------------------------------------------------------------

#: 地心引力常数 [m³/s²]（WGS-84 GM = 3.986004418×10¹⁴；单点定义在 losses.py）。
MU_EARTH_M3_S2 = EARTH_GM_M3_S2

#: 地球赤道半径 [m]（WGS-84 长半轴 a = 6 378 137 m；单点定义在 losses.py）。
R_EARTH_EQUATOR_M = EARTH_EQUATOR_RADIUS_M

#: 停泊轨道缺省高度 [m]（§8.6/losses 的 LEO 缺省剖面同口径 200 km 圆轨道）。
PARKING_DEFAULT_ALT_M = 200_000.0

#: GEO 目标半径 [m]（地球同步轨道 = R_地球 + 35 786 km，losses 单点定义）。
GEO_RADIUS_M = EARTH_EQUATOR_RADIUS_M + GEO_ALTITUDE_M

#: TMI 窗口假设的缺省文案（§8.10 约束 4 / CON-04：无窗口假设的 TMI 输出视为
#: 不可复现——本字段必填，调用方可用具体窗口假设覆写，但不得置空）。
TMI_WINDOW_ASSUMPTION_DEFAULT = (
    "窗口/相位假设：未指定具体发射窗口，按地火 Hohmann 型相位的典型窗口口径处理"
    "（相位角约 44°、地火转移飞行约 260 天，公开工程惯例值）；C3 取公开惯例区间 "
    "8–15 km²/s² 的区间中值（§8.6 表注口径）。⚠ 同一构型在不同窗口的 C3 可差数倍"
    "（§8.10 OI-22），本值仅为跨窗口量级代表——禁止当权威结论引用；指定窗口后须以"
    "实际相位重算 C3 并覆写本假设"
)

#: 转移射入目标类型（TLI / TMI / escape；§8.10 表三行单脉冲机动）。
InjectionTarget = Literal["TLI", "TMI", "escape"]

# ---------------------------------------------------------------------------
# 结果类型（每个结果自带 assumption 文案——μ / r_p / 假设全部标注，§8.10 约束 3）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HohmannTransfer:
    """Hohmann 两脉冲转移闭式结果（§8.10 公式原文）。

    ``dv1`` 在内圆轨道（r1）加速、``dv2`` 在外圆轨道（r2）加速；r2 < r1 时两者
    为负（制动），``total = dv1 + dv2`` 保持代数和口径。
    """

    dv1_km_s: float
    """第一脉冲 [km/s]（内圆轨道 r1 上；下降转移为负值——制动）。"""

    dv2_km_s: float
    """第二脉冲 [km/s]（外圆轨道 r2 上；下降转移为负值——制动）。"""

    total_km_s: float
    """两脉冲代数和 [km/s]（``dv1 + dv2``）。"""

    semi_major_m: float
    """转移椭圆半长轴 [m]（``(r1 + r2) / 2``——标注用，§8.10 约束 3）。"""

    assumption: str
    """模型假设与常量标注（理想脉冲声明 + μ / r1 / r2 出处口径）。"""


@dataclass(frozen=True, slots=True)
class ApogeeComposite:
    """远地点复合机动（圆化 + 平面变更）——矢量合成为权威值（§8.10 约束 2）。"""

    circularization_km_s: float
    """远地点圆化单脉冲 [km/s]（椭圆远地点速度 → 当地圆轨道速度）。"""

    plane_change_km_s: float
    """单独平面变更脉冲 [km/s]（``2·v·sin(Δi/2)``，v 取远地点圆轨道速度）。"""

    vector_km_s: float
    """矢量合成脉冲 [km/s]（权威值：``√(v_c² + v_e² − 2·v_c·v_e·cos Δi)``）。"""

    scalar_sum_km_s: float
    """标量相加 [km/s]（**禁止的口径**——仅随行输出供差值机检，不得作为需求使用）。"""

    inclination_deg: float
    """平面变更转角 [deg]（GTO 倾角 → 0° 赤道圆轨道）。"""

    assumption: str
    """模型假设与常量标注（矢量合成公式 + μ / r_a / Δi 口径）。"""


@dataclass(frozen=True, slots=True)
class GeoViaGto:
    """GEO 的 GTO + 远地点圆化路线（§8.10 表「工程上最常见路线」）。

    近地点点火入 GTO（``perigee_kick_km_s`` = Hohmann 第一脉冲）→ 远地点复合机动
    （圆化 + 消倾角矢量合成）。GEO 直送走同一公式族（同口径：直送即两脉冲连续
    施加、无 GTO 驻留——ΔV 与本路线一致，差别在点火节奏与损失侧，§8.10 约束 1）。
    """

    perigee_kick_km_s: float
    """近地点点火 [km/s]（停泊圆轨道 → GTO 转移椭圆近地点速度）。"""

    apogee: ApogeeComposite
    """远地点复合机动（圆化 + 平面变更矢量合成）。"""

    total_km_s: float
    """路线总 ΔV [km/s]（``perigee_kick + apogee.vector``）。"""

    assumption: str
    """路线假设（理想脉冲 + 复合矢量合成声明 + 常量标注）。"""


@dataclass(frozen=True, slots=True)
class ParkingInjection:
    """停泊轨道单脉冲转移射入（TLI / TMI / escape，§8.10 表 OI-22 两行）。"""

    dv_km_s: float
    """射入脉冲 [km/s]（由 C3 反算，同一组 μ / r_p——双向一致的 C3 方向）。"""

    c3_km2_s2: float
    """特征能量 C3 = v∞² [km²/s²]（TLI 为负——地心束缚；escape ≥ 0；TMI 典型 8–15）。"""

    assumption: str
    """模型假设与常量标注（μ / r_p / C3 取值口径）。"""

    window_assumption: str | None = None
    """窗口/相位假设（§8.10 约束 4）：TMI **必填**（缺省文案见
    :data:`TMI_WINDOW_ASSUMPTION_DEFAULT`）；TLI / escape 为 None。"""


# ---------------------------------------------------------------------------
# 基础闭式（§8.10 表逐行）
# ---------------------------------------------------------------------------


def _validate_radius(radius_m: float, name: str) -> None:
    if not (radius_m > 0.0):
        raise PerfError(
            f"{name} 必须为正，收到 {radius_m} m",
            suggestion="轨道半径为地心距（m），如 200 km 停泊轨道 ≈ 6.578×10⁶ m",
        )


def circular_velocity_km_s(radius_m: float) -> float:
    """圆轨道速度 [km/s]：``√(μ/r)``（μ = WGS-84 GM，标注见常量区）。"""
    _validate_radius(radius_m, "轨道半径")
    return math.sqrt(MU_EARTH_M3_S2 / radius_m) / 1000.0


def hohmann_transfer_km_s(r1_m: float, r2_m: float) -> HohmannTransfer:
    """Hohmann 两脉冲转移（§8.10 公式原文，解析闭式）。

    .. code-block:: text

        a_t  = (r1 + r2) / 2
        Δv1  = √(μ/r1) · ( √(2·r2/(r1+r2)) − 1 )
        Δv2  = √(μ/r2) · ( 1 − √(2·r1/(r1+r2)) )

    r2 < r1（下降/回收转移）时两脉冲为负值（制动），代数和口径不变。
    理想脉冲近似，不含有限推力损失（§8.10 约束 1）。
    """
    _validate_radius(r1_m, "内轨道半径 r1")
    _validate_radius(r2_m, "外轨道半径 r2")
    if r1_m == r2_m:
        raise PerfError(
            "Hohmann 转移要求 r1 ≠ r2（同半径无转移）",
            suggestion="圆轨道到同半径圆轨道的 ΔV 为 0；请检查 r1/r2 输入",
        )
    a_t = 0.5 * (r1_m + r2_m)
    v1 = math.sqrt(MU_EARTH_M3_S2 / r1_m)
    v2 = math.sqrt(MU_EARTH_M3_S2 / r2_m)
    dv1 = v1 * (math.sqrt(2.0 * r2_m / (r1_m + r2_m)) - 1.0)
    dv2 = v2 * (1.0 - math.sqrt(2.0 * r1_m / (r1_m + r2_m)))
    total = dv1 + dv2
    return HohmannTransfer(
        dv1_km_s=dv1 / 1000.0,
        dv2_km_s=dv2 / 1000.0,
        total_km_s=total / 1000.0,
        semi_major_m=a_t,
        assumption=(
            f"理想脉冲 Hohmann 两脉冲闭式（§8.10 公式原文）：μ={MU_EARTH_M3_S2:.9e} m³/s²"
            f"（WGS-84 GM）、r1={r1_m:.1f} m、r2={r2_m:.1f} m、a_t={a_t:.1f} m；"
            "不含有限推力损失（有限推力由 §8.6 损失预算覆盖，§8.10 约束 1）"
        ),
    )


def plane_change_km_s(v_km_s: float, delta_i_deg: float) -> float:
    """平面变更单脉冲 [km/s]（§8.10 表）：``Δv = 2·v·sin(Δi/2)``。

    ``v`` 取机动点的轨道速度 [km/s]；Δi = 0 时恒为 0。与远地点圆化复合时
    **不得**把本结果与圆化脉冲标量相加——用 :func:`apogee_composite_km_s`
    的矢量合成（§8.10 约束 2）。
    """
    if not (0.0 <= delta_i_deg <= 180.0):
        raise PerfError(
            f"平面变更转角必须在 [0, 180]° 内，收到 {delta_i_deg}°",
            suggestion="轨道倾角变更量取两轨道倾角的夹角（0–180°）",
        )
    return 2.0 * v_km_s * math.sin(math.radians(delta_i_deg) / 2.0)


def _ellipse_velocity_km_s(radius_m: float, semi_major_m: float) -> float:
    """椭圆轨道在 radius 处的速度 [km/s]（活力公式 ``√(μ(2/r − 1/a))``）。"""
    return math.sqrt(MU_EARTH_M3_S2 * (2.0 / radius_m - 1.0 / semi_major_m)) / 1000.0


def circularization_km_s(r_p_m: float, r_a_m: float, *, at_apogee: bool = True) -> float:
    """轨道圆化单脉冲 [km/s]（§8.10 表「单次脉冲消除偏心率」）。

    在远地点圆化（默认）：``√(μ/r_a) − v_ellipse(r_a)``（加速，正值）；在近地点
    圆化：``√(μ/r_p) − v_ellipse(r_p)``（制动，负值）。理想脉冲口径。
    """
    _validate_radius(r_p_m, "近地点半径 r_p")
    _validate_radius(r_a_m, "远地点半径 r_a")
    if r_a_m <= r_p_m:
        raise PerfError(
            f"远地点半径必须大于近地点半径：r_a={r_a_m:.1f} m ≤ r_p={r_p_m:.1f} m",
            suggestion="圆化的对象是椭圆转移轨道（r_a > r_p）；同半径即圆轨道无需圆化",
        )
    a_t = 0.5 * (r_p_m + r_a_m)
    point_m = r_a_m if at_apogee else r_p_m
    v_circ_km_s = math.sqrt(MU_EARTH_M3_S2 / point_m) / 1000.0
    return v_circ_km_s - _ellipse_velocity_km_s(point_m, a_t)


def apogee_composite_km_s(r_p_m: float, r_a_m: float, inclination_deg: float) -> ApogeeComposite:
    """远地点复合机动：圆化 + 平面变更**矢量合成**（§8.10 约束 2，禁止标量相加）。

    转移椭圆远地点速度 ``v_e`` 与目标赤道圆轨道速度 ``v_c`` 夹角 Δi（= 转移轨道
    倾角），合成脉冲：

    .. code-block:: text

        Δv = √( v_c² + v_e² − 2·v_c·v_e·cos Δi )

    Δi = 0 时退化为纯圆化（与 :func:`circularization_km_s` 一致）；Δi > 0 时
    **严格小于**「圆化 + 平面变更」的标量和（随行输出 ``scalar_sum_km_s`` 供
    差值机检——标量相加系统性高估，是本约束要拦的形态）。
    """
    if not (0.0 <= inclination_deg <= 180.0):
        raise PerfError(
            f"转移轨道倾角必须在 [0, 180]° 内，收到 {inclination_deg}°",
            suggestion="GTO 倾角即发射场纬度（向东发射）或 Mission.inclination_deg",
        )
    circularization = circularization_km_s(r_p_m, r_a_m, at_apogee=True)
    a_t = 0.5 * (r_p_m + r_a_m)
    v_apogee = _ellipse_velocity_km_s(r_a_m, a_t)
    v_circ = math.sqrt(MU_EARTH_M3_S2 / r_a_m) / 1000.0
    plane = plane_change_km_s(v_circ, inclination_deg)
    cos_i = math.cos(math.radians(inclination_deg))
    vector = math.sqrt(max(v_circ**2 + v_apogee**2 - 2.0 * v_circ * v_apogee * cos_i, 0.0))
    scalar_sum = circularization + plane
    return ApogeeComposite(
        circularization_km_s=circularization,
        plane_change_km_s=plane,
        vector_km_s=vector,
        scalar_sum_km_s=scalar_sum,
        inclination_deg=inclination_deg,
        assumption=(
            f"远地点复合机动（圆化+平面变更）矢量合成 Δv=√(v₁²+v₂²−2v₁v₂·cos Δi)"
            f"（§8.10 约束 2，禁止标量相加）：μ={MU_EARTH_M3_S2:.9e} m³/s²（WGS-84）、"
            f"r_a={r_a_m:.1f} m、Δi={inclination_deg:.2f}°；理想脉冲口径，不含有限推力损失"
        ),
    )


def geo_via_gto_km_s(
    r_p_m: float, inclination_deg: float, *, r_geo_m: float = GEO_RADIUS_M
) -> GeoViaGto:
    """GEO 路线：近地点点火入 GTO → 远地点圆化（§8.10 表「工程上最常见路线」）。

    两脉冲 = Hohmann 第一脉冲（近地点）+ 远地点复合机动（圆化 + 消倾角矢量合成，
    :func:`apogee_composite_km_s`）。GEO 直送同口径（同一公式族、同一 μ / r_p；
    直送只是两脉冲连续施加、无 GTO 驻留——ΔV 差别在损失侧，由 §8.6 覆盖）。
    """
    _validate_radius(r_geo_m, "GEO 半径")
    if r_geo_m <= r_p_m:
        raise PerfError(
            f"GEO 半径必须大于停泊轨道半径：r_geo={r_geo_m:.1f} m ≤ r_p={r_p_m:.1f} m",
            suggestion="停泊轨道为 LEO（r_p ≈ R_地球 + 200 km），GEO 半径 ≈ 4.216×10⁷ m",
        )
    transfer = hohmann_transfer_km_s(r_p_m, r_geo_m)
    apogee = apogee_composite_km_s(r_p_m, r_geo_m, inclination_deg)
    return GeoViaGto(
        perigee_kick_km_s=transfer.dv1_km_s,
        apogee=apogee,
        total_km_s=transfer.dv1_km_s + apogee.vector_km_s,
        assumption=(
            f"GEO（GTO+远地点圆化）两脉冲路线（§8.10）：μ={MU_EARTH_M3_S2:.9e} m³/s²"
            f"（WGS-84）、停泊轨道 r_p={r_p_m:.1f} m、GEO 半径 {r_geo_m:.1f} m"
            f"（R_地球+35 786 km）、Δi={inclination_deg:.2f}°；"
            "理想脉冲口径，不含有限推力损失（§8.10 约束 1）"
        ),
    )


# ---------------------------------------------------------------------------
# C3 ↔ ΔV 双向一致（OI-23 闭式；§8.10 约束 2 的 M6 验收判据）
# ---------------------------------------------------------------------------


def delta_v_from_c3_km_s(c3_km2_s2: float, r_p_m: float) -> float:
    """C3 → 停泊轨道单脉冲 ΔV [km/s]（§8.10 OI-23 闭式）。

    .. code-block:: text

        v_p = √(2μ/r_p + C3)        ← 转移轨道在停泊半径处的速度
        Δv  = v_p − √(μ/r_p)        ← 圆轨道射入脉冲

    定义域：``C3 > −μ/r_p``（否则 v_p ≤ v_circ、Δv ≤ 0——比停泊圆轨道更低的
    能量无需「射入」脉冲；C3 < −2μ/r_p 时 v_p 无实解）。与
    :func:`c3_from_delta_v_km2_s2` 用**同一组 μ / r_p** 互逆（§8.10 约束 2 的
    双向一致性，回归测试以机器精度钉死）。
    """
    _validate_radius(r_p_m, "停泊轨道半径 r_p")
    c3_m2_s2 = c3_km2_s2 * 1e6
    two_mu_over_rp = 2.0 * MU_EARTH_M3_S2 / r_p_m
    if c3_m2_s2 < -two_mu_over_rp:
        raise PerfError(
            f"C3={c3_km2_s2} km²/s² 低于 −2μ/r_p={-two_mu_over_rp / 1e6:.3f} km²/s²"
            f"（r_p={r_p_m:.1f} m）：v_p 无实解（能量低于近地点 = r_p 的退化椭圆）",
            suggestion="C3 可行域为 (−μ/r_p, +∞)；TLI 典型 −2.0 至 −1.3、"
            "TMI 典型 8–15 km²/s²（§8.6 表注）",
        )
    v_p = math.sqrt(two_mu_over_rp + c3_m2_s2)
    v_circ = math.sqrt(MU_EARTH_M3_S2 / r_p_m)
    dv = v_p - v_circ
    if dv <= 0.0:
        raise PerfError(
            f"C3={c3_km2_s2} km²/s² 在 r_p={r_p_m:.1f} m 处不产生正向射入脉冲"
            f"（Δv={dv / 1000.0:.3f} km/s ≤ 0；可行域 C3 > −μ/r_p="
            f"{-MU_EARTH_M3_S2 / r_p_m / 1e6:.3f} km²/s²）",
            suggestion="射入脉冲要求转移轨道能量高于停泊圆轨道（C3 > −μ/r_p 且 v_p > v_circ）",
        )
    return dv / 1000.0


def c3_from_delta_v_km2_s2(dv_km_s: float, r_p_m: float) -> float:
    """ΔV → C3 [km²/s²]（OI-23 反向闭式）：``C3 = (√(μ/r_p) + Δv)² − 2μ/r_p``。

    与 :func:`delta_v_from_c3_km_s` 同一组 μ / r_p、互逆（round-trip 机器精度，
    回归测试独立书写能量方程对拍两侧——两侧任一处硬编码即失配）。
    """
    _validate_radius(r_p_m, "停泊轨道半径 r_p")
    if not (dv_km_s > 0.0):
        raise PerfError(
            f"射入 ΔV 必须为正，收到 {dv_km_s} km/s",
            suggestion="从停泊圆轨道射入转移/逃逸轨道的脉冲恒为正向加速",
        )
    v_p_m_s = math.sqrt(MU_EARTH_M3_S2 / r_p_m) + dv_km_s * 1000.0
    return (v_p_m_s**2 - 2.0 * MU_EARTH_M3_S2 / r_p_m) / 1e6


# ---------------------------------------------------------------------------
# 转移射入（TLI / TMI / escape；§8.10 表 OI-22 两行 + 逃逸行）
# ---------------------------------------------------------------------------


def parking_injection_km_s(
    target: InjectionTarget,
    r_p_m: float,
    *,
    c3_km2_s2: float | None = None,
    window_assumption: str | None = None,
) -> ParkingInjection:
    """停泊轨道单脉冲转移射入（§8.10）：**C3 与 ΔV 成对输出**（OI-22 约束 1）。

    - **TLI**：C3 缺省取 :data:`TLI_C3_TYPICAL_KM2_S2`（−1.65，地月转移惯例区间
      −2.0 至 −1.3 的中值）——**输出 C3 < 0**（地心束缚），ΔV 由 C3 反算；
    - **TMI**：C3 缺省取 :data:`TMI_C3_TYPICAL_KM2_S2`（11.5，§8.6 表注区间 8–15
      中值）；``window_assumption`` **必填**——未传时取
      :data:`TMI_WINDOW_ASSUMPTION_DEFAULT`，传入空串按缺省处理（禁止无窗口假设
      的 TMI 输出，§8.10 约束 4 / CON-04）；
    - **escape**：C3 = v∞²（缺省 0 = 抛物线逃逸），由双曲线超速反推。

    ``c3_km2_s2`` 显式传入时覆写典型值（如指定窗口的 TMI C3）——TLI 要求 < 0、
    escape 要求 ≥ 0，越域显式拒绝。
    """
    _validate_radius(r_p_m, "停泊轨道半径 r_p")
    if target == "TLI":
        c3 = TLI_C3_TYPICAL_KM2_S2 if c3_km2_s2 is None else c3_km2_s2
        if c3 >= 0.0:
            raise PerfError(
                f"TLI 的 C3 必须为负（地心束缚轨道），收到 {c3} km²/s²",
                suggestion="TLI 目标 C3 取地月转移惯例区间 −2.0 至 −1.3 km²/s²（OI-22）；"
                "正 C3 属逃逸/行星转移，改用 target='TMI' 或 'escape'",
            )
        window: str | None = None
        c3_note = (
            f"C3={c3:.2f} km²/s²（地月转移惯例区间 −2.0 至 −1.3 的中值，"
            "公开工程惯例、非权威——束缚轨道为负）"
        )
    elif target == "TMI":
        c3 = TMI_C3_TYPICAL_KM2_S2 if c3_km2_s2 is None else c3_km2_s2
        text = window_assumption if window_assumption else TMI_WINDOW_ASSUMPTION_DEFAULT
        window = text
        c3_note = (
            f"C3={c3:.2f} km²/s²（§8.6 表注典型区间 8–15 的中值；窗口强依赖，"
            "窗口/相位假设见 window_assumption 必填字段——§8.10 约束 4）"
        )
    elif target == "escape":
        c3 = 0.0 if c3_km2_s2 is None else c3_km2_s2
        if c3 < 0.0:
            raise PerfError(
                f"逃逸轨道的 C3（= v∞²）必须非负，收到 {c3} km²/s²",
                suggestion="负 C3 是束缚轨道（TLI/TMI）；逃逸请给 v∞² ≥ 0（抛物线为 0）",
            )
        window = None
        c3_note = f"C3={c3:.2f} km²/s²（= v∞²，由双曲线超速反推；0 为抛物线逃逸）"
    else:  # pragma: no cover - Literal 类型已在契约层拦截
        raise PerfError(
            f"未知转移射入目标 {target!r}",
            suggestion="target ∈ {TLI, TMI, escape}（§8.10 表）",
        )
    dv = delta_v_from_c3_km_s(c3, r_p_m)
    return ParkingInjection(
        dv_km_s=dv,
        c3_km2_s2=c3,
        window_assumption=window,
        assumption=(
            f"停泊轨道单脉冲转移射入（{target}，§8.10）：μ={MU_EARTH_M3_S2:.9e} m³/s²"
            f"（WGS-84）、停泊半径 r_p={r_p_m:.1f} m、{c3_note}；"
            "ΔV 由 C3 反算（v_p=√(2μ/r_p+C3)，与 C3 同组 μ/r_p——双向一致）；"
            "理想脉冲口径，不含有限推力损失（§8.10 约束 1）"
        ),
    )


__all__ = [
    "GEO_RADIUS_M",
    "MU_EARTH_M3_S2",
    "PARKING_DEFAULT_ALT_M",
    "R_EARTH_EQUATOR_M",
    "TMI_WINDOW_ASSUMPTION_DEFAULT",
    "ApogeeComposite",
    "GeoViaGto",
    "HohmannTransfer",
    "InjectionTarget",
    "ParkingInjection",
    "apogee_composite_km_s",
    "c3_from_delta_v_km2_s2",
    "circular_velocity_km_s",
    "circularization_km_s",
    "delta_v_from_c3_km_s",
    "geo_via_gto_km_s",
    "hohmann_transfer_km_s",
    "parking_injection_km_s",
    "plane_change_km_s",
]
