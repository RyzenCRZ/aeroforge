"""九段分区装配树（规格 §5.9 / §5.5 / OI-33 / OI-37，M5 第一片）。

职责
----
把 ``Vehicle`` 的逐级 ``Stage`` 参数映射为 **§5.9 的 9 段轴向固定分区**，逐分区
生成回转实体（GLB 具名节点）+ 质量贡献 + 材料引用 + 参数来源，并落地共底四校验
（§5.5）与尾翼（§5.5）。与既有**母线剖面通路**（``POST /api/geometry/build`` 的
profile 形态，节点名 ``seg-<i>``）并存：本模块是车辆形态构建（节点名 = 分区名）。

轴向布局约定（工程惯例，本片裁决——留白项见模块末尾"约定与留白"）
------------------------------------------------------------------
自下而上逐级铺满 ``Stage.length_m``：

::

    engine_bay（He）→ thrust_structure（h_lo_bot，容纳下箱底封头）
    → 下箱柱段（L_lo）→ [级间舱 h_mid | 共底隔板段 h_b] → 上箱柱段（L_hi）
    → forward_skirt（h_hi_top，容纳上箱顶封头）→ avionics（0 高，§5.9 允许）

- 封头矢高 ``h = flatness × d / 2``（OI-37；``None`` 兜底 0.5）——**优先级**：
  母线 ``ellipse`` 段的显式 ``length``（用户在剖面里画什么是什么）> 级层
  ``flatness_ratio`` > 0.5 兜底。本模块只消费级层值（剖面通路天然用显式值）。
- 箱柱长由 :func:`aeroforge.perf.mass.resolve_tank_geometry` 派生（§5.9 容积比
  ``V_ox/V_fuel = (O/F)·ρ_fuel/ρ_ox``），轴向预留 ``reserved_m``（封头占位 +
  第 6 分区 + 仪器舱，:func:`aeroforge.perf.mass.partition_reserved_m`）使分区
  **恰好铺满**级长——M5 第二片起 §8.4 几何解析账缺省消费**同一份分区高度**
  （分区高度是几何事实，两账同源），不另立第二套箱体解析。
- 储箱排列**读字段不硬编码**（§5.9 非铁律）：``oxidizer_upper`` ⇒ 氧箱在上；
  ``fuel_upper`` ⇒ 燃料箱在上（第 5/7 分区的上下次序随之对调）。
- 共底：隔板为凹向上箱的椭球面，矢高 ``h_b = flatness × D / 2``（按**级径**反算，
  共底物理上要求两箱同径）；级长缩减量 ``saving = (h_lo_top + h_hi_bot) − h_b``
  （公式反算，不得手填，§5.9 口径 2）。

质量贡献（§8.4 几何解析账，复用 :mod:`aeroforge.perf.mass`）
-------------------------------------------------------------
- 氧箱 / 燃料箱：``wetted_area × 物理面密度``（:func:`tank_dry_masses_kg`，与
  :func:`dry_mass_geometric_kg` 同源、按箱分列；M6 前置专项①起壁厚为承压/轴压
  双路物理推导取大 × 焊缝加成）。
- 共底隔板：隔板表面积 × 最小工艺面密度（:func:`bulkhead_dry_mass_kg` 口径，
  与 §8.4 账同源）。
- 非贮箱部位（M6 前置专项①填实）：推力结构 → ``thrust_structure`` 带；发动机
  （循环 T/W 推算）+ 管路/增压 → ``engine_bay`` 带（级间段包容发动机时随
  ``interstage`` 带）；裙段/级间段/舱段/航电等固定件按包络面积分摊到
  ``forward_skirt`` / ``intertank`` / ``interstage`` / ``avionics``——各带质量
  之和与 :func:`dry_mass_geometric_kg` 精确闭合（两账对拍）。
- 整流罩 / 适配器 / 尾翼 / 助推器 / 喷管：**0.0 + 留白注记**——整流罩与适配器
  是飞行器级部件（不入级干重），尾翼与助推器体积在 metrics 的 ``fins`` /
  ``boosters`` 块单独成账，喷管随发动机账——不为无模型部件编造数字（§1.4-4）。

约定与留白（显式声明，不静默）
------------------------------
1. ``avionics`` 仪器舱高：``Stage.avionics_height_m`` 显式值优先（M5 第二片
   Schema 增补）；缺省 0 高（§5.9 允许）——0 高分区不产出 GLB 节点（部件缺失
   即无节点，OI-33 演化口径）。
2. 级间段（interstage，级间分离舱段，§5.9 共性 2）：``Stage.interstage_height_m``
   显式值 > 0 时切出独立 band / 节点 ``s<级>-interstage``——位于本级布局的**最
   底部**（顶接本级发动机舱段下缘、底接下级前裙上缘；剖面图中在上级 engine_bay
   之下、下级 forward_skirt 之上）。⚠ 级间段（两级之间）与级间舱（intertank，
   同级两箱之间）是两个部件；级间舱高由 ``Stage.intertank_height_m`` 显式可输入。
   级间段的长度预算**计入本级 length_m**（如 F9 二级 19.2 m 含级间段 6.6 m，
   从 19.2 内划出、非加高）；本级发动机伸入级间段时发动机舱段让位包容（净余量
   从贮箱可用高扣除，两账同源，见 :mod:`aeroforge.perf.mass`）。
3. ``engine_bay`` 的节点 mesh 为柱段（外模线）；**喷管钟形外形**自推进参数派生
   （M5 第四片）：喉部半径 ``rt = √(F_vac/(Pc·π·Cf))``（Cf = 1.65 工程惯例）、
   出口半径 ``Re = rt·√ε``（工作点口径）、钟长 = 80% Rao（放不下时按可用发动机
   占高收长度比），多管发动机周向布置，独立具名节点 ``s<级>-nozzle[-<k>]``。
4. 共底隔板（M5 第四片）：从内嵌曲面升级为**独立薄壳节点**
   ``s<级>-common-bulkhead``（与 sections 的 ``common_bulkhead`` band 名对齐），
   壳厚 = 两侧壁厚和（工程近似：半轴各内缩同厚），metadata 标注 saving 与隔热
   标志。
5. 整流罩高：``Vehicle.fairing_height_m`` 显式值优先（M5 第二片 Schema 增补）；
   缺省为工程惯例常量（见 :data:`_FAIRING_*`，现状值保持不变）。适配器高度
   仍为惯例常量（无 Schema 输入）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import build123d as bd
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.geometry.bundle import BoosterSummary, booster_cylinders_for_radius
from aeroforge.geometry.meridian import (
    GEOM_TOL,
    BellNozzleSegment,
    EllipseSegment,
    LineSegment,
    MeridianProfile,
    TangentOgiveSegment,
    resolve,
)
from aeroforge.geometry.revolve import GLB_ROOT_NAME, build_solid
from aeroforge.params.propellants import fuel_is_lh2
from aeroforge.params.schema import Stage, Vehicle
from aeroforge.perf.mass import (
    dome_height_m,
    dome_volume_m3,
    dry_mass_geometric_kg,
    partition_heights,
    resolve_tank_geometry,
    stage_dry_mass_breakdown_kg,
    tank_diameters_m,
    tank_roles,
)

# ---------------------------------------------------------------------------
# §5.9 的 9 段轴向固定分区（权威次序，自上而下）
# ---------------------------------------------------------------------------

#: 分区枚举（§5.9 表；``intertank`` / ``bulkhead`` 二选一，由共底开关决定）。
SECTION_FAIRING = "fairing"
SECTION_ADAPTER = "adapter"
SECTION_AVIONICS = "avionics"
SECTION_FORWARD_SKIRT = "forward_skirt"
SECTION_OX_TANK = "ox_tank"
SECTION_INTERTANK = "intertank"
SECTION_BULKHEAD = "bulkhead"
SECTION_FUEL_TANK = "fuel_tank"
SECTION_THRUST_STRUCTURE = "thrust_structure"
SECTION_ENGINE_BAY = "engine_bay"
#: 级间段（§5.9 共性 2 的扩展分区，M5 第四片）：**两级之间**的分离舱段，
#: 不属于 §5.9 的 9 段（那是单级内的分区）——作为第 10 个枚举追加在末位。
SECTION_INTERSTAGE = "interstage"

#: §5.9 的轴向固定次序（自上而下；第 5/7 段的上下次序由 ``tank_arrangement`` 决定，
#: 实现读字段而非按序号硬编码）。级间段是级**外**的扩展分区，追加在末位。
SECTION_ORDER: tuple[str, ...] = (
    SECTION_FAIRING,
    SECTION_ADAPTER,
    SECTION_AVIONICS,
    SECTION_FORWARD_SKIRT,
    SECTION_OX_TANK,
    SECTION_INTERTANK,  # 或 SECTION_BULKHEAD（共底开启时）
    SECTION_FUEL_TANK,
    SECTION_THRUST_STRUCTURE,
    SECTION_ENGINE_BAY,
    SECTION_INTERSTAGE,
)

#: 分区枚举 → GLB 节点名后缀（连字符形态，与前端按名寻址兼容）。
#: 共底隔板节点名与 sections 的 ``common_bulkhead`` band 名对齐（M5 第四片）。
_SECTION_NODE_SUFFIX: dict[str, str] = {
    SECTION_FAIRING: "fairing",
    SECTION_ADAPTER: "adapter",
    SECTION_AVIONICS: "avionics",
    SECTION_FORWARD_SKIRT: "forward-skirt",
    SECTION_OX_TANK: "ox-tank",
    SECTION_INTERTANK: "intertank",
    SECTION_BULKHEAD: "common-bulkhead",
    SECTION_FUEL_TANK: "fuel-tank",
    SECTION_THRUST_STRUCTURE: "thrust-structure",
    SECTION_ENGINE_BAY: "engine-bay",
    SECTION_INTERSTAGE: "interstage",
}

#: 角色（oxidizer / fuel）→ 分区枚举。
_ROLE_SECTION: dict[str, str] = {"oxidizer": SECTION_OX_TANK, "fuel": SECTION_FUEL_TANK}

#: 整流罩 / 适配器的工程惯例常量（无 Schema 输入；留白见模块 docstring 第 5 条）。
_FAIRING_HEIGHT_DIAMETER_RATIO = 2.2
_FAIRING_HEIGHT_MIN_M = 5.0
_FAIRING_HEIGHT_MAX_M = 20.0
_FAIRING_CYLINDER_FRACTION = 0.55  # 柱段占整流罩高的比例，其余为切线卵形
_ADAPTER_HEIGHT_DIAMETER_RATIO = 0.35
_ADAPTER_HEIGHT_MIN_M = 0.8
_ADAPTER_HEIGHT_MAX_M = 3.0

#: 共底容积守恒容差（§5.5 校验 2：0.5%）。
BULKHEAD_VOLUME_TOL = 5e-3

#: 尾翼厚度工程惯例：t = 0.04 × 弦长（任务参数表未列厚度，Schema 无输入）。
FIN_THICKNESS_RATIO = 0.04

#: 根部倒圆半径系数（局部圆角，§5.5：避免根部应力集中）。
FIN_ROOT_FILLET_RATIO = 0.25


class AssemblyError(ValueError):
    """装配布局的域错误（分区铺不满 / 矢高超限 / 参数不自洽等）。"""


def section_node_name(stage_index: int, section: str) -> str:
    """分区 GLB 节点名：``s<级序>-<分区>``（稳定可枚举，前端按名寻址）。"""
    return f"s{stage_index}-{_SECTION_NODE_SUFFIX[section]}"


# ---------------------------------------------------------------------------
# 装配数据模型（metrics.assembly_tree 的形状）
# ---------------------------------------------------------------------------


class AssemblyNode(BaseModel):
    """装配树节点元数据（节点名 → 部件账目，随 metrics 下发）。"""

    model_config = ConfigDict(frozen=True)

    stage_index: int = Field(description="级序（助推器 = 0，与 GCAT 记法对齐）")
    section: str = Field(description="§5.9 分区枚举（fin / booster 为扩展分区）")
    z_start_m: float = Field(description="分区底面在整箭坐标系的 z（m，自箭体底部起算）")
    length_m: float = Field(description="分区轴向高度（m）")
    mass_kg: float = Field(description="质量贡献（kg，§8.4 几何解析账）")
    material: str = Field(description="材料库引用")
    source_fields: tuple[str, ...] = Field(
        description="参数来源（Schema 字段路径；`i` 为该级在 stages[] 的 0 基下标）"
    )
    note: str | None = Field(default=None, description="留白注记（无质量模型的部件显式声明）")


class AssemblyCheck(BaseModel):
    """装配层单项校验结果（形状与 §5.7 的 CheckResult 同构）。"""

    model_config = ConfigDict(frozen=True)

    check: str
    severity: str = Field(description="pass / warn / fail")
    detail: str
    stage_index: int | None = None
    value: float | None = None
    expected: float | None = None
    relative_error: float | None = None

    @property
    def ok(self) -> bool:
        return self.severity != "fail"


@dataclass(frozen=True, slots=True)
class Band:
    """一个分区的布局事实（内部账目；对外经 :class:`AssemblyNode` 下发）。"""

    section: str
    z_start: float
    z_end: float
    radius_start: float
    radius_end: float
    mass_kg: float
    material: str
    source_fields: tuple[str, ...]
    note: str | None = None

    @property
    def length(self) -> float:
        return self.z_end - self.z_start


@dataclass(frozen=True, slots=True)
class StageLayout:
    """一级的布局结果（纯数值，无 OCCT）。"""

    stage_index: int
    bands: tuple[Band, ...]
    height: float
    reserved_m: float
    h_mid: float
    saving_m: float
    l_ox: float
    l_fuel: float
    h_ox_dome: float
    h_fuel_dome: float
    tank_masses: tuple[float, float]


@dataclass(frozen=True, slots=True)
class VehicleAssembly:
    """整箭装配结果：GLB 场景图 + 装配树 + 校验 + 量测。"""

    root: bd.Compound
    nodes: dict[str, AssemblyNode]
    checks: tuple[AssemblyCheck, ...]
    total_length: float
    max_radius: float
    volume: float
    saving_by_stage: dict[int, float] = field(default_factory=dict)
    fins: dict[str, Any] | None = None
    boosters: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 布局推导（纯数值，无 OCCT——布局先行，实体随后）
# ---------------------------------------------------------------------------


def _tank_material(stage: Stage, role: str) -> str:
    """箱材料引用（读 Schema 字段，不硬编码）。"""
    if role == "oxidizer":
        return stage.geometry.oxidizer_tank.material
    return stage.geometry.fuel_tank.material


def _cylinders_derived(stage: Stage) -> bool:
    """两箱柱长是否走派生路径（任一显式即非全派生）。"""
    return (
        stage.geometry.oxidizer_tank.length_m is None and stage.geometry.fuel_tank.length_m is None
    )


def plan_stage(stage: Stage) -> StageLayout:
    """推导一级的九段分区布局（自下而上，恰好铺满 ``Stage.length_m``）。

    分区高度（封头矢高 / 第 6 分区 / 仪器舱）消费
    :func:`aeroforge.perf.mass.partition_heights`——与 §8.4 几何解析账同一份
    分区高度事实（M5 第二片裁定），本模块不另算第二套。

    矢高 ≤ 允许值（§5.5 校验 3，防干涉）：共底隔板矢高不得超过
    ``级长 − 发动机高 − 两端封头占位``；显式级间舱高不得小于两箱相邻封头矢高和
    （否则两封头相互干涉）——超限即 :class:`AssemblyError`。
    """
    geometry = stage.geometry
    heights = partition_heights(stage)
    upper_role, lower_role = heights.upper_role, heights.lower_role
    diameter = stage.diameter_m
    engine_height = stage.engine_height_m
    diameters = tank_diameters_m(stage)

    # 封头矢高（OI-37 三级优先级的级层档；None 兜底 0.5 在 dome_height_m 内）
    dome_heights = {
        "oxidizer": dome_height_m(diameters["oxidizer"], stage.flatness_ratio),
        "fuel": dome_height_m(diameters["fuel"], stage.flatness_ratio),
    }

    # 中段（第 6 分区）：共底 = 隔板段（按级径）；非共底 = 级间舱（显式值优先，
    # 缺省派生 = 两箱相邻封头矢高和——partition_heights 已裁定，此处做防干涉校验）
    if geometry.common_bulkhead:
        h_bulkhead = heights.bulkhead_m
        assert h_bulkhead is not None
        h_mid = heights.h_mid_m
        allowed = (
            stage.length_m - engine_height - dome_heights[lower_role] - dome_heights[upper_role]
        )
        if h_bulkhead > allowed + GEOM_TOL:
            msg = (
                f"第 {stage.index} 级共底矢高 {h_bulkhead:.6f} m 超过允许值 {allowed:.6f} m"
                f"（级长 {stage.length_m} m − 发动机高 {engine_height} m − 两端封头占位）——"
                "隔板将与上下箱干涉（§5.5 校验 3）。请降低 flatness_ratio 或加长该级。"
            )
            raise AssemblyError(msg)
    else:
        h_bulkhead = None
        h_mid = heights.h_mid_m
        if heights.intertank_source == "user":
            dome_sum = dome_heights[lower_role] + dome_heights[upper_role]
            if h_mid < dome_sum - GEOM_TOL:
                msg = (
                    f"第 {stage.index} 级级间舱高 {h_mid:.6f} m 小于两箱相邻封头矢高和 "
                    f"{dome_sum:.6f} m——两只相邻封头将相互干涉（§5.5 校验 3 同判据）。"
                    "请增大 intertank_height_m，或省略该字段改用派生值。"
                )
                raise AssemblyError(msg)

    # 轴向预留：下箱底封头 + 中段 + 上箱顶封头 + 仪器舱（分区铺满级长的关键扣减）
    reserved = heights.reserved_m
    ox_estimate, fuel_estimate = resolve_tank_geometry(stage, reserved_m=reserved)
    lengths = {
        "oxidizer": ox_estimate.cylinder_length_m,
        "fuel": fuel_estimate.cylinder_length_m,
    }
    if not _cylinders_derived(stage):
        # 显式箱长路径：用户权威（§5.9 规则 2，不静默覆盖），仅拦截溢出
        total = engine_height + reserved + lengths["oxidizer"] + lengths["fuel"]
        if total > stage.length_m + 1e-6:
            msg = (
                f"第 {stage.index} 级分区总高 {total:.6f} m 超过级长 {stage.length_m} m——"
                "显式箱长与封头占位/发动机高不相容，请缩短箱长或加长该级"
            )
            raise AssemblyError(msg)

    # 非贮箱部位填实（M6 前置专项①，分部位干重模型）：分数闭环的各部位按
    # 分配比例落位——推力结构 → thrust_structure 带；发动机（循环 T/W 推算）
    # + 管路/增压 → engine_bay 带（级间段包容发动机时随级间段带，§5.9 共性 2）；
    # 裙段/级间段/舱段/航电等固定件按包络面积在既有带间分摊（薄壳结构质量随
    # 面积走）。各带质量之和 == §8.4 几何解析干重（两账对拍闭合）。
    breakdown = stage_dry_mass_breakdown_kg(stage, reserved_m=reserved)
    masses = {"oxidizer": breakdown.tank_oxidizer_kg, "fuel": breakdown.tank_fuel_kg}

    aft_kg = breakdown.engine_kg + breakdown.residual_plumbing_kg
    engine_bay_carries_aft = heights.engine_bay_m > GEOM_TOL
    interstage_carries_aft = (not engine_bay_carries_aft) and heights.interstage_m > GEOM_TOL
    thrust_structure_mass = breakdown.residual_thrust_structure_kg + (
        0.0 if engine_bay_carries_aft or interstage_carries_aft else aft_kg
    )

    skirt_members: list[tuple[str, float]] = []
    skirt_members.append(
        (SECTION_FORWARD_SKIRT, math.pi * (diameter / 2.0) * dome_heights[upper_role])
    )
    if not geometry.common_bulkhead:
        skirt_members.append((SECTION_INTERTANK, math.pi * (diameter / 2.0) * h_mid))
    if heights.avionics_m > GEOM_TOL:
        skirt_members.append((SECTION_AVIONICS, math.pi * (diameter / 2.0) * heights.avionics_m))
    if heights.interstage_m > GEOM_TOL and not interstage_carries_aft:
        skirt_members.append(
            (SECTION_INTERSTAGE, math.pi * (diameter / 2.0) * heights.interstage_m)
        )
    skirt_area_total = sum(area for _, area in skirt_members)
    skirt_share = {
        section: breakdown.residual_skirts_misc_kg * area / skirt_area_total
        for section, area in skirt_members
    }

    saving_m = (
        dome_heights[upper_role] + dome_heights[lower_role] - h_bulkhead
        if h_bulkhead is not None
        else 0.0
    )

    # ── 自下而上铺带 ──
    bands: list[Band] = []
    z = 0.0
    stage_prefix = f"stages[{stage.index - 1}]"
    part_note = (
        "非贮箱部位质量（M6 前置专项①分部位干重模型：占级干重分数闭环，"
        "参数表见 perf.mass；与 §8.4 几何解析账同源）"
    )

    def _add(
        section: str,
        length: float,
        radius_start: float,
        radius_end: float,
        mass: float,
        material: str,
        source: tuple[str, ...],
        note: str | None = None,
    ) -> None:
        nonlocal z
        bands.append(
            Band(
                section=section,
                z_start=z,
                z_end=z + length,
                radius_start=radius_start,
                radius_end=radius_end,
                mass_kg=mass,
                material=material,
                source_fields=source,
                note=note,
            )
        )
        z += length

    # 级间段（§5.9 共性 2，M5 第四片）：位于本级布局的**最底部**——顶接本级
    # 发动机舱段下缘、底接下级前裙上缘（剖面图中在上级 engine_bay 之下、
    # 下级 forward_skirt 之上）。长度预算计入本级 length_m（从分区账划出，非加高）；
    # 未声明（None / 0）不产带、不产节点——现状字节不变（§9.2）
    if heights.interstage_m > GEOM_TOL:
        interstage_source = (
            (f"{stage_prefix}.interstage_height_m",) if heights.interstage_source == "user" else ()
        )
        housed_note = (
            "；本级发动机（喷管）伸入本段——发动机舱段已让位包容"
            if heights.engine_bay_m <= GEOM_TOL
            else ""
        )
        if interstage_carries_aft:
            interstage_note = (
                "两级之间的级间段（不是同级两箱间的级间舱）；"
                "发动机舱段被级间段包容——发动机 + 管路/增压质量落位于本带" + housed_note
            )
            interstage_mass = aft_kg
        else:
            interstage_note = (
                "两级之间的级间段（不是同级两箱间的级间舱）；裙段/级间段/舱段类固定件按包络面积分摊"
            )
            interstage_mass = skirt_share[SECTION_INTERSTAGE]
        _add(
            SECTION_INTERSTAGE,
            heights.interstage_m,
            diameter / 2.0,
            diameter / 2.0,
            interstage_mass,
            stage.material,
            (f"{stage_prefix}.diameter_m", *interstage_source),
            note=interstage_note,
        )
    # 发动机舱段：级间段让位包容后的余量（级间段完全包容发动机时为 0 高——
    # 0 高分区不产带、不产节点，OI-33 演化口径）；带质量 = 发动机（循环 T/W
    # 惯例推算）+ 管路/增压分数份额
    if heights.engine_bay_m > GEOM_TOL:
        _add(
            SECTION_ENGINE_BAY,
            heights.engine_bay_m,
            diameter / 2.0,
            diameter / 2.0,
            aft_kg,
            stage.material,
            (
                f"{stage_prefix}.engine_height_m",
                f"{stage_prefix}.engine.thrust_vacuum_n",
                f"{stage_prefix}.engine_count",
                f"{stage_prefix}.diameter_m",
                *(
                    (f"{stage_prefix}.interstage_height_m",)
                    if heights.interstage_m > GEOM_TOL
                    else ()
                ),
            ),
            note=part_note + "；发动机按循环 T/W 惯例推算，管路/增压按分数份额；"
            "喷管钟形外形见 s<级>-nozzle[-<k>] 节点（自推进参数派生）",
        )
    _add(
        SECTION_THRUST_STRUCTURE,
        dome_heights[lower_role],
        diameter / 2.0,
        diameter / 2.0,
        thrust_structure_mass,
        stage.material,
        (f"{stage_prefix}.flatness_ratio", f"{stage_prefix}.diameter_m"),
        note=part_note + "（推力结构/机架份额）",
    )
    # 下箱柱段（储箱排列决定角色——读字段，§5.9 非铁律）
    _add(
        _ROLE_SECTION[lower_role],
        lengths[lower_role],
        diameters[lower_role] / 2.0,
        diameters[lower_role] / 2.0,
        masses[lower_role],
        _tank_material(stage, lower_role),
        (
            f"{stage_prefix}.geometry.{lower_role}_tank.length_m",
            f"{stage_prefix}.geometry.{lower_role}_tank.diameter_m",
            f"{stage_prefix}.engine.mixture_ratio",
            f"{stage_prefix}.flatness_ratio",
        ),
    )
    # 第 6 分区：非共底 = 级间舱（容纳两只相邻封头）；共底 = 隔板段（凹向上箱椭球面）
    if geometry.common_bulkhead:
        assert h_bulkhead is not None
        _add(
            SECTION_BULKHEAD,
            h_mid,
            diameter / 2.0,
            diameter / 2.0,
            breakdown.bulkhead_kg,
            _tank_material(stage, "fuel"),
            (
                f"{stage_prefix}.flatness_ratio",
                f"{stage_prefix}.diameter_m",
                f"{stage_prefix}.geometry.common_bulkhead_type",
            ),
            note="独立薄壳节点 s<级>-common-bulkhead（壳厚 = 两侧壁厚和；隔板面已过四校验）",
        )
    else:
        intertank_source = (
            (f"{stage_prefix}.intertank_height_m",) if heights.intertank_source == "user" else ()
        )
        _add(
            SECTION_INTERTANK,
            h_mid,
            diameter / 2.0,
            diameter / 2.0,
            skirt_share[SECTION_INTERTANK],
            stage.material,
            (f"{stage_prefix}.flatness_ratio", f"{stage_prefix}.diameter_m", *intertank_source),
            note=part_note + "（级间舱：裙段/舱段类固定件按包络面积分摊）",
        )
    # 上箱柱段
    _add(
        _ROLE_SECTION[upper_role],
        lengths[upper_role],
        diameters[upper_role] / 2.0,
        diameters[upper_role] / 2.0,
        masses[upper_role],
        _tank_material(stage, upper_role),
        (
            f"{stage_prefix}.geometry.{upper_role}_tank.length_m",
            f"{stage_prefix}.geometry.{upper_role}_tank.diameter_m",
            f"{stage_prefix}.engine.mixture_ratio",
            f"{stage_prefix}.flatness_ratio",
        ),
    )
    _add(
        SECTION_FORWARD_SKIRT,
        dome_heights[upper_role],
        diameter / 2.0,
        diameter / 2.0,
        skirt_share[SECTION_FORWARD_SKIRT],
        stage.material,
        (f"{stage_prefix}.flatness_ratio", f"{stage_prefix}.diameter_m"),
        note=part_note + "（前裙：裙段/舱段类固定件按包络面积分摊）",
    )
    # avionics（第 3 分区，级顶）：显式值优先（M5 第二片 Schema 增补）；缺省 0 高
    # （§5.9 允许）——0 高不产带、不产节点（部件缺失即无节点，OI-33 演化口径）；
    # 显式存在时参与裙段/舱段类固定件的包络面积分摊
    if heights.avionics_m > GEOM_TOL:
        _add(
            SECTION_AVIONICS,
            heights.avionics_m,
            diameter / 2.0,
            diameter / 2.0,
            skirt_share[SECTION_AVIONICS],
            stage.material,
            (f"{stage_prefix}.avionics_height_m", f"{stage_prefix}.diameter_m"),
            note=part_note + "（仪器舱/舱段固定件按包络面积分摊）",
        )

    return StageLayout(
        stage_index=stage.index,
        bands=tuple(bands),
        height=z,
        reserved_m=reserved,
        h_mid=h_mid,
        saving_m=saving_m,
        l_ox=lengths["oxidizer"],
        l_fuel=lengths["fuel"],
        h_ox_dome=dome_heights["oxidizer"],
        h_fuel_dome=dome_heights["fuel"],
        tank_masses=(masses["oxidizer"], masses["fuel"]),
    )


# ---------------------------------------------------------------------------
# 实体生成（OCCT）
# ---------------------------------------------------------------------------


def _cylinder_solid(radius: float, z_start: float, height: float, label: str) -> bd.Solid:
    solid = bd.Solid.make_cylinder(radius, height, bd.Plane(origin=(0.0, 0.0, z_start)))
    solid.label = label
    return solid


def _cone_solid(
    radius_bottom: float, radius_top: float, z_start: float, height: float, label: str
) -> bd.Solid:
    # 两底半径相等时退化为圆柱（如整流罩直径 = 上面级直径的构型）——内核拒绝等径圆锥
    if abs(radius_top - radius_bottom) <= GEOM_TOL:
        return _cylinder_solid(radius_bottom, z_start, height, label)
    solid = bd.Solid.make_cone(
        radius_bottom, radius_top, height, bd.Plane(origin=(0.0, 0.0, z_start))
    )
    solid.label = label
    return solid


def _dome_solid(radius: float, height: float, label: str) -> bd.Solid:
    """半椭球穹顶实体（底面在 z=0、顶点在 z=height）：与共底四校验的 cap 同构。"""
    profile = MeridianProfile(
        name=label,
        base_radius=radius,
        segments=(EllipseSegment(length=height, end_radius=0.0),),
    )
    solid = build_solid(profile)
    solid.label = label
    return solid


def build_bulkhead_shell(stage: Stage, layout: StageLayout, z_offset: float) -> bd.Solid:
    """共底隔板独立薄壳（M5 第四片，交付 6）：``s<级>-common-bulkhead``。

    壳厚 = **两侧壁厚和**（氧化剂箱 + 燃料箱壁厚，§5.9 共性 3 的隔热夹层口径以
    壁厚和作工程近似）；几何 = 外半椭球穹顶 − 半轴各内缩同厚的内穹顶（近似等距
    壳：顶点处为竖直厚度 t、底缘处为径向厚度 t，其余部位介于两者之间——注记随
    节点下发，不做 OCCT 偏置的精确等距）。隔板面本身已过 §5.5 四校验。
    """
    radius = stage.diameter_m / 2.0
    height = layout.h_mid
    thickness = (
        stage.geometry.oxidizer_tank.wall_thickness_m + stage.geometry.fuel_tank.wall_thickness_m
    )
    outer = _dome_solid(radius, height, "bulkhead-outer")
    inner = _dome_solid(
        max(radius - thickness, GEOM_TOL * 10.0),
        max(height - thickness, GEOM_TOL * 10.0),
        "bulkhead-inner",
    )
    shell = outer.cut(inner)
    shell = shell.moved(bd.Location((0.0, 0.0, z_offset)))
    shell.label = section_node_name(stage.index, SECTION_BULKHEAD)
    return shell


# ---------------------------------------------------------------------------
# 喷管钟形派生（交付 5，§5.3 钟形曲线族 + §6.1 Engine 推进参数）
# ---------------------------------------------------------------------------

#: 喷管推力系数 C_F（**工程惯例值，非权威来源**）：喉部面积由
#: ``A_t = F_vac / (Pc·C_F)`` 反推。真实 C_F 依膨胀比 / 环境压在 1.5–2.0 间，
#: 取 1.65 为海平面~真空的中间惯例值；派生结果在节点 metadata 注明该惯例。
NOZZLE_THRUST_COEFFICIENT = 1.65

#: 钟长比缺省口径（Rao 常用值）：钟长 = 80% L₁₅°。
NOZZLE_BELL_LENGTH_RATIO = 0.8

#: 钟长比下限：可用占高不足 15% 钟长时放弃派生（型面退化为近直锥，无可视价值）。
NOZZLE_BELL_MIN_RATIO = 0.15


@dataclass(frozen=True, slots=True)
class NozzleGeometry:
    """自推进参数派生的单个喷管钟形（纯数值，无 OCCT）。"""

    throat_radius_m: float
    exit_radius_m: float
    length_m: float
    length_ratio: float
    """实际采用的钟长比（80% Rao 放不下时按可用发动机占高收敛）。"""

    clamped: bool
    """钟长比是否因可用占高不足而收敛（true = 非 80% Rao，注记随节点下发）。"""

    placement_radius_m: float
    """发动机布置圆半径（m）：单管 = 0（轴心）；多管 = 出口半径 / sin(π/n)
    （钟间相切），超出箭体半径时收敛到 ``R_级 − R_出口``（钟间轻微重叠，注记）。"""


def derive_nozzle_geometry(stage: Stage, stage_radius_m: float) -> NozzleGeometry | None:
    """从推进参数派生喷管钟形（交付 5）。

    - 喉部半径 ``rt = √(F_vac / (Pc·π·C_F))``，C_F = 1.65（工程惯例，见
      :data:`NOZZLE_THRUST_COEFFICIENT`；真空推力只配室压的工作点口径）；
    - 出口半径 ``Re = rt·√ε``（ε 取 ``Engine.expansion_ratio``，工作点口径）；
    - 钟长 = 80% Rao（复用 :mod:`aeroforge.geometry.meridian` 的钟形曲线族；
      可用占高 = 发动机高度 ``engine_height_m``，放不下时收敛长度比并注记）。

    参数缺失 / 派生退化（喉部非正、出口不大于喉部、钟长比越下限）→ 返回 ``None``
    （不生成节点 + 留白注记，不编造，§1.4-4）。
    """
    engine = stage.engine
    thrust_vac = engine.thrust_vacuum_n
    chamber_pressure = engine.chamber_pressure_pa
    expansion = engine.expansion_ratio
    # Schema 已保证三者恒有且为正（gt=0 / gt=1.0）——此处防御性留白而非编造
    if thrust_vac <= 0.0 or chamber_pressure <= 0.0 or expansion <= 1.0:
        return None
    throat = math.sqrt(thrust_vac / (chamber_pressure * math.pi * NOZZLE_THRUST_COEFFICIENT))
    exit_radius = throat * math.sqrt(expansion)
    if exit_radius <= throat + GEOM_TOL:
        return None
    length_15deg = (exit_radius - throat) / math.tan(math.radians(15.0))
    available = stage.engine_height_m
    ratio = min(NOZZLE_BELL_LENGTH_RATIO, available / length_15deg)
    if ratio < NOZZLE_BELL_MIN_RATIO:
        return None
    length = ratio * length_15deg
    count = stage.engine_count
    if count <= 1:
        placement = 0.0
    else:
        tangent_circle = exit_radius / math.sin(math.pi / count)  # 钟间相切的布置圆
        placement = min(tangent_circle, max(stage_radius_m - exit_radius, 0.0))
    return NozzleGeometry(
        throat_radius_m=throat,
        exit_radius_m=exit_radius,
        length_m=length,
        length_ratio=ratio,
        clamped=ratio < NOZZLE_BELL_LENGTH_RATIO - 1e-12,
        placement_radius_m=placement,
    )


def _nozzle_profile(nozzle: NozzleGeometry) -> MeridianProfile:
    """喷管钟形的母线剖面：出口（底）→ 喉部（顶），复用钟形曲线族。"""
    return MeridianProfile(
        name="nozzle",
        base_radius=nozzle.exit_radius_m,
        segments=(
            BellNozzleSegment(
                length=nozzle.length_m,
                end_radius=nozzle.throat_radius_m,
                throat_radius=nozzle.throat_radius_m,
                length_ratio=nozzle.length_ratio,
            ),
        ),
    )


def build_nozzle_solids(
    stage: Stage, stage_z_bottom: float, stage_radius_m: float
) -> list[tuple[str, bd.Solid]]:
    """一级的喷管实体（具名 ``s<级>-nozzle`` / ``s<级>-nozzle-<k>``，交付 5）。

    多管发动机（``engine_count > 1``）按 :func:`derive_nozzle_geometry` 的布置圆
    周向均布（与 M4 助推器同惯例：自 +X 轴起 2πk/n）；单管置于轴心。派生退化
    （参数缺失 / 占高不足）→ 空列表（留白注记由调用方下发给装配树）。
    """
    nozzle = derive_nozzle_geometry(stage, stage_radius_m)
    if nozzle is None:
        return []
    solid_template = build_solid(_nozzle_profile(nozzle))
    count = stage.engine_count
    solids: list[tuple[str, bd.Solid]] = []
    for index in range(count):
        angle = 2.0 * math.pi * index / count
        center = (
            nozzle.placement_radius_m * math.cos(angle),
            nozzle.placement_radius_m * math.sin(angle),
            stage_z_bottom,
        )
        placed = solid_template.moved(bd.Location(center))
        label = f"s{stage.index}-nozzle" if count == 1 else f"s{stage.index}-nozzle-{index}"
        placed.label = label
        solids.append((label, placed))
    return solids


def nozzle_source_fields(stage: Stage) -> tuple[str, ...]:
    """喷管派生来源字段（装配树 metadata，§5.5「参数来源」口径）。"""
    prefix = f"stages[{stage.index - 1}]"
    return (
        f"{prefix}.engine.thrust_vacuum_n",
        f"{prefix}.engine.chamber_pressure_pa",
        f"{prefix}.engine.expansion_ratio",
        f"{prefix}.engine_count",
    )


def nozzle_note(stage: Stage, stage_radius_m: float) -> str:
    """喷管节点的派生注记：公式、惯例值与收敛情况（不编造，§1.4-4）。"""
    nozzle = derive_nozzle_geometry(stage, stage_radius_m)
    if nozzle is None:
        return (
            "喷管钟形未派生：推进参数缺失或可用发动机占高不足（钟长比越下限"
            f" {NOZZLE_BELL_MIN_RATIO}）——留白不编造"
        )
    ratio_note = (
        "80% Rao"
        if not nozzle.clamped
        else f"长度比收敛为 {nozzle.length_ratio:.4f}（80% Rao 放不下，可用占高 "
        f"{stage.engine_height_m:.3f} m）"
    )
    overlap_note = ""
    if (
        stage.engine_count > 1
        and nozzle.placement_radius_m > GEOM_TOL
        and nozzle.placement_radius_m
        < nozzle.exit_radius_m / math.sin(math.pi / stage.engine_count) - 1e-9
    ):
        overlap_note = "；布置圆收敛到级半径内，钟间可能轻微重叠（工程近似）"
    return (
        f"自推进参数派生：rt=√(F_vac/(Pc·π·C_F))={nozzle.throat_radius_m:.4f} m"
        f"（C_F={NOZZLE_THRUST_COEFFICIENT}，工程惯例非权威）、Re=rt·√ε="
        f"{nozzle.exit_radius_m:.4f} m、钟长 {nozzle.length_m:.3f} m（{ratio_note}）" + overlap_note
    )


def _fairing_profile(diameter: float, height: float) -> MeridianProfile:
    """整流罩母线：柱段 + 切线卵形顶（基底相切 ⇒ G1，§5.3）。"""
    radius = diameter / 2.0
    cylinder_height = height * _FAIRING_CYLINDER_FRACTION
    nose_height = height - cylinder_height
    return MeridianProfile(
        name="fairing",
        base_radius=radius,
        segments=(
            LineSegment(length=cylinder_height, end_radius=radius),
            TangentOgiveSegment(length=nose_height, end_radius=0.0),
        ),
    )


def fairing_adapter_heights(vehicle: Vehicle) -> tuple[float, float] | None:
    """顶级整流罩 / 适配器段高 ``(适配器高, 整流罩高)``；无整流罩返回 ``None``。

    整流罩高：``Vehicle.fairing_height_m`` **显式值优先**（M5 第二片 Schema 增补）；
    缺省按工程惯例常量 ``min(max(2.2×直径, 5), 20)`` m——assembly 现状值保持不变
    （惯例值提示只进诊断查询，不做常驻 warning 噪声）。适配器高无 Schema 输入，
    维持惯例常量。装配树与 2D 分区下发（sections 端点）共同消费本函数——单一实现。
    """
    if vehicle.fairing_diameter_m is None:
        return None
    fairing_height = vehicle.fairing_height_m
    if fairing_height is None:
        fairing_height = min(
            max(_FAIRING_HEIGHT_DIAMETER_RATIO * vehicle.fairing_diameter_m, _FAIRING_HEIGHT_MIN_M),
            _FAIRING_HEIGHT_MAX_M,
        )
    adapter_height = min(
        max(_ADAPTER_HEIGHT_DIAMETER_RATIO * vehicle.fairing_diameter_m, _ADAPTER_HEIGHT_MIN_M),
        _ADAPTER_HEIGHT_MAX_M,
    )
    return adapter_height, fairing_height


def vehicle_core_height_m(vehicle: Vehicle) -> float:
    """芯级整箭总高（m，含顶级整流罩 / 适配器；**不含助推器**）。

    与 sections 端点 ``dimensions.total_length_m``、装配树 ``total_length``
    同口径同源（Σ 级装配高 + 适配器 + 整流罩）——整箭数据面板（FR-10）与
    sections 共用同一份高度事实（M5 第三片抽出，单一实现）。
    """
    total = sum(plan_stage(stage).height for stage in sorted(vehicle.stages, key=lambda s: s.index))
    top = fairing_adapter_heights(vehicle)
    if top is not None:
        total += top[0] + top[1]
    return total


def build_stage_solids(
    stage: Stage, layout: StageLayout, z_offset: float
) -> list[tuple[str, bd.Solid]]:
    """一级的分区实体（按布局带逐段生成，节点名 = ``s<级序>-<分区>``）。

    0 高分区（avionics / 被级间段包容的 engine_bay 等）不产出节点——部件缺失即无
    节点，前端按名寻址（OI-33 空轮廓机制在真部件下的演化，"保留下标对齐"语义改为
    "保序"）。共底隔板带（``bulkhead``）特例：实体为**独立薄壳**（交付 6，M5 第四片），
    而非带的柱段包络。
    """
    solids: list[tuple[str, bd.Solid]] = []
    for band in layout.bands:
        if band.length <= GEOM_TOL:
            continue
        label = section_node_name(stage.index, band.section)
        if band.section == SECTION_BULKHEAD:
            solids.append((label, build_bulkhead_shell(stage, layout, z_offset + band.z_start)))
        elif abs(band.radius_start - band.radius_end) <= GEOM_TOL:
            solids.append(
                (
                    label,
                    _cylinder_solid(band.radius_start, z_offset + band.z_start, band.length, label),
                )
            )
        else:
            solids.append(
                (
                    label,
                    _cone_solid(
                        band.radius_start,
                        band.radius_end,
                        z_offset + band.z_start,
                        band.length,
                        label,
                    ),
                )
            )
    return solids


# ---------------------------------------------------------------------------
# 纯数值节点枚举（装配树与分离时序共用的单一事实源）
# ---------------------------------------------------------------------------


def vehicle_node_index(vehicle: Vehicle) -> dict[str, tuple[int, str]]:
    """整箭装配节点的**纯数值**枚举：``节点名 → (级序, 分区)``——无 OCCT。

    与 :func:`build_assembly` 的节点产出同源（分区带 / 尾翼 / 顶级罩 / 助推器 /
    喷管 / 共底隔板壳），供 **不触内核的消费者** 复用——分离时序的
    ``surviving_nodes``（§8.9）按级序 / 分区元数据推导存留节点，绝不硬编码
    节点名清单。:func:`build_assembly` 内部以意图断言钉住两者一致（漂移即报错）。
    """
    index: dict[str, tuple[int, str]] = {}
    fin_count_total = 0
    booster_total = 0
    for stage in vehicle.stages:
        layout = plan_stage(stage)
        for band in layout.bands:
            if band.length <= GEOM_TOL:
                continue
            index[section_node_name(stage.index, band.section)] = (
                stage.index,
                band.section,
            )
        # 喷管（交付 5）：具名 s<级>-nozzle[-<k>]，与 build_nozzle_solids 同规则
        nozzle = derive_nozzle_geometry(stage, stage.diameter_m / 2.0)
        if nozzle is not None:
            if stage.engine_count <= 1:
                index[f"s{stage.index}-nozzle"] = (stage.index, "nozzle")
            else:
                for k in range(stage.engine_count):
                    index[f"s{stage.index}-nozzle-{k}"] = (stage.index, "nozzle")
        # 尾翼（fin-<k> 全局编号）
        if stage.geometry.fins_enabled and stage.geometry.fin_count:
            for k in range(stage.geometry.fin_count):
                index[f"fin-{fin_count_total + k}"] = (stage.index, "fin")
            fin_count_total += stage.geometry.fin_count
    # 顶级：载荷适配器 + 整流罩（有整流罩时）
    top_stage = vehicle.stages[-1]
    if fairing_adapter_heights(vehicle) is not None:
        index[section_node_name(top_stage.index, SECTION_ADAPTER)] = (
            top_stage.index,
            SECTION_ADAPTER,
        )
        index[section_node_name(top_stage.index, SECTION_FAIRING)] = (
            top_stage.index,
            SECTION_FAIRING,
        )
    # 助推器（M4 形态沿用：booster-<k> 全局编号，级号 0）
    for group in vehicle.boosters:
        for k in range(group.count):
            index[f"booster-{booster_total + k}"] = (0, "booster")
        booster_total += group.count
    return index


# ---------------------------------------------------------------------------
# 共底四校验（§5.5）
# ---------------------------------------------------------------------------


def _bulkhead_checks(stage: Stage, layout: StageLayout) -> list[AssemblyCheck]:
    """共底四校验：G1 / 容积守恒 / 矢高（防干涉）/ LH₂ 隔热。

    校验 1（G1）：隔板椭球弧在半径端的切向严格竖直（四分之一椭圆弧的几何事实），
    与柱段壁相切——实测角即证。
    校验 2（容积守恒，容差 0.5%）：**内核实测**的隔板穹顶体积代入两箱分割账
    （下箱得穹顶、上箱得碗，二者之和 = 隔板段柱体 πR²h_b），加上两箱柱段与两端
    封头，与解析"柱段包络 + 封头"公式对拍（等径两箱时严格闭合；内核-解析两侧
    算法无关，§5.7 的对照形态）。
    校验 3（矢高 ≤ 允许值）：超限已在 :func:`plan_stage` 以硬错误拦截，此处留痕。
    校验 4（LH₂ 隔热）：缺失 → warning（参数层另有 HARD 级裁定，双保险）。
    """
    checks: list[AssemblyCheck] = []
    diameter = stage.diameter_m
    radius = diameter / 2.0
    h_b = layout.h_mid
    upper_role, lower_role = tank_roles(stage)
    diameters = tank_diameters_m(stage)
    d_lower, d_upper = diameters[lower_role], diameters[upper_role]
    l_lower = layout.l_ox if lower_role == "oxidizer" else layout.l_fuel
    l_upper = layout.l_ox if upper_role == "oxidizer" else layout.l_fuel
    h_lo_bot = dome_height_m(d_lower, stage.flatness_ratio)
    h_hi_top = dome_height_m(d_upper, stage.flatness_ratio)

    # 校验 1：G1（隔板弧半径端切向 vs 竖直）
    cap_profile = MeridianProfile(
        name=f"s{stage.index}-bulkhead-cap",
        base_radius=radius,
        segments=(EllipseSegment(length=h_b, end_radius=0.0),),
    )
    cap_resolved = resolve(cap_profile)
    radius_end_tangent = cap_resolved.segments[0].start_tangent
    angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, radius_end_tangent[1]))))
    checks.append(
        AssemblyCheck(
            check="共底校验 1：隔板-柱段 G1",
            severity="pass" if angle_deg <= 0.5 else "fail",
            detail=(
                f"隔板椭球弧在半径端的切向与竖直夹角 {angle_deg:.6f}°（容差 0.5°）——"
                "隔板与柱段壁相切（§5.5 校验 1）"
            ),
            stage_index=stage.index,
            value=angle_deg,
            expected=0.0,
        )
    )

    # 校验 2：容积守恒（内核实测隔板体积 → 分割账 → 柱段包络对拍）
    cap_volume_kernel = build_solid(cap_profile).volume
    cap_cylinder = math.pi * radius**2 * h_b
    v_lower = (
        math.pi * (d_lower / 2.0) ** 2 * l_lower
        + dome_volume_m3(d_lower, h_lo_bot)
        + cap_volume_kernel
    )
    v_upper = (
        math.pi * (d_upper / 2.0) ** 2 * l_upper
        + dome_volume_m3(d_upper, h_hi_top)
        + (cap_cylinder - cap_volume_kernel)
    )
    envelope = (
        math.pi * radius**2 * (l_lower + h_b + l_upper)
        + dome_volume_m3(d_lower, h_lo_bot)
        + dome_volume_m3(d_upper, h_hi_top)
    )
    conservation_error = (
        abs(v_lower + v_upper - envelope) / envelope if envelope > GEOM_TOL else math.inf
    )
    checks.append(
        AssemblyCheck(
            check="共底校验 2：容积守恒",
            severity="pass" if conservation_error <= BULKHEAD_VOLUME_TOL else "fail",
            detail=(
                f"下箱 {v_lower:.6f} m³ + 上箱 {v_upper:.6f} m³（隔板占体取内核实测 "
                f"{cap_volume_kernel:.6f} m³，解析穹顶 {dome_volume_m3(diameter, h_b):.6f} m³）"
                f"vs 柱段包络 {envelope:.6f} m³，相对误差 {conservation_error:.3e}"
                f"（门禁 ≤ {BULKHEAD_VOLUME_TOL:g}，等径两箱时严格闭合）"
            ),
            stage_index=stage.index,
            value=v_lower + v_upper,
            expected=envelope,
            relative_error=conservation_error,
        )
    )

    # 校验 3 的留痕（超限已在 plan_stage 以 AssemblyError 拦截，此处登记实测值）
    allowed = stage.length_m - stage.engine_height_m - h_lo_bot - h_hi_top
    checks.append(
        AssemblyCheck(
            check="共底校验 3：矢高 ≤ 允许值",
            severity="pass" if h_b <= allowed + GEOM_TOL else "fail",
            detail=(
                f"隔板矢高 {h_b:.6f} m（flatness="
                f"{'0.5（兜底）' if stage.flatness_ratio is None else stage.flatness_ratio}"
                f" × 级径 {diameter} m / 2），允许值 {allowed:.6f} m（防上下箱干涉）"
            ),
            stage_index=stage.index,
            value=h_b,
            expected=allowed,
        )
    )

    # 校验 4：LH₂ 侧隔热层（缺失 → warning；参数层另有 HARD_BULKHEAD_INSULATION_MISSING）
    insulation = stage.geometry.fuel_tank.common_bulkhead_insulation_m
    if fuel_is_lh2(stage.propellant) and insulation is None:
        checks.append(
            AssemblyCheck(
                check="共底校验 4：LH₂ 侧隔热层",
                severity="warn",
                detail=(
                    "燃料为液氢且未给出共底隔热层厚度——共享隔板不隔温会把另一侧推进剂"
                    "冻住（§5.9 口径 2③；参数层已另发 HARD_BULKHEAD_INSULATION_MISSING）"
                ),
                stage_index=stage.index,
            )
        )
    else:
        checks.append(
            AssemblyCheck(
                check="共底校验 4：LH₂ 侧隔热层",
                severity="pass",
                detail=(
                    "液氢侧隔热层厚度 "
                    f"{insulation if insulation is not None else '—（燃料非液氢，不适用）'}"
                ),
                stage_index=stage.index,
            )
        )
    return checks


# ---------------------------------------------------------------------------
# 尾翼（§5.5）
# ---------------------------------------------------------------------------


def _fin_airfoil_points(airfoil: str, chord: float, thickness: float) -> list[tuple[float, float]]:
    """翼型剖面多边形（(z_offset, y)——z=0 为**后缘**（该级底面方向）、z=chord 为
    前缘（向上/前），厚度沿 ±y）。"""
    half = thickness / 2.0
    if airfoil == "flat":
        return [(0.0, half), (chord, half), (chord, -half), (0.0, -half)]
    if airfoil == "wedge":
        # 楔形：前缘尖、后缘全厚（超声速翼型）
        return [(0.0, half), (0.0, -half), (chord, 0.0)]
    if airfoil == "double_wedge":
        # 双楔（菱形）：前后缘均尖、最大厚度在中弦
        return [(0.0, 0.0), (chord / 2.0, half), (chord, 0.0), (chord / 2.0, -half)]
    msg = f"未知翼型 {airfoil!r}（合法域 flat / wedge / double_wedge）"
    raise AssemblyError(msg)


def build_fin_solids(
    stage: Stage, stage_z_bottom: float, stage_radius: float, start_index: int
) -> list[tuple[str, bd.Solid]]:
    """一级的尾翼实体（周向等角均布 + 根部倒圆），节点名 ``fin-<k>``（全局编号）。

    放置：翼根弦的**后缘**落在该级底面（z = stage_z_bottom），前缘向上；梢部整剖面
    按后掠角后倾 ``span·tan(Λ)``；厚度按工程惯例 ``0.04 × 弦长`` 随弦比例收敛。
    """
    geometry = stage.geometry
    root_chord = geometry.fin_root_chord_m
    tip_chord = geometry.fin_tip_chord_m
    span = geometry.fin_span_m
    sweep_deg = geometry.fin_sweep_deg
    airfoil = geometry.fin_airfoil
    fin_count = geometry.fin_count
    roll_deg = geometry.fin_roll_deg
    assert root_chord is not None and tip_chord is not None and span is not None
    assert sweep_deg is not None and airfoil is not None and fin_count is not None
    assert roll_deg is not None
    sweep = math.radians(sweep_deg)

    root_thickness = FIN_THICKNESS_RATIO * root_chord
    tip_thickness = FIN_THICKNESS_RATIO * tip_chord
    sweep_drop = span * math.tan(sweep)  # 梢部整剖面的后倾量

    root_wire = bd.Wire.make_polygon(
        [
            (stage_radius, y, stage_z_bottom + z)
            for z, y in _fin_airfoil_points(airfoil, root_chord, root_thickness)
        ],
        close=True,
    )
    tip_wire = bd.Wire.make_polygon(
        [
            (stage_radius + span, y, stage_z_bottom + sweep_drop + z)
            for z, y in _fin_airfoil_points(airfoil, tip_chord, tip_thickness)
        ],
        close=True,
    )
    fin = bd.Solid.make_loft([root_wire, tip_wire])
    if fin is None:
        msg = "尾翼放样失败：检查翼型 / 弦长 / 展长参数"
        raise AssemblyError(msg)

    # 根部倒圆（§5.5：局部圆角，避免根部应力集中）
    fillet_radius = FIN_ROOT_FILLET_RATIO * root_thickness
    root_face = min(fin.faces(), key=lambda face: face.center().X)
    fin = fin.fillet(fillet_radius, root_face.edges())

    fins: list[tuple[str, bd.Solid]] = []
    for index in range(fin_count):
        angle_deg = roll_deg + 360.0 * index / fin_count
        placed = fin.rotate(bd.Axis.Z, angle_deg)
        label = f"fin-{start_index + index}"
        placed.label = label
        fins.append((label, placed))
    return fins


# ---------------------------------------------------------------------------
# 整箭装配
# ---------------------------------------------------------------------------


def build_assembly(vehicle: Vehicle) -> VehicleAssembly:
    """车辆形态构建入口：Stage 参数 → 九段分区装配树 + GLB 场景图。

    场景图：``vehicle``（根，**不持 mesh**）→ ``s<级序>-<分区>`` / ``fin-<k>`` /
    ``booster-<k>`` 各持一个 mesh（OI-33 机制的 M5 演化：节点名 = 分区名）。
    """
    nodes: dict[str, AssemblyNode] = {}
    children: list[bd.Solid | bd.Part] = []
    checks: list[AssemblyCheck] = []
    saving_by_stage: dict[int, float] = {}
    fin_index = 0
    fins_summary: dict[str, Any] | None = None
    z_offset = 0.0
    core_max_radius = 0.0

    for stage in vehicle.stages:
        layout = plan_stage(stage)

        # 分区铺满意图断言（§5.7：总高 = 各分区之和）
        band_sum = sum(band.length for band in layout.bands)
        if abs(band_sum - layout.height) > 1e-9:
            msg = (
                f"第 {stage.index} 级分区总高 {band_sum:.9f} ≠ 级装配高 {layout.height:.9f}"
                "（布局不自洽，这是几何公式缺陷，请上报）"
            )
            raise AssemblyError(msg)
        checks.append(
            AssemblyCheck(
                check="意图断言：分区铺满级高",
                severity="pass",
                detail=f"第 {stage.index} 级 Σ分区高 = 级装配高 = {layout.height:.9f} m",
                stage_index=stage.index,
                value=band_sum,
                expected=layout.height,
            )
        )

        if stage.geometry.common_bulkhead:
            checks.extend(_bulkhead_checks(stage, layout))
            saving_by_stage[stage.index] = layout.saving_m
            checks.append(
                AssemblyCheck(
                    check="共底级长缩减量（saving）",
                    severity="pass",
                    detail=(
                        f"第 {stage.index} 级共底缩减 {layout.saving_m:.6f} m"
                        "（两只相邻封头矢高和 − 隔板矢高，公式反算，§5.9 口径 2）"
                    ),
                    stage_index=stage.index,
                    value=layout.saving_m,
                )
            )

        for _label, solid in build_stage_solids(stage, layout, z_offset):
            children.append(solid)
        for band in layout.bands:
            core_max_radius = max(core_max_radius, band.radius_start, band.radius_end)
            if band.length <= GEOM_TOL:
                continue
            node = section_node_name(stage.index, band.section)
            nodes[node] = AssemblyNode(
                stage_index=stage.index,
                section=band.section,
                z_start_m=z_offset + band.z_start,
                length_m=band.length,
                mass_kg=band.mass_kg,
                material=band.material,
                source_fields=band.source_fields,
                note=band.note,
            )
        section_mass = sum(band.mass_kg for band in layout.bands)
        account_mass = dry_mass_geometric_kg(stage)
        checks.append(
            AssemblyCheck(
                check="级质量对拍（装配 vs §8.4 几何解析账）",
                severity="pass",
                detail=(
                    f"第 {stage.index} 级分区质量合计 {section_mass:.3f} kg 与 §8.4 几何解析账 "
                    f"{account_mass:.3f} kg 同源闭合（两账消费同一份 §5.9 分区高度与分部位干重"
                    "分解〔M6 前置专项①：贮箱壁/隔板/发动机/非贮箱分数分摊〕，"
                    "M5 第二片 reserved 口径差复核的裁定延续）"
                ),
                stage_index=stage.index,
                value=section_mass,
                expected=account_mass,
            )
        )

        # 尾翼（非回转体，§5.5）：fin-<k> 全局编号，挂在对应级
        if stage.geometry.fins_enabled and stage.geometry.fin_count:
            fin_solids = build_fin_solids(stage, z_offset, stage.diameter_m / 2.0, fin_index)
            per_fin_volumes = [solid.volume for _, solid in fin_solids]
            for label, solid in fin_solids:
                children.append(solid)
                nodes[label] = AssemblyNode(
                    stage_index=stage.index,
                    section="fin",
                    z_start_m=z_offset,
                    length_m=stage.geometry.fin_root_chord_m or 0.0,
                    mass_kg=0.0,
                    material=stage.material,
                    source_fields=tuple(
                        f"stages[{stage.index - 1}].geometry.fin_{name}"
                        for name in (
                            "airfoil",
                            "span_m",
                            "root_chord_m",
                            "tip_chord_m",
                            "sweep_deg",
                            "count",
                            "roll_deg",
                        )
                    ),
                    note="非回转体；体积入 metrics.fins 块（材料质量模型留白）",
                )
            fins_summary = {
                "count": fin_index + len(fin_solids),
                "per_fin_volume_m3": per_fin_volumes[0],
                "total_volume_m3": sum(per_fin_volumes),
                "airfoil": stage.geometry.fin_airfoil,
                "stage_index": stage.index,
            }
            fin_index += len(fin_solids)

        # 喷管钟形（交付 5，M5 第四片）：独立具名节点 s<级>-nozzle[-<k>]，
        # 位于该级底部（级间段 / 发动机舱段占位内），派生公式随 metadata 下发
        nozzle_solids = build_nozzle_solids(stage, z_offset, stage.diameter_m / 2.0)
        for label, solid in nozzle_solids:
            children.append(solid)
            nodes[label] = AssemblyNode(
                stage_index=stage.index,
                section="nozzle",
                z_start_m=z_offset,
                length_m=solid.bounding_box().max.Z - solid.bounding_box().min.Z,
                mass_kg=0.0,
                material=stage.material,
                source_fields=nozzle_source_fields(stage),
                note=nozzle_note(stage, stage.diameter_m / 2.0),
            )
        if not nozzle_solids:
            # 派生退化：留白注记挂在发动机舱段 / 级间段节点上（不编造，§1.4-4）
            skip_note = "；喷管钟形未派生（推进参数缺失或占高不足）——留白"
            for name in (f"s{stage.index}-engine-bay", f"s{stage.index}-interstage"):
                if name in nodes and nodes[name].note is not None:
                    nodes[name] = nodes[name].model_copy(
                        update={"note": (nodes[name].note or "") + skip_note}
                    )

        z_offset += layout.height

    # 顶级：载荷适配器 + 整流罩（有整流罩时，§5.9 表；vehicle 级输入）
    top_stage = vehicle.stages[-1]
    top_heights = fairing_adapter_heights(vehicle)
    if top_heights is not None and vehicle.fairing_diameter_m is not None:
        fairing_diameter = vehicle.fairing_diameter_m
        adapter_height, fairing_height = top_heights
        core_max_radius = max(core_max_radius, fairing_diameter / 2.0)
        fairing_explicit = vehicle.fairing_height_m is not None
        adapter_label = section_node_name(top_stage.index, SECTION_ADAPTER)
        children.append(
            _cone_solid(
                top_stage.diameter_m / 2.0,
                fairing_diameter / 2.0,
                z_offset,
                adapter_height,
                adapter_label,
            )
        )
        nodes[adapter_label] = AssemblyNode(
            stage_index=top_stage.index,
            section=SECTION_ADAPTER,
            z_start_m=z_offset,
            length_m=adapter_height,
            mass_kg=0.0,
            material=vehicle.material,
            source_fields=(
                "fairing_diameter_m",
                f"stages[{top_stage.index - 1}].diameter_m",
            ),
            note="高度为工程惯例常量（无 Schema 输入）；质量留白",
        )
        z_offset += adapter_height

        fairing_label = section_node_name(top_stage.index, SECTION_FAIRING)
        fairing = build_solid(_fairing_profile(fairing_diameter, fairing_height))
        fairing = fairing.moved(bd.Location((0.0, 0.0, z_offset)))
        fairing.label = fairing_label
        children.append(fairing)
        nodes[fairing_label] = AssemblyNode(
            stage_index=top_stage.index,
            section=SECTION_FAIRING,
            z_start_m=z_offset,
            length_m=fairing_height,
            mass_kg=0.0,
            material=vehicle.material,
            source_fields=(
                "fairing_diameter_m",
                *(("fairing_height_m",) if fairing_explicit else ()),
            ),
            note=(
                "高度为用户显式指定（柱段 + 切线卵形）；质量留白"
                if fairing_explicit
                else "高度为工程惯例常量（柱段 + 切线卵形，可显式指定 fairing_height_m）；质量留白"
            ),
        )
        z_offset += fairing_height

    # 助推器（OI-36：M4 周向均布现状沿用，M5 完整布局——径向偏移 / 自定义角位，
    # 节点名 booster-<k>，级号 0）
    boosters_summary: dict[str, Any] | None = None
    if vehicle.boosters:
        booster_solids: list[bd.Solid] = []
        for group in vehicle.boosters:
            summary = BoosterSummary(
                count=group.count,
                diameter_m=group.stage.diameter_m,
                length_m=group.stage.length_m,
                radial_offset_m=group.radial_offset_m,
                angles_deg=group.angles_deg,
            )
            booster_solids.extend(booster_cylinders_for_radius(core_max_radius, summary))
        for global_index, solid in enumerate(booster_solids):
            # 多组助推器连续编号（booster_cylinders_for_radius 按组内 0 基命名，此处改为全局）
            solid.label = f"booster-{global_index}"
            children.append(solid)
            box = solid.bounding_box()
            nodes[str(solid.label)] = AssemblyNode(
                stage_index=0,
                section="booster",
                z_start_m=box.min.Z,
                length_m=box.max.Z - box.min.Z,
                mass_kg=0.0,
                material=vehicle.material,
                source_fields=(
                    "boosters[i].stage.diameter_m",
                    "boosters[i].stage.length_m",
                    "boosters[i].count",
                ),
                note="M4 简化形态（圆柱）；体积入 metrics.boosters 块，质量留白",
            )
        volumes = [solid.volume for solid in booster_solids]
        boosters_summary = {
            "count": len(booster_solids),
            "per_booster_volume_m3": volumes[0] if volumes else 0.0,
            "total_booster_volume_m3": sum(volumes),
        }

    root = bd.Compound(children=children)
    root.label = GLB_ROOT_NAME

    # 意图断言（§5.7：防"有效但错误"）：内核路径产出的节点集合必须与纯数值
    # 枚举（vehicle_node_index，供分离时序复用）逐名一致——两处漂移即布局缺陷
    pure_index = vehicle_node_index(vehicle)
    if set(nodes) != set(pure_index):
        msg = (
            f"装配节点与纯数值枚举不一致：内核多出 {sorted(set(nodes) - set(pure_index))}、"
            f"缺少 {sorted(set(pure_index) - set(nodes))}——这是节点枚举漂移缺陷，请上报"
        )
        raise AssemblyError(msg)

    total_length = z_offset
    max_radius = max(
        [core_max_radius, *(solid.bounding_box().max.X for solid in children)], default=0.0
    )
    volume = sum(solid.volume for solid in children)

    return VehicleAssembly(
        root=root,
        nodes=nodes,
        checks=tuple(checks),
        total_length=total_length,
        max_radius=max_radius,
        volume=volume,
        saving_by_stage=saving_by_stage,
        fins=fins_summary,
        boosters=boosters_summary,
    )


__all__ = [
    "BULKHEAD_VOLUME_TOL",
    "FIN_THICKNESS_RATIO",
    "NOZZLE_BELL_LENGTH_RATIO",
    "NOZZLE_BELL_MIN_RATIO",
    "NOZZLE_THRUST_COEFFICIENT",
    "SECTION_ADAPTER",
    "SECTION_AVIONICS",
    "SECTION_BULKHEAD",
    "SECTION_ENGINE_BAY",
    "SECTION_FAIRING",
    "SECTION_FORWARD_SKIRT",
    "SECTION_FUEL_TANK",
    "SECTION_INTERSTAGE",
    "SECTION_INTERTANK",
    "SECTION_ORDER",
    "SECTION_OX_TANK",
    "SECTION_THRUST_STRUCTURE",
    "AssemblyCheck",
    "AssemblyError",
    "AssemblyNode",
    "Band",
    "NozzleGeometry",
    "StageLayout",
    "VehicleAssembly",
    "build_assembly",
    "build_bulkhead_shell",
    "build_fin_solids",
    "build_nozzle_solids",
    "build_stage_solids",
    "derive_nozzle_geometry",
    "fairing_adapter_heights",
    "plan_stage",
    "section_node_name",
    "vehicle_core_height_m",
    "vehicle_node_index",
]
