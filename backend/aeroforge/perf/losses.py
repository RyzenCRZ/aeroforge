"""弹道损失层 L1（规格 §8.6，M4 计算内核第三片）。

四项损失 + 自转加成 + 目标轨道 ΔV 需求（§8.6 表）
--------------------------------------------------
全部为**参数化经验模型（L1）**：系数来自公开工程的量级经验（工程惯例、非权威
来源）——每个系数的注释都标明这一属性，**禁止**把本层数值当作标准或论文结论
引用（§8.6 表下警告原文）。M6 的 L2 简化上升弹道积分（指数大气 + 重力转弯）
将把「经验系数」升级为「物理模型」，届时收窄区间。

字段命名纪律（§8.8 警告）
--------------------------
``back_pressure_loss_km_s`` 即 §8.6 L1 表中的「压力/控制余量」——同一件事的两个
名字（用户核心目标称"背压损失"），实现**只保留一个字段**，禁止两处各算一份。

方位角—倾角相容性（§8.6 约束 2）
--------------------------------
自转加成的相容因子与转向损失**由同一个倾角失配量驱动**（:func:`_inclination_mismatch_deg`
单点计算、两侧共用）：需要大倾角却向东发射时，加成被相容因子削减，且**必须同时**
产生转向损失增量——二者是同一件事的两面，不得只算加成不算损失。

理想 ΔV 与特征能量（OI-23）
---------------------------
:func:`ideal_orbit_dv_km_s` 给轨道力学理论速度增量（WGS-84 常数，真空脉冲口径），
是 ΔV 瀑布（§8.8 ``delta_v_budget``）的起点；:func:`characteristic_energy_km2_s2`
给终态轨道的 C3 = −μ/a（束缚轨道为负，逃逸为 0）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from aeroforge.errors import PerfError
from aeroforge.params.schema import LaunchSite, Mission, OrbitType

# ---------------------------------------------------------------------------
# 地球常量（WGS-84；来源：WGS-84 世界大地测量系定义值）
# ---------------------------------------------------------------------------

#: 地球自转角速度 [rad/s]（WGS-84：7.292115×10⁻⁵）。
OMEGA_EARTH_RAD_S = 7.292115e-5

#: 地球赤道半径 [m]（WGS-84 长半轴 a = 6 378 137 m）。
EARTH_EQUATOR_RADIUS_M = 6_378_137.0

#: 地心引力常数 [m³/s²]（WGS-84 GM = 3.986004418×10¹⁴）。
EARTH_GM_M3_S2 = 3.986004418e14

# ---------------------------------------------------------------------------
# 损失模型标定域（全部为工程惯例系数、非权威来源——§8.6 L1 的本质）
# ---------------------------------------------------------------------------

#: 重力损失：起飞推重比标定域（现役运载典型区间；工程惯例，非权威来源）。
_TWR_LOW, _TWR_HIGH = 1.2, 1.6

#: 重力损失：一级燃时参考值 [s]（现役中型运载典型量级；工程惯例，非权威来源）。
_BURN_TIME_REF_S = 160.0

#: 气动损失：长径比标定域（工程惯例，非权威来源）。
_AERO_LD_LOW, _AERO_LD_HIGH = 8.0, 15.0

#: 气动损失：参考阻力系数与参考截面积（Cd≈0.3、F9 芯级截面 ≈10.75 m²；工程惯例锚）。
_AERO_CD_REF = 0.3
_AERO_AREA_REF_M2 = math.pi * 1.85**2

#: 气动损失：Aero 层缺失时的默认 Cd（§6.1 口径：缺失按默认值并附 warning）。
DEFAULT_DRAG_COEFFICIENT = 0.3

#: 背压损失：海平面发动机参考膨胀比（Merlin 1D / RD 系典型值；工程惯例锚）。
_BACK_PRESSURE_EPS_REF = 16.0

#: 发射场缺失时的默认场（工程惯例锚：卡纳维拉尔角 28.5°N、向东）。
DEFAULT_LAUNCH_SITE = LaunchSite(
    name="默认发射场（工程惯例：卡纳维拉尔角 28.5°N）",
    latitude_deg=28.5,
    altitude_m=3.0,
    azimuth_deg=90.0,
)

# ---------------------------------------------------------------------------
# §8.6 目标轨道 ΔV 需求表的锚点（量级锚定、非权威——禁止当标准引用）
# ---------------------------------------------------------------------------

#: 纬度分档锚点 [deg]：库鲁（GTO 最优，§8.6 表）与卡纳维拉尔角（表中"高纬发射场"）。
_LOW_LAT_ANCHOR_DEG = 5.2
_HIGH_LAT_ANCHOR_DEG = 28.5

#: 四目标锚定值 (低纬, 高纬) [km/s]。GTO 两档直取表中值；LEO 高纬行、SSO 低纬行、
#: GEO 高纬行表未给出——按工程惯例外推并在 :func:`orbit_dv_requirement` 的来源文案
#: 中声明（量级锚定，非权威）。
_DV_ANCHORS_KM_S: dict[str, tuple[float, float]] = {
    "LEO": (9.40, 9.65),  # 表低纬行 9.3–9.5 中值；高纬 +0.25（工程外推：倾角 dogleg）
    "SSO": (9.90, 9.75),  # 表单行 9.5–10.0 中值 9.75；低纬发极轨多付转向 → +0.15（工程外推）
    "GTO": (11.55, 12.30),  # 表两行中值：库鲁 11.3–11.8 / 高纬 12.0–12.6
    "GEO": (14.65, 15.40),  # 表单行 14.3–15.0 中值；高纬按 GTO 同源纬度惩罚外推 +0.75
}

#: 需求表来源文案（dv_source 的锚定档）。
DV_SOURCE_ANCHORED = "量级锚定（§8.6 表中值）"

#: 轨道要素缺省值（工程惯例剖面；M6 轨道层可按任务要素收窄）。
_DEFAULT_LEO_ALT_M = 200_000.0
_DEFAULT_SSO_ALT_M = 700_000.0
_GEO_ALT_M = 35_786_000.0
_MOON_DISTANCE_M = 384_400_000.0

#: payload_by_orbit 的四个目标（OI-38；TLI / TMI 须与 C3 成对，随 M6 轨道层交付）。
CAPACITY_ORBITS: tuple[str, ...] = ("LEO", "SSO", "GTO", "GEO")


@dataclass(frozen=True, slots=True)
class LossItem:
    """一项损失的 L1 计算结果：值 + 越域警告 + 模型假设文案。"""

    value_km_s: float
    """损失值 [km/s]（L1 参数化经验模型，工程惯例系数、非权威来源）。"""

    warning: str | None = None
    """输入越出标定域时的显式警告（不静默外推）。"""

    assumption: str = ""
    """该损失项的模型假设与系数来源（进入 delta_v_budget.assumptions）。"""


@dataclass(frozen=True, slots=True)
class DvRequirement:
    """一个目标轨道的 ΔV 需求（含损失、含发射场纬度依赖）。"""

    value_km_s: float
    source: str
    """需求来源：量级锚定（§8.6 表中值）/ Mission 用户输入。"""


# ---------------------------------------------------------------------------
# 四项损失（§8.6 L1 表逐行）
# ---------------------------------------------------------------------------


def gravity_loss(twr: float, burn_time_s: float) -> LossItem:
    """重力损失（§8.6 L1：典型 1.0–1.5 km/s）。

    - 推重比↑（推力充足、重力转弯快）⟹ 损失↓：基值在 TWR∈[1.2, 1.6] 内由
      1.5 线性降至 1.0 km/s（系数为工程惯例、非权威来源）；
    - 一级燃时↑（在重力场中烧得更久）⟹ 损失↑：以 160 s 为参考（现役中型运载
      一级典型燃时，工程惯例）做 ±15% 封顶的线性修正；
    - TWR 越出 [1.2, 1.6] 标定域：取边界值并**显式 warning**（不静默外推）。
    """
    clamped = min(max(twr, _TWR_LOW), _TWR_HIGH)
    base = 1.5 - 0.5 * (clamped - _TWR_LOW) / (_TWR_HIGH - _TWR_LOW)
    time_factor = min(max(burn_time_s / _BURN_TIME_REF_S, 0.85), 1.15)
    warning = None
    if not (_TWR_LOW <= twr <= _TWR_HIGH):
        warning = (
            f"重力损失模型在 TWR∈[{_TWR_LOW}, {_TWR_HIGH}] 内标定，当前 TWR={twr:.3f} "
            "越出该域——取边界值参与计算，结果仅供参考（§8.6 L1 为参数化经验模型）"
        )
    return LossItem(
        value_km_s=base * time_factor,
        warning=warning,
        assumption=(
            f"重力损失：L1 参数化（TWR={twr:.3f}、一级燃时 {burn_time_s:.0f} s；"
            "系数工程惯例、非权威来源，§8.6 L1）"
        ),
    )


def aero_loss(length_diameter_ratio: float, cd: float, a_ref_m2: float) -> LossItem:
    """气动损失（§8.6 L1：典型 0.1–0.3 km/s）。

    - 长径比↑（细长构型穿大气更快）⟹ 损失↓：L/D∈[8, 15] 内由 0.3 线性降至
      0.1 km/s（工程惯例系数、非权威来源）；
    - Cd 与参考面积按锚值（0.3 / F9 芯级截面 10.75 m²）线性与开方缩放；
    - 最终值夹在 [0.05, 0.45] km/s（表典型范围外留工程余量，越界仅由极端
      Cd / 面积组合触发）。
    """
    ld_factor = min(
        max((_AERO_LD_HIGH - length_diameter_ratio) / (_AERO_LD_HIGH - _AERO_LD_LOW), 0.0),
        1.0,
    )
    base = 0.1 + 0.2 * ld_factor
    value = base * (cd / _AERO_CD_REF) * math.sqrt(a_ref_m2 / _AERO_AREA_REF_M2)
    return LossItem(
        value_km_s=min(max(value, 0.05), 0.45),
        assumption=(
            f"气动损失：长径比 {length_diameter_ratio:.1f} / Cd={cd:.2f} / "
            f"A_ref={a_ref_m2:.1f} m² 参数化（工程惯例、非权威来源，§8.6 L1）"
        ),
    )


def _azimuth_deviation_deg(azimuth_deg: float) -> float:
    """方位角与正东（90°）的偏差 [deg]，取 0–180。"""
    return abs(((azimuth_deg - 90.0 + 180.0) % 360.0) - 180.0)


def _inclination_mismatch_deg(
    latitude_deg: float, azimuth_deg: float, inclination_deg: float | None
) -> float:
    """倾角失配量 [deg]：目标倾角超出「当前纬度 + 方位角直接可达倾角」的部分。

    可达倾角由球面三角 ``cos(i) = cos(lat)·sin(az)`` 给出；``inclination_deg=None``
    表示用户未指定倾角（按当前方位角的自然倾角理解）⟹ 失配 0。**该量是相容因子
    与转向损失的同一驱动源**（§8.6 约束 2：加成削减与损失增量是同一件事的两面）。
    """
    if inclination_deg is None:
        return 0.0
    cos_i = math.cos(math.radians(latitude_deg)) * math.sin(math.radians(azimuth_deg))
    reachable = math.degrees(math.acos(min(max(cos_i, -1.0), 1.0)))
    return max(0.0, inclination_deg - reachable)


def compatibility_factor(
    latitude_deg: float, azimuth_deg: float, inclination_deg: float | None
) -> float:
    """自转加成的相容因子（§8.6 公式）：向东且倾角≈可达倾角取 1，失配越大削减越多。

    ``factor = clamp(1 − 失配/90°, 0, 1)``（系数工程惯例、非权威来源）——需要大
    倾角却向东发射时加成被削减，且 :func:`steering_loss` 同时产生同向增量。
    """
    mismatch = _inclination_mismatch_deg(latitude_deg, azimuth_deg, inclination_deg)
    return min(max(1.0 - mismatch / 90.0, 0.0), 1.0)


def steering_loss(
    latitude_deg: float, azimuth_deg: float, inclination_deg: float | None
) -> LossItem:
    """转向损失（§8.6 L1：典型 0.1–0.5 km/s）。

    - 基值 0.1 km/s（向东、自然倾角发射的工程底）；
    - 方位角偏离正东的机动惩罚：0.3×(偏差/180°)（正南发射极轨约 +0.15）；
    - 倾角失配惩罚：0.2×(失配/90°)——与 :func:`compatibility_factor` 的削减
      **同一驱动源**（§8.6 约束 2，二者同向、不得只算其一）；
    - 全部系数为工程惯例、非权威来源；总值夹在 [0.1, 0.5] km/s。
    """
    az_dev = _azimuth_deviation_deg(azimuth_deg)
    mismatch = _inclination_mismatch_deg(latitude_deg, azimuth_deg, inclination_deg)
    value = 0.1 + 0.3 * min(az_dev / 180.0, 1.0) + 0.2 * min(mismatch / 90.0, 1.0)
    return LossItem(
        value_km_s=min(max(value, 0.1), 0.5),
        assumption=(
            f"转向损失：方位角 {azimuth_deg:.0f}°（偏离正东 {az_dev:.0f}°）与倾角失配 "
            f"{mismatch:.1f}° 参数化（工程惯例、非权威来源，§8.6 L1）"
        ),
    )


def back_pressure_loss(expansion_ratio: float) -> LossItem:
    """背压损失（§8.6 L1「压力/控制余量」：典型 0.1–0.2 km/s）。

    ⚠ 字段名唯一 ``back_pressure_loss_km_s``（§8.8 警告）：即 §8.6 表中的
    「压力/控制余量」，同一件事的两个名字——实现只保留一个字段，禁止两处各算一份。

    按一级喷管膨胀比参数化：海平面发动机膨胀比越大、低空过度膨胀越显著——以
    ε=16 为锚取 0.15 km/s，ε 每偏 40 线性 ±0.05，夹在 [0.1, 0.2]（工程惯例、
    非权威来源）。
    """
    value = 0.15 + 0.05 * (expansion_ratio - _BACK_PRESSURE_EPS_REF) / 40.0
    return LossItem(
        value_km_s=min(max(value, 0.1), 0.2),
        assumption=(
            f"背压损失（即 §8.6「压力/控制余量」，§8.8 单一字段）：按一级喷管膨胀比 "
            f"ε={expansion_ratio:.0f} 参数化（工程惯例、非权威来源）"
        ),
    )


# ---------------------------------------------------------------------------
# 自转加成（§8.6 公式）
# ---------------------------------------------------------------------------


def rotation_assist_km_s(
    latitude_deg: float,
    altitude_m: float,
    azimuth_deg: float,
    inclination_deg: float | None,
) -> float:
    """发射场自转加成 [km/s]（§8.6 公式，可为负——向西发射为逆向罚项）。

    ``v_rot = ω·(R + 海拔)·cos(纬度)``（发射场随地球自转的线速度；ω、R 取
    WGS-84）；``Δv_assist = v_rot·cos(方位角)·相容因子``（向东取最大，相容因子
    见 :func:`compatibility_factor`——方位角与目标倾角不相容时削减，且转向损失
    同向增加，§8.6 约束 2）。

    ⚠ 角度基准换算：§8.6 公式的 ``cos(方位角)`` 以**正东为 0° 基准**（数学约定，
    "向东发射取最大"由此成立）；而 ``LaunchSite.azimuth_deg`` 是**罗盘方位角**
    （自正北顺时针，§6.1）。发射方向的东向分量 = ``sin(罗盘方位角)``——正东
    90° 取 1、正北 0° 取 0、正西 270° 取 −1，直接拿罗盘角喂 ``cos`` 会把"向东
    满加成"算成零（物理上荒谬的静默错误）。
    """
    v_rot = (
        OMEGA_EARTH_RAD_S
        * (EARTH_EQUATOR_RADIUS_M + altitude_m)
        * math.cos(math.radians(latitude_deg))
    )
    compat = compatibility_factor(latitude_deg, azimuth_deg, inclination_deg)
    eastward = math.sin(math.radians(azimuth_deg))
    return v_rot * eastward * compat / 1000.0


# ---------------------------------------------------------------------------
# 目标轨道 ΔV 需求（§8.6 表：量级锚定、非权威）与理想 ΔV（轨道力学理论值）
# ---------------------------------------------------------------------------


def orbit_dv_requirement(orbit: OrbitType | str, site: LaunchSite) -> DvRequirement:
    """目标轨道 ΔV 需求 [km/s]（含损失、含发射场纬度依赖——§8.6 约束 3）。

    取 §8.6 表的**区间中值**为锚，按发射场纬度在线鲁—卡角两档锚点间线性插值
    （两端 clamp）：低纬向东发射运力更高（FR-19）由此保证。**量级锚定、非权威**
    ——M6 的 L2 弹道积分将收窄区间；禁止把本值当标准或论文结论引用。

    仅覆盖 OI-38 的四目标（LEO / SSO / GTO / GEO 直送）；TLI / TMI 的 ΔV 与 C3
    成对（§8.10），随 M6 轨道层交付——本层显式拒绝而不是给一个缺 C3 的裸值。
    """
    key = str(orbit)
    anchors = _DV_ANCHORS_KM_S.get(key)
    if anchors is None:
        raise PerfError(
            f"轨道类型 {key!r} 不在 §8.6 需求表覆盖范围（LEO/SSO/GTO/GEO）；"
            "TLI/TMI 的 ΔV 必须与 C3 成对输出，随 M6 轨道层交付",
            suggestion="运力表目标固定为 LEO / SSO / GTO / GEO（直送）四个（OI-38）",
        )
    low, high = anchors
    span = _HIGH_LAT_ANCHOR_DEG - _LOW_LAT_ANCHOR_DEG
    t = min(max((site.latitude_deg - _LOW_LAT_ANCHOR_DEG) / span, 0.0), 1.0)
    return DvRequirement(
        value_km_s=low + t * (high - low),
        source=DV_SOURCE_ANCHORED,
    )


def _orbit_elements(orbit: str, mission: Mission) -> tuple[float, float]:
    """(r_p, r_a) [m]：轨道要素缺省值按工程惯例剖面补齐。"""
    perigee = mission.perigee_altitude_m or _DEFAULT_LEO_ALT_M
    if orbit in {"LEO", "SSO", "custom"}:
        if orbit == "custom" and mission.altitude_m is None:
            raise PerfError(
                "custom 轨道必须给出 mission.altitude_m 才能计算理想 ΔV",
                suggestion="补齐圆轨道高度，或改用 LEO/SSO/GTO/GEO 等定型轨道",
            )
        default_alt = _DEFAULT_SSO_ALT_M if orbit == "SSO" else _DEFAULT_LEO_ALT_M
        radius = EARTH_EQUATOR_RADIUS_M + (mission.altitude_m or default_alt)
        return radius, radius
    apogees = {"GTO": _GEO_ALT_M, "GEO": _GEO_ALT_M, "TLI": _MOON_DISTANCE_M}
    apogee = mission.apogee_altitude_m or apogees.get(orbit, _GEO_ALT_M)
    return EARTH_EQUATOR_RADIUS_M + perigee, EARTH_EQUATOR_RADIUS_M + apogee


def ideal_orbit_dv_km_s(orbit: OrbitType | str, mission: Mission) -> float:
    """理想轨道速度增量 [km/s]（真空脉冲口径、WGS-84 常数；不含任何损失）。

    - LEO / SSO / custom：圆轨道速度 ``√(μ/r)``；
    - GTO / TLI：转移椭圆近地点速度 ``√(μ(2/r_p − 1/a))``；
    - GEO（直送）：GTO 近地点速度 + 远地点圆化 ΔV（两脉冲理想分解）；
    - escape / TMI：逃逸速度口径（TMI 强窗口依赖、主变量是 C3——此处为工程
      近似，M6 轨道层须与 C3 一并收窄，§8.6 表注）。
    """
    key = str(orbit)
    r_p, r_a = _orbit_elements(key, mission)
    if key in {"escape", "TMI"}:
        return math.sqrt(2.0 * EARTH_GM_M3_S2 / r_p) / 1000.0
    semi_major = (r_p + r_a) / 2.0
    v_perigee = math.sqrt(EARTH_GM_M3_S2 * (2.0 / r_p - 1.0 / semi_major))
    if key == "GEO":
        # 直送：进入转移椭圆（近地点脉冲）+ 远地点圆化（第二脉冲）
        v_apogee = math.sqrt(EARTH_GM_M3_S2 * (2.0 / r_a - 1.0 / semi_major))
        v_circ_apogee = math.sqrt(EARTH_GM_M3_S2 / r_a)
        return (v_perigee + v_circ_apogee - v_apogee) / 1000.0
    return v_perigee / 1000.0


def characteristic_energy_km2_s2(orbit: OrbitType | str, mission: Mission) -> float:
    """终态轨道特征能量 C3 = v∞² [km²/s²]（OI-23；束缚轨道为负，抛物线逃逸为 0）。

    统一半长轴口径：``C3 = −μ/a``（圆轨道 a = r）。TLI / TMI / 逃逸轨道必输出
    本值（其 ΔV 是窗口强依赖的，单看 ΔV 会误导，§8.8 OI-23 约束）。TMI 本片按
    escape 口径工程近似（:func:`ideal_orbit_dv_km_s`）⟹ C3=0；真实 TMI 的 C3
    典型 8–15 km²/s² 且强窗口依赖（§8.6 表注），随 M6 轨道层与 C3 成对交付。
    """
    key = str(orbit)
    if key in {"escape", "TMI"}:
        return 0.0
    r_p, r_a = _orbit_elements(key, mission)
    return -EARTH_GM_M3_S2 / ((r_p + r_a) / 2.0) / 1e6


__all__ = [
    "CAPACITY_ORBITS",
    "DEFAULT_DRAG_COEFFICIENT",
    "DEFAULT_LAUNCH_SITE",
    "DV_SOURCE_ANCHORED",
    "EARTH_EQUATOR_RADIUS_M",
    "EARTH_GM_M3_S2",
    "OMEGA_EARTH_RAD_S",
    "DvRequirement",
    "LossItem",
    "aero_loss",
    "back_pressure_loss",
    "characteristic_energy_km2_s2",
    "compatibility_factor",
    "gravity_loss",
    "ideal_orbit_dv_km_s",
    "orbit_dv_requirement",
    "rotation_assist_km_s",
    "steering_loss",
]
