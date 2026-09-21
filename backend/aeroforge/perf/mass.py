"""质量估算层（规格 §8.4，M4 计算内核第二片）。

双来源 + 交叉校验（§8.4 表逐行）
--------------------------------
- **几何解析**：柱段 + 椭球封头的贮箱容积 / 湿面积 × 面密度。纯数值解析公式，
  **不碰 OCCT**（几何内核属 M5 装配树，本层只做质量量级的工程估算）。
- **统计回归**：GCAT stages 表按（级序位置、推进剂大类）分箱的 σ 中位数 + 分位
  区间。**只作对照 / 缺省建议，不覆盖用户输入的 σ**（σ 是存储权威，§6.1）。
- **交叉校验**：几何解析干重 vs 用户 σ 推算干重，偏差 > 20% 报警（§8.4 一致性
  校验）——**单来源不得作为结论**（AGENTS 领域规则）。

几何模型口径（工程惯例估算，非权威）
------------------------------------
- 封头 = 半椭球，矢高 ``h = 扁度系数 × 直径 / 2``（OI-37；None 兜底 0.5，
  即 2:1 椭圆封头 → h = d/4，与压力容器标准 2:1 封头口径一致）。
- 封头表面积用 Knud Thomsen 近似（p = 1.6075，误差 < 1.5%，半球 / 圆盘
  退化极限均正确）。
- 两箱柱段长度：``Tank.length_m`` 显式给定 → 用户权威（§5.9 派生规则 2，不静默
  覆盖）；缺失 → 按 §5.9 容积比 ``V_ox/V_fuel = (O/F)·(ρ_fuel/ρ_ox)`` 分配
  **可用长度**（级长 − 发动机高度 − 分区轴向预留）。Schema 的 Tank 层**不存容积**
  （派生量不存储是 §6.1 唯一权威原则），用户显式通道只有 ``length_m``，故优先级为：
  显式箱长 > §5.9 容积比派生。
- **分区轴向预留（M5 第二片裁定，§5.9）**：九段分区的各段高度是**几何事实**，
  本账的贮箱容积必须与装配树消费**同一份分区高度**（燃料/氧箱柱长 = 分区里的
  箱段高）——``resolve_tank_geometry`` 的 ``reserved_m`` 缺省即取
  :func:`partition_reserved_m`（下箱底封头 + 第 6 分区 + 上箱顶封头 + 仪器舱），
  使「推进剂质量（几何）」与「装配树质量贡献」同源；σ 回归干重保持独立（统计
  来源，§8.4）。显式传 ``reserved_m=0.0`` 可退回旧口径（仅限对照）。
- 推进剂质量 = 氧箱容积 × ρ_ox + 燃料箱容积 × ρ_fuel，再乘**级层**加注比例
  （QA-2：级层加注比例是 M4 定尺求解的整体输入）。
- **几何干重（M6 前置专项①，分部位物理模型）**：逐级干重的清算式为

  ``m_dry = (Σ_箱 A_湿·ρ·t + m_隔板 + m_发动机) / (1 − Σf_非贮箱)``

  其中贮箱壁厚取**两路物理推导的大者**再乘焊缝/加强框加成系数：

  - 承压路径 ``t_P = P·R/(σ_y·k)``（薄壁压力容器环向公式；P = 镇压力惯例值、
    k = 安全系数、σ_y = 材料库室温屈服）；
  - 轴压稳定路径 ``t_ax = √(P_ax / (2π·γ(t)·0.605·E))``（NASA SP-8007 薄壁圆柱
    轴压屈曲，经典小挠度解系数 0.605〔ν=0.3〕，折减系数 γ 按 SP-8007 经验式
    ``γ = 1 − 0.908(1 − e^(−φ))``、``φ = (1/16)√(R/t)`` 隐式迭代；轴压载荷以
    **本级推进剂重量**作量级代理——下箱偏不保守〔上级与载荷未入账〕、上箱偏
    保守，工程惯例近似并留痕）；大运载贮箱壁实际由轴压/屈曲主导尺寸，只按
    承压推导会低估一个量级（M5 收官片登记的 ~10 倍来源差的物理根因）。
  - 面密度 = ρ_material × t（刻意不用用户的 Tank.wall_thickness_m，保持几何
    来源独立于用户细观输入）。
  - 发动机质量 = 台数 × F_vac/(T/W·g₀)，T/W 按循环分档取文献惯例值。
  - 非贮箱部位（推力结构/机架、推进系统管路与增压、裙段/级间段/舱段/航电等
    固定件）按**占级干重质量分数**闭环：各部位质量 = f_i × m_dry，分数基准与
    贮箱壁结果按上式闭合（分配比例见 :data:`NON_TANK_MASS_FRACTIONS`）。

  **防循环验证红线**：本账全部参数取文献工程惯例（集中参数表见本模块常量区，
  逐项出处），GCAT σ 干重样本只作对照判据（holdout），**不进入本账标定回路**
  ——σ 回归账（本模块后半）保持独立，交叉校验才有判据效力。
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from aeroforge.data.models import EngineRecord
from aeroforge.data.repository import CatalogRepository
from aeroforge.params import propellants
from aeroforge.params.dag import G0, volumetric_ratio
from aeroforge.params.materials import MaterialEntry, get_material
from aeroforge.params.schema import Stage

#: OI-37：扁度系数缺省值（2:1 椭圆封头）。兜底发生在本层（派生处），Schema 存 None。
DEFAULT_FLATNESS_RATIO = 0.5

#: §8.4 一致性校验阈值：几何解析干重与 σ 推算干重偏差超过 20% 即报警。
CROSS_CHECK_THRESHOLD = 0.2

#: 回归分箱的最小样本数：低于该值的箱并入相邻箱（§8.4 回归要求 + 任务口径）。
MIN_BIN_SIZE = 5

# ---------------------------------------------------------------------------
# 分部位干重模型参数表（M6 前置专项①：集中定义、逐项出处）
# ---------------------------------------------------------------------------
# ⚠ 全部为「工程惯例非权威」量级，只用于 §8.4 几何解析干重账；GCAT σ 回归账
# （本模块后半）保持独立——σ 干重样本只作对照判据（holdout），不进入本表标定
# 回路（防循环验证红线）。改任一数值即视为质量模型版本变化：同步递增
# :data:`MASS_MODEL_VERSION`，使几何产物缓存键失效（§9.2 / cache.store）。


#: 质量模型版本（参与几何产物缓存键，见 cache.store.compute_vehicle_key）：
#: 分部位物理模型首发版。参数表变更时必须递增，防旧缓存命中旧质量账。
MASS_MODEL_VERSION = "m6-partwise-1"

#: 泵压式液体贮箱镇压力（箱底增压，Pa）：工程惯例中值（典型 2–3 bar；
#: Sutton《Rocket Propulsion Elements》贮箱增压口径；非权威）。
TANK_ULLAGE_PRESSURE_PA = 2.5e5

#: 承压路径安全系数 k（无量纲）：任务口径 1.25–1.5 取保守端 1.5（压力容器惯例）。
PRESSURE_SAFETY_FACTOR = 1.5

#: 焊缝/加强框加成系数（无量纲）：等效壁厚加成——搅拌摩擦焊焊缝增强与化铣
#: 加强框/壁板的工程惯例区间 1.2–1.5 取中值 1.35；非权威。
WELD_STIFFENER_FACTOR = 1.35

#: NASA SP-8007《Buckling of Thin-Walled Circular Cylinders》轴压屈曲：
#: 经典小挠度解系数 1/√(3(1−ν²)) = 0.605（ν = 0.3）、折减经验式系数 0.908 与
#: φ = (1/16)·√(R/t) 的分母 16。
SP8007_CLASSICAL_COEFF = 0.605
SP8007_KNOCKDOWN_COEFF = 0.908
SP8007_PHI_DIVISOR = 16.0

#: 发动机推重比 T/W（真空推力/自重，无量纲）按循环分档的工程惯例代表值：
#: 燃气发生器 80（公开发动机手册样本 F-1 94 / H-1 92 / RS-27 80 / J-2 59 /
#: RS-68 52 / Merlin 1D 213——区间 52–213 的中位档）；分级燃烧 75（YF-100 74 /
#: RD-180 77）；膨胀循环与挤压式 40（RL10 37–59 / AJ10 ≈37，小推力级轻构造）。
#: 非权威，仅作干重量级估算。
ENGINE_THRUST_TO_WEIGHT: dict[str, float] = {
    "gas_generator": 80.0,
    "staged_combustion": 75.0,
    "expander": 40.0,
    "pressure_fed": 40.0,
}

#: 非贮箱部位质量分数（占级干重比例；清算式各部位质量 = f_i × m_dry）：
#: 液体级干重构成的文献惯例区间中值——推力结构/机架 3–8% 取 4%、推进系统管路
#: 与增压 3–6% 取 4.5%、裙段/级间段/舱段/航电及其他固定件 8–12% 取 9%；
#: 合计 17.5%（惯例区间 14–26% 之内）。非权威。
NON_TANK_MASS_FRACTIONS: dict[str, float] = {
    "thrust_structure": 0.04,
    "plumbing_pressurization": 0.045,
    "skirts_avionics_misc": 0.09,
}

#: 非贮箱部位分数合计（清算式分母 1 − Σf）。
NON_TANK_FRACTION_TOTAL = sum(NON_TANK_MASS_FRACTIONS.values())

#: §8.4 回归要求「只用 official / literature 标签的样本」——其余 quality 值跳过并计数。
_ALLOWED_QUALITY = frozenset({"official", "literature"})

#: GCAT 中「氧化剂缺失但燃料为已知液体单组元」的燃料清单（它们不是固体）：
#: 双组元液体机必填氧化剂，固体与单组元都不填；不区分会把单组元上面级误入固体箱。
_MONOPROPELLANT_FUELS = frozenset({"Hydrazine", "Hydrazine?", "N2", "NH3OHNO3 AF-M315E"})

Position = Literal["booster", "first", "upper", "unknown", "pooled"]
"""样本的级序位置（由 stage_links 的 Stage_No 投票；pooled = 小箱合并兜底）。"""

PropellantClass = Literal["liquid", "solid", "unknown", "mixed"]
"""推进剂大类（由发动机氧化剂/燃料字段判定；mixed = 合并兜底箱）。"""

#: §8.4 的 σ 有效域（按 位置 × 大类）。助推器液体箱规格未单列——按一级液体口径
#: 对照并显式注明；unknown / pooled 箱无规格区间，只报告不判定。
SIGMA_INTERVALS: dict[tuple[str, str], tuple[float, float]] = {
    ("first", "liquid"): (0.04, 0.08),
    ("upper", "liquid"): (0.08, 0.15),
    ("booster", "liquid"): (0.04, 0.08),
    ("first", "solid"): (0.08, 0.12),
    ("upper", "solid"): (0.08, 0.12),
    ("booster", "solid"): (0.08, 0.12),
}

#: 分箱合并时的位置邻接序（同级最近箱按此距离选取）。
_POSITION_ORDER: dict[str, int] = {"booster": 0, "first": 1, "upper": 2, "unknown": 3}


# ---------------------------------------------------------------------------
# 几何解析（纯数值公式）
# ---------------------------------------------------------------------------


def dome_height_m(diameter_m: float, flatness_ratio: float | None) -> float:
    """封头矢高（m）：``h = 扁度系数 × d / 2``。

    扁度系数 = 封头椭球短轴 / 长轴（OI-37）。2:1 椭圆封头（系数 0.5）的矢高为
    d/4，与压力容器标准的 2:1 半椭圆封头一致；系数 1.0 退化为半球（h = d/2）。
    """
    ratio = DEFAULT_FLATNESS_RATIO if flatness_ratio is None else flatness_ratio
    return ratio * diameter_m / 2.0


def dome_volume_m3(diameter_m: float, dome_height_m_: float) -> float:
    """单个半椭球封头容积（m³）：``(2/3)·π·(d/2)²·h``。"""
    return 2.0 / 3.0 * math.pi * (diameter_m / 2.0) ** 2 * dome_height_m_


def dome_surface_area_m2(diameter_m: float, dome_height_m_: float) -> float:
    """单个半椭球封头表面积（m²），Knud Thomsen 近似（p = 1.6075，误差 < 1.5%）。

    旋转椭球取赤道半径 ``a = d/2``、极半轴 ``b = h``：整球面积
    ``S ≈ 4π·((2·(ab)^p + a^(2p))/3)^(1/p)``，封头取其半。退化极限自检：
    ``h = d/2``（半球）→ 2π(d/2)² 精确成立；``h → 0`` → π(d/2)²（圆盘）成立。
    """
    a = diameter_m / 2.0
    b = dome_height_m_
    p = 1.6075
    spheroid: float = 4.0 * math.pi * ((2.0 * (a**p) * (b**p) + a ** (2.0 * p)) / 3.0) ** (1.0 / p)
    return spheroid / 2.0


def tank_volume_m3(diameter_m: float, cylinder_length_m: float, dome_height_m_: float) -> float:
    """贮箱容积（m³）= 柱段 + 前后两个椭球封头。"""
    cylinder = math.pi * diameter_m**2 / 4.0 * cylinder_length_m
    return cylinder + 2.0 * dome_volume_m3(diameter_m, dome_height_m_)


def tank_wetted_area_m2(
    diameter_m: float, cylinder_length_m: float, dome_height_m_: float
) -> float:
    """贮箱湿面积（m²）= 柱段侧面积 + 两个封头表面积。"""
    cylinder = math.pi * diameter_m * cylinder_length_m
    return cylinder + 2.0 * dome_surface_area_m2(diameter_m, dome_height_m_)


# ---------------------------------------------------------------------------
# §5.9 九段分区的轴向高度事实（M5 第二片：装配树与 §8.4 账共源）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartitionHeights:
    """§5.9 九段分区的封头 / 中段 / 仪器舱高度（纯数值，无 OCCT）。

    装配树（:mod:`aeroforge.geometry.assembly` 的分段布局）与 §8.4 几何解析账
    （:func:`resolve_tank_geometry` 的轴向预留）共同消费这一份高度——分区高度是
    **几何事实**，两账不得各算一套（M5 第二片 reserved 口径差复核的裁定）。
    """

    lower_role: str
    """下箱角色（oxidizer / fuel，由储箱排列决定）。"""

    upper_role: str
    """上箱角色。"""

    diameter_lower_m: float
    """下箱直径（m）。"""

    diameter_upper_m: float
    """上箱直径（m）。"""

    dome_lower_m: float
    """下箱封头矢高（m）＝ 推力结构段高（容纳下箱底封头）。"""

    dome_upper_m: float
    """上箱封头矢高（m）＝ 前裙段高（容纳上箱顶封头）。"""

    h_mid_m: float
    """第 6 分区高（m）：非共底 = 级间舱；共底 = 隔板段。"""

    bulkhead_m: float | None
    """共底隔板矢高（m）；非共底为 ``None``。"""

    avionics_m: float
    """仪器舱高（m）：``Stage.avionics_height_m`` 显式值，缺省 0（§5.9 允许 0 高）。"""

    interstage_m: float
    """级间段高（m，§5.9 共性 2：**两级之间**的分离舱段，位于本级底端之下）：
    ``Stage.interstage_height_m`` 显式值，缺省 0（不切出，现状字节不变）。"""

    engine_bay_m: float
    """发动机舱段高（m）：``max(0, 发动机高 − 级间段高)``。

    物理口径：本级发动机（喷管）**伸入级间段**（如 F9 二级 MVac 伸入级间段 6.6 m）
    ——级间段与发动机的轴向占位重叠，发动机舱段只承载级间段之外的余量；
    级间段完全包容发动机时本值为 0（0 高分区不产带、不产节点，OI-33 演化口径）。
    """

    interstage_net_m: float
    """级间段占位中**发动机包容不了**的净余量（m）= ``max(0, 级间段 − 发动机高)``。

    从**贮箱可用高**中扣除（与裙段同口径）：级间段总占位 = ``min(级间段, 发动机高)``
    （发动机舱段让位包容）+ 本值（贮箱让位）。装配树与 §8.4 几何解析账共同消费
    同一份划分（M5 第四片两账同源纪律）。
    """

    intertank_source: Literal["user", "derived"]
    """第 6 分区（级间舱）高度来源：用户显式（``Stage.intertank_height_m``）/ 派生。"""

    interstage_source: Literal["user", "none"]
    """级间段高度来源：用户显式（``Stage.interstage_height_m``）/ 未给出（不切出）。"""

    @property
    def reserved_m(self) -> float:
        """分区轴向预留（m）：下箱底封头 + 第 6 分区 + 上箱顶封头 + 仪器舱
        + 级间段净占位（发动机包容不了的余量）。

        这是「级长 − 发动机高度」里**不属于两箱柱段**的部分；分区恰好铺满级长的
        关键扣减（:func:`resolve_tank_geometry` 缺省消费它）。
        """
        return (
            self.dome_lower_m
            + self.h_mid_m
            + self.dome_upper_m
            + self.avionics_m
            + self.interstage_net_m
        )


def tank_roles(stage: Stage) -> tuple[str, str]:
    """按储箱排列返回 ``(上箱角色, 下箱角色)``——§5.9 非铁律，读字段不硬编码。

    ``oxidizer_upper``（默认）⇒ 氧在上（S-IC / S-II / Falcon 9 形态）；
    ``fuel_upper`` ⇒ 燃料在上（S-IVB 形态）。
    """
    if stage.geometry.tank_arrangement == "fuel_upper":
        return ("fuel", "oxidizer")
    return ("oxidizer", "fuel")


def tank_diameters_m(stage: Stage) -> dict[str, float]:
    """两箱直径（m；省略 = 继承级直径，读 Schema 不硬编码）。"""
    return {
        "oxidizer": stage.geometry.oxidizer_tank.diameter_m or stage.diameter_m,
        "fuel": stage.geometry.fuel_tank.diameter_m or stage.diameter_m,
    }


def partition_heights(stage: Stage) -> PartitionHeights:
    """推导一级的九段分区高度事实（封头矢高 / 第 6 分区 / 仪器舱 / 级间段）。

    - 封头矢高：``h = 扁度系数 × 直径 / 2``（OI-37，级层 flatness，None 兜底 0.5）。
    - 第 6 分区：共底 = 隔板段（按**级径**反算，共底物理上要求两箱同径）；
      非共底 = 级间舱——``Stage.intertank_height_m`` 显式值优先，缺省按派生规则
      （两箱相邻封头矢高和，容纳两只相邻封头）。
    - 仪器舱：``Stage.avionics_height_m`` 显式值，缺省 0 高（§5.9 允许）。
    - 级间段（M5 第四片，§5.9 共性 2）：``Stage.interstage_height_m`` 显式值，
      缺省不切出（0）。本级发动机伸入级间段时发动机舱段让位包容（
      ``engine_bay_m = max(0, 发动机高 − 级间段)``），净余量从贮箱可用高扣除
      （``interstage_net_m``，与裙段同口径）。
    """
    upper_role, lower_role = tank_roles(stage)
    diameters = tank_diameters_m(stage)
    dome_lower = dome_height_m(diameters[lower_role], stage.flatness_ratio)
    dome_upper = dome_height_m(diameters[upper_role], stage.flatness_ratio)

    bulkhead: float | None = None
    intertank_source: Literal["user", "derived"] = "derived"
    if stage.geometry.common_bulkhead:
        bulkhead = dome_height_m(stage.diameter_m, stage.flatness_ratio)
        h_mid = bulkhead
    elif stage.intertank_height_m is not None:
        h_mid = stage.intertank_height_m
        intertank_source = "user"
    else:
        h_mid = dome_upper + dome_lower

    # 级间段（两级之间的分离舱段）：发动机伸入级间段时发动机舱段让位（物理事实：
    # 如 F9 二级 MVac 喷管伸入级间段），净余量由贮箱可用高承担
    interstage = stage.interstage_height_m or 0.0
    engine_bay = max(0.0, stage.engine_height_m - interstage)
    interstage_net = max(0.0, interstage - stage.engine_height_m)

    return PartitionHeights(
        lower_role=lower_role,
        upper_role=upper_role,
        diameter_lower_m=diameters[lower_role],
        diameter_upper_m=diameters[upper_role],
        dome_lower_m=dome_lower,
        dome_upper_m=dome_upper,
        h_mid_m=h_mid,
        bulkhead_m=bulkhead,
        avionics_m=stage.avionics_height_m or 0.0,
        interstage_m=interstage,
        engine_bay_m=engine_bay,
        interstage_net_m=interstage_net,
        intertank_source=intertank_source,
        interstage_source="user" if stage.interstage_height_m is not None else "none",
    )


def partition_reserved_m(stage: Stage) -> float:
    """九段分区的轴向预留（m）：:meth:`PartitionHeights.reserved_m` 的便捷入口。"""
    return partition_heights(stage).reserved_m


@dataclass(frozen=True, slots=True)
class TankGeometryEstimate:
    """单箱几何解析结果（工程惯例估算，非权威）。"""

    role: str
    """oxidizer / fuel。"""

    diameter_m: float
    cylinder_length_m: float
    dome_height_m: float
    volume_m3: float
    wetted_area_m2: float
    length_source: Literal["user", "derived"]
    """柱段长度来源：显式箱长（user，§5.9 规则 2）/ §5.9 容积比派生（derived）。"""


def resolve_tank_geometry(
    stage: Stage, *, reserved_m: float | None = None
) -> tuple[TankGeometryEstimate, TankGeometryEstimate]:
    """解析一级的两箱几何（氧化剂箱、燃料箱）。

    箱长优先级（读 Schema 裁定）：Tank 层**不存容积**（派生量不存储，§6.1），
    用户显式通道只有 ``Tank.length_m``——两箱都显式 → 直接用；只有一箱显式 →
    另一箱按 §5.9 容积比由显式箱长锚定；都缺 → 按容积比分配可用长度（级长 −
    发动机高度 − ``reserved_m``）。

    ``reserved_m``（M5 第二片裁定，§5.9）：从可用长度中扣除的**分区轴向预留**
    （下箱底封头 + 第 6 分区 + 上箱顶封头 + 仪器舱 + 级间段净占位）。缺省 ``None`` ⇒ 取
    :func:`partition_reserved_m`——装配树九段分区与 §8.4 几何解析账消费**同一份
    分区高度**（箱段高是几何事实，两账同源，不另立第二套箱体解析）；
    显式传 ``0.0`` 退回旧「级长 − 发动机高全算贮箱」口径（仅限对照）。
    """
    ox_tank = stage.geometry.oxidizer_tank
    fuel_tank = stage.geometry.fuel_tank
    props = propellants.properties(stage.propellant)

    if reserved_m is None:
        reserved_m = partition_reserved_m(stage)

    ox_diameter = ox_tank.diameter_m or stage.diameter_m
    fuel_diameter = fuel_tank.diameter_m or stage.diameter_m
    ratio = volumetric_ratio(
        stage.engine.mixture_ratio, props.density_ox_kg_m3, props.density_fuel_kg_m3
    )

    ox_length: float | None = ox_tank.length_m
    fuel_length: float | None = fuel_tank.length_m
    ox_source: Literal["user", "derived"] = "user" if ox_length is not None else "derived"
    fuel_source: Literal["user", "derived"] = "user" if fuel_length is not None else "derived"

    if ox_length is not None and fuel_length is not None:
        pass  # 两箱均显式：用户权威，直接用（§5.9 规则 2，不静默覆盖）
    elif ox_length is not None:
        fuel_length = ox_length / ratio  # 由显式氧箱长按 §5.9 容积比锚定
    elif fuel_length is not None:
        ox_length = fuel_length * ratio
    else:
        available = stage.length_m - stage.engine_height_m - reserved_m
        if available <= 0.0:
            msg = (
                f"第 {stage.index} 级可用箱长非正（级长 {stage.length_m} m − 发动机高度 "
                f"{stage.engine_height_m} m − 轴向预留 {reserved_m:.6f} m = {available:.6f} m），"
                "无法按 §5.9 分配两箱柱长"
            )
            raise ValueError(msg)
        # §5.9 容积比 → 柱长比（两箱截面积按各自直径，容积比与柱长比仅在等直径时
        # 严格相等；分母按各箱截面积折算，避免异径箱分配失真）
        ox_area = math.pi * ox_diameter**2 / 4.0
        fuel_area = math.pi * fuel_diameter**2 / 4.0
        # V_ox/V_fuel = ratio，V = A·L（柱段）⇒ L_ox·A_ox = ratio·L_fuel·A_fuel
        # L_ox + L_fuel = available ⇒ L_fuel = available·A_ox/(A_ox + ratio·A_fuel)
        fuel_length = available * ox_area / (ox_area + ratio * fuel_area)
        ox_length = available - fuel_length

    def _estimate(
        role: str, diameter: float, length: float, source: Literal["user", "derived"]
    ) -> TankGeometryEstimate:
        height = dome_height_m(diameter, stage.flatness_ratio)
        return TankGeometryEstimate(
            role=role,
            diameter_m=diameter,
            cylinder_length_m=length,
            dome_height_m=height,
            volume_m3=tank_volume_m3(diameter, length, height),
            wetted_area_m2=tank_wetted_area_m2(diameter, length, height),
            length_source=source,
        )

    return (
        _estimate("oxidizer", ox_diameter, ox_length, ox_source),
        _estimate("fuel", fuel_diameter, fuel_length, fuel_source),
    )


def propellant_mass_kg(stage: Stage) -> float:
    """几何解析的推进剂质量（kg）：各箱容积 × 本剂密度之和 × 级层加注比例。

    贮箱容积消费**九段分区的箱段高**（与装配树同源，M5 第二片裁定）——分区高度
    是几何事实，推进剂质量（几何）不得绕开分区另算一套「级长 − 发动机高」。
    """
    ox, fuel = resolve_tank_geometry(stage)
    props = propellants.properties(stage.propellant)
    return (
        ox.volume_m3 * props.density_ox_kg_m3 + fuel.volume_m3 * props.density_fuel_kg_m3
    ) * stage.fill_fraction


def axial_buckling_thickness_m(
    axial_load_n: float, elastic_modulus_pa: float, radius_m: float
) -> float:
    """轴压屈曲等价壁厚（m，NASA SP-8007）：``t = √(P_ax / (2π·γ(t)·0.605·E))``。

    由「环向均布轴压应力 σ = P_ax/(2πR·t) = 折减屈曲应力 γ·0.605·E·t/R」解出，
    R 在两式相乘时消去。折减系数 γ 依 SP-8007 经验式 ``γ = 1 − 0.908(1 − e^(−φ))``、
    ``φ = (1/16)√(R/t)``——是 t 的函数，故隐式迭代（t ∈ [√(base/1), √(base/0.092)]
    有界，映射向不动点单调收缩，64 次内收敛到相对 1e-12）。载荷 ≤ 0 返回 0
    （无轴压尺寸需求，承压路径仍生效）。
    """
    if axial_load_n <= 0.0:
        return 0.0
    base = axial_load_n / (2.0 * math.pi * SP8007_CLASSICAL_COEFF * elastic_modulus_pa)
    thickness = math.sqrt(base / 0.4)  # γ = 0.4 初值（迭代收敛域中点）
    for _ in range(64):
        phi = math.sqrt(radius_m / thickness) / SP8007_PHI_DIVISOR
        gamma = 1.0 - SP8007_KNOCKDOWN_COEFF * (1.0 - math.exp(-phi))
        updated = math.sqrt(base / gamma)
        if abs(updated - thickness) <= 1e-12 * updated:
            return updated
        thickness = updated
    return thickness


def tank_areal_density_kg_m2(
    material: MaterialEntry, tank_diameter_m: float, axial_load_n: float
) -> float:
    """贮箱壁面密度（kg/m²）= ρ · max(承压, 轴压稳定) · 焊缝加成。

    - 承压 ``t_P = P·R/(σ_y·k)``（薄壁压力容器环向公式，P/k 取参数表惯例值）；
    - 轴压稳定 ``t_ax``（:func:`axial_buckling_thickness_m`，SP-8007）——大运载
      贮箱壁的实际尺寸主导路径（环向应力仅需亚毫米壁厚，而轴压/屈曲需毫米级）；
    - 两者取大再乘焊缝/加强框加成系数（参数表出处见常量注记）。
    """
    radius = tank_diameter_m / 2.0
    t_pressure = (
        TANK_ULLAGE_PRESSURE_PA * radius / (material.yield_strength_pa * PRESSURE_SAFETY_FACTOR)
    )
    t_axial = axial_buckling_thickness_m(axial_load_n, material.elastic_modulus_pa, radius)
    t_effective = max(t_pressure, t_axial) * WELD_STIFFENER_FACTOR
    return material.density_kg_m3 * t_effective


def engine_dry_mass_kg(stage: Stage) -> float:
    """发动机干重（kg）= 台数 × F_vac/(T/W·g₀)。

    T/W 按循环分档取文献惯例值（:data:`ENGINE_THRUST_TO_WEIGHT`，出处见常量
    注记）；F_vac 取发动机真空推力（Schema 必填，量级口径对两类工作点一致）。
    """
    thrust_to_weight = ENGINE_THRUST_TO_WEIGHT[stage.engine.cycle]
    return stage.engine_count * stage.engine.thrust_vacuum_n / (thrust_to_weight * G0)


@dataclass(frozen=True, slots=True)
class StageDryMassBreakdown:
    """逐级干重的分部位账目（M6 前置专项①；:func:`dry_mass_geometric_kg` 的展开）。

    清算式：``total = (两箱壁 + 隔板 + 发动机) / (1 − Σf)``；
    非贮箱各部位质量 = f_i × total（占级干重分数闭环，分数见
    :data:`NON_TANK_MASS_FRACTIONS`）。装配树分区填实与本账同源（两账对拍闭合）。
    """

    tank_oxidizer_kg: float
    """氧化剂箱壁干重（kg，湿面积 × 物理面密度）。"""

    tank_fuel_kg: float
    """燃料箱壁干重（kg，同上）。"""

    bulkhead_kg: float
    """共底隔板干重（kg）；非共底为 0.0（:func:`bulkhead_dry_mass_kg` 口径）。"""

    engine_kg: float
    """发动机干重（kg，:func:`engine_dry_mass_kg` 推算）。"""

    residual_thrust_structure_kg: float
    """非贮箱部位——推力结构/机架（kg，= f_thrust × total）。"""

    residual_plumbing_kg: float
    """非贮箱部位——推进系统管路与增压（kg，= f_plumb × total）。"""

    residual_skirts_misc_kg: float
    """非贮箱部位——裙段/级间段/舱段/航电及其他固定件（kg）。"""

    total_kg: float
    """级干重合计（kg）== :func:`dry_mass_geometric_kg`。"""


def stage_dry_mass_breakdown_kg(
    stage: Stage, *, reserved_m: float | None = None
) -> StageDryMassBreakdown:
    """逐级干重的分部位账目（清算式见模块 docstring；全部参数出处见常量参数表）。

    贮箱湿面积按**分区箱段高**计（与装配树同源，``reserved_m`` 语义同
    :func:`resolve_tank_geometry`）；轴压载荷以**本级推进剂重量**（同一份分区
    几何的推进剂账）作量级代理——两箱同载（工程惯例近似，留痕不精确分箱）。
    """
    ox, fuel = resolve_tank_geometry(stage, reserved_m=reserved_m)
    props = propellants.properties(stage.propellant)
    m_prop = (
        ox.volume_m3 * props.density_ox_kg_m3 + fuel.volume_m3 * props.density_fuel_kg_m3
    ) * stage.fill_fraction
    axial_load_n = m_prop * G0

    tank_masses: dict[str, float] = {}
    for estimate, role, material_id in (
        (ox, "oxidizer", stage.geometry.oxidizer_tank.material),
        (fuel, "fuel", stage.geometry.fuel_tank.material),
    ):
        material = get_material(material_id)
        tank_masses[role] = estimate.wetted_area_m2 * tank_areal_density_kg_m2(
            material, estimate.diameter_m, axial_load_n
        )

    bulkhead = 0.0
    if stage.geometry.common_bulkhead:
        heights = partition_heights(stage)
        assert heights.bulkhead_m is not None
        bulkhead = bulkhead_dry_mass_kg(stage, heights.bulkhead_m)

    engine = engine_dry_mass_kg(stage)
    core = tank_masses["oxidizer"] + tank_masses["fuel"] + bulkhead + engine
    total = core / (1.0 - NON_TANK_FRACTION_TOTAL)
    fractions = NON_TANK_MASS_FRACTIONS
    return StageDryMassBreakdown(
        tank_oxidizer_kg=tank_masses["oxidizer"],
        tank_fuel_kg=tank_masses["fuel"],
        bulkhead_kg=bulkhead,
        engine_kg=engine,
        residual_thrust_structure_kg=fractions["thrust_structure"] * total,
        residual_plumbing_kg=fractions["plumbing_pressurization"] * total,
        residual_skirts_misc_kg=fractions["skirts_avionics_misc"] * total,
        total_kg=total,
    )


def tank_dry_masses_kg(stage: Stage, *, reserved_m: float | None = None) -> tuple[float, float]:
    """按箱分列的几何解析干重（kg）：``(氧化剂箱, 燃料箱)``。

    与 :func:`stage_dry_mass_breakdown_kg` 同源（湿面积 × 物理面密度；壁厚 =
    承压/轴压两路物理推导取大 × 焊缝加成，参数表见模块常量区——刻意不用用户
    的 Tank.wall_thickness_m，保持本来源独立于细观参数）。``reserved_m`` 语义同
    :func:`resolve_tank_geometry`（缺省 = 分区轴向预留，与装配树同源）。
    """
    breakdown = stage_dry_mass_breakdown_kg(stage, reserved_m=reserved_m)
    return (breakdown.tank_oxidizer_kg, breakdown.tank_fuel_kg)


def bulkhead_dry_mass_kg(stage: Stage, bulkhead_height_m: float) -> float:
    """共底隔板干重（kg）：隔板表面积 × 面密度（材料取燃料/LH₂ 侧）。

    面密度口径与箱壁**不同**：共底隔板只承担两箱间的差压与惯性载荷（无环向
    镇压、无轴压柱稳定需求），按材料库**最小工艺壁厚**口径取值——与承压/轴压
    双路尺寸的箱壁分属不同载荷谱，故「共底另计」（任务口径）。驻留本模块
    （而非装配层）：§8.4 几何解析账覆盖**贮箱与共底隔板**（§5.9 口径 2），
    装配树的隔板质量贡献与本账必须同源——单一实现，两处消费。
    """
    material = get_material(stage.geometry.fuel_tank.material)
    areal_density = material.typical_min_wall_thickness_m * material.density_kg_m3
    return dome_surface_area_m2(stage.diameter_m, bulkhead_height_m) * areal_density


def dry_mass_geometric_kg(stage: Stage) -> float:
    """几何解析干重（kg，M6 前置专项①分部位物理模型）。

    清算式：``m_dry = (两箱壁物理面密度账 + 共底隔板〔开启时〕 + 发动机推算)
    / (1 − Σf_非贮箱)``；全部参数出处见模块常量参数表，GCAT σ 干重样本只作
    对照（holdout），不进入本账标定回路（防循环验证红线）。贮箱湿面积按
    **分区箱段高**计（与装配树同源）。
    """
    return stage_dry_mass_breakdown_kg(stage).total_kg


@dataclass(frozen=True, slots=True)
class CrossCheckOutcome:
    """§8.4 一致性校验结果（几何解析 vs σ 推算）。"""

    m_dry_geometric_kg: float
    """几何解析干重（湿面积 × 面密度）。"""

    m_dry_sigma_kg: float
    """σ 推算干重 = 几何解析推进剂质量 × σ/(1−σ)（σ 是存储权威，作为对照基准）。"""

    relative_deviation: float
    """相对偏差 = |几何 − σ| / σ（σ 推算值为基准）。"""

    exceeds_threshold: bool
    """是否超过 §8.4 的 20% 阈值（超过 → 调用方须以 warning 呈现）。"""

    warning: str | None
    """超过阈值时的警示文案；未超过为 None。"""


def cross_check(stage: Stage) -> CrossCheckOutcome:
    """§8.4 一致性校验：几何解析干重 vs 用户 σ 推算干重。

    σ 推算干重以**几何解析推进剂质量**为基数（同一容积口径下比较结构效率），
    偏差 > 20% 意味着构型异常或外推（§8.4）——单来源不得作为结论。
    两侧的贮箱几何均消费九段分区高度（M5 第二片起同源）；几何侧干重为分部位
    物理模型（M6 前置专项①，清算式见模块 docstring），残余偏差的本底是模型
    颗粒度与统计 σ 的来源差，与分区口径无关。
    """
    m_prop = propellant_mass_kg(stage)
    sigma = stage.structure_coefficient
    m_dry_sigma = m_prop * sigma / (1.0 - sigma)
    m_dry_geo = dry_mass_geometric_kg(stage)
    deviation = abs(m_dry_geo - m_dry_sigma) / m_dry_sigma if m_dry_sigma > 0.0 else math.inf
    exceeds = deviation > CROSS_CHECK_THRESHOLD
    return CrossCheckOutcome(
        m_dry_geometric_kg=m_dry_geo,
        m_dry_sigma_kg=m_dry_sigma,
        relative_deviation=deviation,
        exceeds_threshold=exceeds,
        warning=(
            f"第 {stage.index} 级双来源交叉校验：几何解析干重 {m_dry_geo:.1f} kg 与 "
            f"σ={sigma} 推算干重 {m_dry_sigma:.1f} kg 偏差 {deviation:.1%}，"
            f"超过 {CROSS_CHECK_THRESHOLD:.0%} 阈值（§8.4：构型异常或回归外推，"
            "单来源不得作为结论）"
            if exceeds
            else None
        ),
    )


# ---------------------------------------------------------------------------
# 统计回归（GCAT stages 表；取数只经仓储层公共 API，本模块零 SQL）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SigmaSample:
    """一个回归样本：某级记录的 σ 及其分箱维度。"""

    stage_name: str
    position: Position
    propellant_class: PropellantClass
    sigma: float


@dataclass(frozen=True, slots=True)
class SigmaBin:
    """一个分箱的统计（中位数 + 四分位，§8.4 口径的简化回归）。"""

    position: Position
    propellant_class: PropellantClass
    sample_count: int
    sigma_median: float
    sigma_q1: float
    sigma_q3: float
    interval: tuple[float, float] | None
    """§8.4 的 σ 有效域（无对应区间时为 None，只报告不判定）。"""

    out_of_interval: bool
    """箱中位数是否越出 §8.4 有效域（越界 → 显式警告，不得静默外推）。"""


@dataclass(frozen=True, slots=True)
class SigmaBinSummary:
    """分箱统计汇总（含合并留痕与越界警告）。"""

    bins: tuple[SigmaBin, ...]
    merge_notes: tuple[str, ...]
    """样本不足箱的合并去向（§8.4：合并必须记录，不静默）。"""

    warnings: tuple[str, ...]
    """σ 中位数越出 §8.4 有效域的箱级警告。"""

    total_samples: int


@dataclass(frozen=True, slots=True)
class SigmaRegressionReport(SigmaBinSummary):
    """GCAT 回归完整报告（含覆盖率账目，供 provenance / 测试如实断言）。"""

    snapshot_id: str
    snapshot_release: str
    total_stage_rows: int
    """stages 表总行数（覆盖率分母）。"""

    dual_non_missing_rows: int
    """full_mass_kg / dry_mass_kg 双非缺失的行数（含 dry ≥ full 的病态行）。"""

    valid_rows: int
    """物理有效样本数（0 < dry < full 且 quality 合格）——进入分箱的样本。"""

    skipped_quality_rows: int = 0
    """因 quality 不在 official/literature 白名单而被跳过的行数。"""

    method_note: str = field(
        default=(
            "分箱中位数 + 四分位（inclusive 分位）统计；非连续回归——"
            "§8.4 的 R² 要求按任务口径降为本统计形态"
        )
    )


def classify_propellant(engine: EngineRecord | None) -> PropellantClass:
    """推进剂大类判定：有氧化剂 → 液体（双组元）；氧化剂缺失且燃料非已知单组元
    液体 → 固体；无发动机记录 → unknown。"""
    if engine is None:
        return "unknown"
    if engine.oxidizer is not None:
        return "liquid"
    if engine.fuel in _MONOPROPELLANT_FUELS:
        return "liquid"
    return "solid"


def resolve_position(votes: Counter[str]) -> Position:
    """由 stage_links 的 Stage_No 投票裁定级序位置。

     GCAT 的 Stage_No：1 = 一级、≥ 2 = 上面级、≤ 0（0 / −1）= 助推器；非数字
    （' F' 整流罩 / ' C' 过渡段等）不参与投票。同一级名出现在多种位置时取**多数**
     （并列时按 一级 > 上面级 > 助推器 优先，保证确定性）。
    """
    if not votes:
        return "unknown"
    precedence = ("first", "upper", "booster")
    best = max(votes.items(), key=lambda pair: (pair[1], -precedence.index(pair[0])))
    return best[0]  # type: ignore[return-value]  # votes 的键只来自 precedence 三值


def summarize_sigma_bins(samples: Sequence[SigmaSample]) -> SigmaBinSummary:
    """把样本按（位置 × 大类）分箱并统计（纯函数，供回归与测试共用）。

    合并规则：样本 < :data:`MIN_BIN_SIZE` 的箱并入**同级**（同推进剂大类）中位置
    最近的大箱；同级无大箱 → 并入混合兜底箱（pooled / mixed）。每次合并都写入
    ``merge_notes``，不静默。§8.4 区间对照：箱中位数越界 → warnings 显式警告。
    """
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sample in samples:
        groups[(sample.position, sample.propellant_class)].append(sample.sigma)

    big_keys = {key for key, values in groups.items() if len(values) >= MIN_BIN_SIZE}
    merged: dict[tuple[str, str], list[float]] = {key: list(groups[key]) for key in big_keys}
    merge_notes: list[str] = []
    pooled: list[float] = []

    for key in sorted(groups, key=lambda k: (k[1], _POSITION_ORDER.get(k[0], 9))):
        if key in big_keys:
            continue
        n = len(groups[key])
        same_class = sorted(
            (candidate for candidate in big_keys if candidate[1] == key[1]),
            key=lambda candidate: (
                abs(_POSITION_ORDER[candidate[0]] - _POSITION_ORDER.get(key[0], 9)),
                _POSITION_ORDER[candidate[0]],
            ),
        )
        if same_class:
            target = same_class[0]
            merged[target].extend(groups[key])
            merge_notes.append(
                f"箱 {key}（n={n} < {MIN_BIN_SIZE}）并入同级最近箱 {target}（§8.4 合并留痕）"
            )
        else:
            pooled.extend(groups[key])
            merge_notes.append(
                f"箱 {key}（n={n} < {MIN_BIN_SIZE}）无同级大箱，并入混合兜底箱（pooled/mixed）"
            )
    if pooled:
        merged[("pooled", "mixed")] = pooled

    bins: list[SigmaBin] = []
    warnings: list[str] = []
    for key in sorted(merged, key=lambda k: (k[1], _POSITION_ORDER.get(k[0], 9))):
        values = merged[key]
        median = statistics.median(values)
        quartiles = statistics.quantiles(values, n=4, method="inclusive")
        interval = SIGMA_INTERVALS.get(key)
        out = interval is not None and not (interval[0] <= median <= interval[1])
        bins.append(
            SigmaBin(
                position=key[0],  # type: ignore[arg-type]
                propellant_class=key[1],  # type: ignore[arg-type]
                sample_count=len(values),
                sigma_median=median,
                sigma_q1=quartiles[0],
                sigma_q3=quartiles[2],
                interval=interval,
                out_of_interval=out,
            )
        )
        if out and interval is not None:
            note = (
                "（§8.4 未单列助推器，按一级液体口径对照）" if key == ("booster", "liquid") else ""
            )
            warnings.append(
                f"箱 {key} 的 σ 中位数 {median:.4f} 越出 §8.4 有效域 "
                f"[{interval[0]}, {interval[1]}]{note}——统计口径与规格区间存在分歧，"
                "该箱只作对照，不得作为缺省建议直接引用"
            )

    return SigmaBinSummary(
        bins=tuple(bins),
        merge_notes=tuple(merge_notes),
        warnings=tuple(warnings),
        total_samples=len(samples),
    )


def regress_structure_coefficients(repository: CatalogRepository) -> SigmaRegressionReport:
    """从 GCAT stages 表回归结构系数 σ（§8.4 统计回归来源）。

    样本口径：``full_mass_kg`` / ``dry_mass_kg`` 双非缺失、0 < dry < full（σ ∈ (0,1)）、
    quality ∈ {official, literature}。级序位置由 vehicles × stage_links 的 Stage_No
    投票裁定；推进剂大类由 engines 表氧化剂/燃料判定。回归值**只作对照 / 缺省建议**，
    不覆盖用户输入的 σ（σ 是存储权威）。
    """
    stages = repository.query_stages()
    engines: dict[str, EngineRecord] = {}
    for engine in repository.query_engines():
        engines.setdefault(engine.name, engine)  # 同名取首见（record_id 序，留痕口径同仓储层）

    votes: dict[str, Counter[str]] = {}
    for vehicle in repository.query_vehicles():
        variant = vehicle.variant if vehicle.variant else "-"
        for link in repository.stage_links(vehicle.name, variant):
            if not link.stage_name:
                continue
            try:
                stage_no = float(link.stage_no) if link.stage_no else math.nan
            except ValueError:
                continue  # ' F' / ' C' 等非数字段号不参与投票
            if math.isnan(stage_no):
                continue
            if stage_no == 1:
                position = "first"
            elif stage_no >= 2:
                position = "upper"
            else:
                position = "booster"
            votes.setdefault(link.stage_name, Counter())[position] += 1

    samples: list[SigmaSample] = []
    dual_non_missing = 0
    valid = 0
    skipped_quality = 0
    for record in stages:
        if record.full_mass_kg is None or record.dry_mass_kg is None:
            continue
        dual_non_missing += 1
        if record.quality not in _ALLOWED_QUALITY:
            skipped_quality += 1
            continue
        full = record.full_mass_kg
        dry = record.dry_mass_kg
        if not (full > 0.0 and dry > 0.0 and dry < full):
            continue  # dry ≥ full 的病态行（适配器 / 舱段等无推进剂构件）如实剔除
        valid += 1
        engine_record = engines.get(record.engine_name) if record.engine_name else None
        samples.append(
            SigmaSample(
                stage_name=record.name,
                position=resolve_position(votes.get(record.name, Counter())),
                propellant_class=classify_propellant(engine_record),
                sigma=dry / full,
            )
        )

    summary = summarize_sigma_bins(samples)
    snapshot = repository.snapshot()
    return SigmaRegressionReport(
        bins=summary.bins,
        merge_notes=summary.merge_notes,
        warnings=summary.warnings,
        total_samples=summary.total_samples,
        snapshot_id=snapshot.id,
        snapshot_release=snapshot.release,
        total_stage_rows=len(stages),
        dual_non_missing_rows=dual_non_missing,
        valid_rows=valid,
        skipped_quality_rows=skipped_quality,
    )


__all__ = [
    "CROSS_CHECK_THRESHOLD",
    "DEFAULT_FLATNESS_RATIO",
    "ENGINE_THRUST_TO_WEIGHT",
    "MASS_MODEL_VERSION",
    "MIN_BIN_SIZE",
    "NON_TANK_FRACTION_TOTAL",
    "NON_TANK_MASS_FRACTIONS",
    "PRESSURE_SAFETY_FACTOR",
    "SIGMA_INTERVALS",
    "SP8007_CLASSICAL_COEFF",
    "SP8007_KNOCKDOWN_COEFF",
    "SP8007_PHI_DIVISOR",
    "TANK_ULLAGE_PRESSURE_PA",
    "WELD_STIFFENER_FACTOR",
    "CrossCheckOutcome",
    "PartitionHeights",
    "Position",
    "PropellantClass",
    "SigmaBin",
    "SigmaBinSummary",
    "SigmaRegressionReport",
    "SigmaSample",
    "StageDryMassBreakdown",
    "TankGeometryEstimate",
    "axial_buckling_thickness_m",
    "bulkhead_dry_mass_kg",
    "classify_propellant",
    "cross_check",
    "dome_height_m",
    "dome_surface_area_m2",
    "dome_volume_m3",
    "dry_mass_geometric_kg",
    "engine_dry_mass_kg",
    "partition_heights",
    "partition_reserved_m",
    "propellant_mass_kg",
    "regress_structure_coefficients",
    "resolve_position",
    "resolve_tank_geometry",
    "stage_dry_mass_breakdown_kg",
    "summarize_sigma_bins",
    "tank_areal_density_kg_m2",
    "tank_diameters_m",
    "tank_dry_masses_kg",
    "tank_roles",
    "tank_volume_m3",
    "tank_wetted_area_m2",
]
