"""内置火箭模板库（规格 §11.5 ⑤ 规则 2/5/6、OI-29 / OI-34；§13.2 同源）。

定位（与起始箭 :mod:`aeroforge.params.template` 的分工）
--------------------------------------------------------
起始箭是**未经来源核对**的示例骨架；本模块才是 §11.5 的「内置示例火箭模板」产品能力：
公开型号、参数逐条标注出处，并与 §13.2 基准表**同源**——同一份数据，两个用途
（回归基准 + 产品模板，FR-22 / OI-29）。当前覆盖 **Falcon 9（2 级）+ Saturn V（3 级）**：
两者纯串联即可表达；长征五号 / Falcon Heavy 带并联助推器，**须待 M4 Booster Schema
（OI-36）落地后补齐**——禁止「把助推器折算成串级」的假构型凑数（§11.5 ⑤ 规则 6）。

数值纪律（§1.4-4 溯源红线；§11.5 ⑤ 规则 2）
--------------------------------------------
- ``SOURCED_FIELDS`` 覆盖模板 Vehicle 的**每一个数值字段**（键 = §6.3 / §6.5 的
  ``field_path`` 口径）；已核对来源与「工程惯例估算」在文案里显式区分，估算值
  （壁厚、上面级海平面外推值等）**不得冒充已核对来源**；
- σ（``structure_coefficient``，存储权威）由公开干重 / 推进剂质量**反算**并注明算式；
- 上面级真空喷管的海平面推力 / 比冲是**非工作点**：填的是满足 Schema 必填的工程外推值，
  出处条目显式声明「不得用于性能判定」；
- 构造完成的 Vehicle **必须过产品自己的约束校验器**（与测试夹具同一调用方式）；
  警告级裁定（如未填 ``aero`` 的 ``ENGINEER_AERO_DEFAULTED``）按骨架同一哲学如实保留，
  不在模板里编造数值去消音。

匹配口径（OI-34）
------------------
名称经 :func:`normalize_name` 规范化（NFKC 全角→半角 + casefold + 去空白与连字符变体）
后与「模板名 + 别名」做**精确等值**；不做模糊 / 编辑距离匹配（宁漏勿错——误命中的代价
是整套错误参数）。匹配宇宙 = 本模块的模板库；GCAT 检索属「数据库浏览流」，不入此通路。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from aeroforge.errors import ParamsError
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.schema import (
    Engine,
    Geometry,
    LaunchSite,
    Mission,
    Stage,
    Tank,
    TankArrangement,
    TankType,
    Vehicle,
)

# ---------------------------------------------------------------------------
# 名称规范化（OI-34：等值匹配的唯一规范化点，独立成可测函数）
# ---------------------------------------------------------------------------

#: 连字符变体：ASCII 连字符、U+2010~U+2015 各式破折/连字符、下划线。
#: 两侧（用户输入与模板名/别名）走同一张表，删得多不会引入「误命中」。
_HYPHEN_VARIANTS = frozenset("-\u2010\u2011\u2012\u2013\u2014\u2015_")


def normalize_name(name: str) -> str:
    """名称规范化：NFKC（全角→半角）+ casefold + 去除空白与连字符变体。

    OI-34 的匹配口径要求「大小写 / 全半角 / 空白与连字符变体」不影响等值判定，
    而规范化之后的比较必须是**精确等值**——本函数之后不存在第二次口径调整。
    """
    nfkc = unicodedata.normalize("NFKC", name).casefold()
    return "".join(ch for ch in nfkc if not ch.isspace() and ch not in _HYPHEN_VARIANTS)


# ---------------------------------------------------------------------------
# 共享出处文案（同一口径的文字只写一遍，避免多份手写副本漂移）
# ---------------------------------------------------------------------------

_NOTE_INDEX = "级序号：自下而上编号（§6.1 约定）；结构元数据，非型号数据"
_NOTE_WALL = "工程惯例估算（公开资料无权威壁厚；待材料库与 M4 校核复核）"
_NOTE_TANK_WALL = "工程惯例估算（与该级壁厚同口径）"
_NOTE_FILL = "对照任务的标称加注状态：加注量即公开推进剂质量（工程惯例口径）"
_NOTE_TANK_FILL = "同该级加注比例（对照任务标称加注状态）"
_NOTE_EFFICIENCY = "1.0：公开比冲为手册/用户指南标称值，不再叠加折减（避免双计）"
_NOTE_SITE_ALT = "海岸发射场海拔数米（公开资料约 3–10 m；工程惯例取 3 m）"
_NOTE_AZIMUTH = "向东发射的标准方位角 90°（公开任务剖面；工程惯例取值）"


# ---------------------------------------------------------------------------
# 产品校验器门禁（§11.5 ⑤ 规则 2 的机器可查部分）
# ---------------------------------------------------------------------------


def _gate(vehicle: Vehicle) -> Vehicle:
    """模板门禁：构造完成的 Vehicle 必须过产品自己的约束校验器（无硬违反）。

    与测试夹具（``tests/conftest.py``）同一调用方式：``check_vehicle`` + 硬违反过滤。
    模板是内置资产，违约属**实现缺陷**而非用户输入问题，故抛 ``RuntimeError``
    而不是 :class:`ParamsError`。
    """
    hard = [item for item in check_vehicle(vehicle) if item.level == "hard"]
    if hard:
        first = hard[0]
        msg = (
            f"内置模板 {vehicle.name!r} 违反硬约束 {first.code}（{first.field_path}）："
            "模板数据属内置资产，违约是实现缺陷而非用户输入问题"
        )
        raise RuntimeError(msg)
    return vehicle


def _geometry(
    *,
    material: str,
    wall_thickness_m: float,
    fill_fraction: float,
    common_bulkhead: bool = False,
    insulation_m: float | None = None,
    tank_arrangement: TankArrangement = "oxidizer_upper",
) -> Geometry:
    """构造该级构型：两箱同壁厚、同加注比例，输送一律泵压式。

    共底级（氢氧级）两箱类型必须同为 ``common_bulkhead``（约束引擎判定一致性），
    且按 §5.9 口径 2③ 在 **LH₂ 侧（燃料箱）** 给隔热层；储箱排列是 §5.9 非铁律，
    逐级显式给出（S-IVB 氢在上，其余默认氧在上）。
    """
    tank_type: TankType = "common_bulkhead" if common_bulkhead else "separate"
    return Geometry(
        common_bulkhead=common_bulkhead,
        common_bulkhead_type="insulated_sandwich" if common_bulkhead else None,
        tank_arrangement=tank_arrangement,
        oxidizer_tank=Tank(
            tank_type=tank_type,
            wall_thickness_m=wall_thickness_m,
            material=material,
            fill_fraction=fill_fraction,
            feed_system="pump_fed",
        ),
        fuel_tank=Tank(
            tank_type=tank_type,
            wall_thickness_m=wall_thickness_m,
            material=material,
            fill_fraction=fill_fraction,
            feed_system="pump_fed",
            common_bulkhead_insulation_m=insulation_m,
        ),
    )


# ---------------------------------------------------------------------------
# Falcon 9（Block 5，2 级 LOX/RP-1）
# ---------------------------------------------------------------------------

# 公开分项质量（kg）。§13.2 标称 GLOW ≈ 549 t 对应典型任务剖面，与「分项之和 ≈ 571 t」
# 在公开来源中本就不闭合（干重含着陆支腿与栅格翼）——模板保存**分项**公开值，
# GLOW 同源门禁在测试层放宽（见 test_templates 的 §13.2 门禁；精确运力误差归 M4 回归）。
_F9_S1_DRY_KG = 25_600.0
_F9_S1_PROP_KG = 411_000.0
_F9_S2_DRY_KG = 4_000.0
_F9_S2_PROP_KG = 107_500.0


def _merlin_1d() -> Engine:
    """Merlin 1D（海平面型）：燃气发生器循环，用户指南口径的公开值。"""
    return Engine(
        model="Merlin 1D",
        cycle="gas_generator",
        chamber_pressure_pa=9.7e6,
        expansion_ratio=16.0,
        efficiency_factor=1.0,
        thrust_sea_level_n=845_000.0,
        thrust_vacuum_n=981_000.0,
        isp_sea_level_s=282.0,
        isp_vacuum_s=311.0,
        mixture_ratio=2.34,
    )


def _merlin_vac() -> Engine:
    """Merlin 1D Vacuum：大膨胀比真空喷管，海平面为非工作点（外推值见出处条目）。"""
    return Engine(
        model="Merlin 1D Vacuum",
        cycle="gas_generator",
        chamber_pressure_pa=9.7e6,
        expansion_ratio=165.0,
        efficiency_factor=1.0,
        # 海平面值按真空流量（ṁ ≈ 287 kg/s）与外推比冲 250 s 反推（≈ 705 kN）——
        # 仅满足 Schema 必填，SOURCED_FIELDS 已声明不得用于性能判定。
        thrust_sea_level_n=705_000.0,
        thrust_vacuum_n=981_000.0,
        isp_sea_level_s=250.0,
        isp_vacuum_s=348.0,
        mixture_ratio=2.34,
    )


def falcon9_vehicle() -> Vehicle:
    """构造 Falcon 9 模板（每次调用返回全新实例，且已过产品校验器门禁）。"""
    stage1 = Stage(
        index=1,
        propellant="LOX/RP-1",
        diameter_m=3.7,
        length_m=42.6,
        wall_thickness_m=0.006,
        material="al-li-2198",
        structure_coefficient=_F9_S1_DRY_KG / (_F9_S1_DRY_KG + _F9_S1_PROP_KG),
        fill_fraction=1.0,
        engine_count=9,
        engine=_merlin_1d(),
        engine_height_m=3.0,
        interstage_type="cold_staging",
        isp_source="default",
        geometry=_geometry(material="al-li-2198", wall_thickness_m=0.006, fill_fraction=1.0),
    )
    stage2 = Stage(
        index=2,
        propellant="LOX/RP-1",
        diameter_m=3.7,
        length_m=12.6,
        wall_thickness_m=0.004,
        material="al-li-2198",
        structure_coefficient=_F9_S2_DRY_KG / (_F9_S2_DRY_KG + _F9_S2_PROP_KG),
        fill_fraction=1.0,
        engine_count=1,
        engine=_merlin_vac(),
        engine_height_m=4.5,
        interstage_type="none",
        isp_source="default",
        geometry=_geometry(material="al-li-2198", wall_thickness_m=0.004, fill_fraction=1.0),
    )
    return _gate(
        Vehicle(
            name="Falcon 9",
            stages=(stage1, stage2),
            payload_mass_kg=22_800.0,
            fairing_diameter_m=5.2,
            material="al-li-2198",
            propellant="LOX/RP-1",
            mission=Mission(
                orbit_type="LEO",
                altitude_m=200_000.0,
                inclination_deg=28.5,
                launch_site=LaunchSite(
                    name="Cape Canaveral SLC-40",
                    latitude_deg=28.56,
                    altitude_m=3.0,
                    azimuth_deg=90.0,
                ),
            ),
        )
    )


FALCON9_NOTE = (
    "两级构型：一级 9× Merlin 1D，二级 1× Merlin Vacuum（LOX/RP-1）。"
    "来源：公开资料整理；关键参数与 §13.2 基准表同源；M4 基准回归将校验运力误差。"
    "数值仅在 sourced_fields 有出处标注时方可视为已核对，其余为工程惯例估算（§1.4-4）；"
    "载入后全部字段保持可编辑。"
)

FALCON9_SOURCED_FIELDS: dict[str, str] = {
    "payload_mass_kg": "公开 LEO 运力对照值（§13.2）：22.8 t（SpaceX 公开运力口径）",
    "fairing_diameter_m": "公开资料：标准整流罩直径 5.2 m",
    "mission.altitude_m": (
        "公开资料常用参考剖面：200 km 圆轨道（§13.2 未规定轨道要素；工程惯例取值）"
    ),
    "mission.inclination_deg": "卡纳维拉尔角向东发射的自然倾角 28.5°（公开资料常用值）",
    "mission.launch_site.latitude_deg": "公开资料：卡纳维拉尔角 SLC-40 北纬约 28.56°",
    "mission.launch_site.altitude_m": _NOTE_SITE_ALT,
    "mission.launch_site.azimuth_deg": _NOTE_AZIMUTH,
    # ---- 一级（9× Merlin 1D）----
    "stages[0].index": _NOTE_INDEX,
    "stages[0].diameter_m": "公开资料：芯级直径 3.7 m",
    "stages[0].length_m": "公开资料：一级长约 42.6 m（含级间段；整箭高约 70 m）",
    "stages[0].wall_thickness_m": _NOTE_WALL,
    "stages[0].structure_coefficient": (
        "由公开干重 25,600 kg（含着陆支腿与栅格翼）/ 推进剂 411,000 kg 反算："
        "σ = 25,600/436,600 ≈ 0.0586"
    ),
    "stages[0].fill_fraction": _NOTE_FILL,
    "stages[0].engine_count": "公开资料：一级 9 台 Merlin 1D",
    "stages[0].engine_height_m": "工程惯例估算（含喷管与安装高度；公开资料未统一）",
    "stages[0].engine.chamber_pressure_pa": "公开资料：Merlin 1D 室压 ≈ 9.7 MPa",
    "stages[0].engine.expansion_ratio": "公开资料：海平面型喷管面积比 16",
    "stages[0].engine.efficiency_factor": _NOTE_EFFICIENCY,
    "stages[0].engine.thrust_sea_level_n": "公开资料（用户指南口径）：单机海平面推力 ≈ 845 kN",
    "stages[0].engine.thrust_vacuum_n": "公开资料（用户指南口径）：单机真空推力 ≈ 981 kN",
    "stages[0].engine.isp_sea_level_s": "公开资料：海平面比冲 282 s",
    "stages[0].engine.isp_vacuum_s": "公开资料：真空比冲 311 s",
    "stages[0].engine.mixture_ratio": "公开资料（用户指南口径）：LOX/RP-1 混合比 2.34",
    "stages[0].geometry.oxidizer_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[0].geometry.oxidizer_tank.fill_fraction": _NOTE_TANK_FILL,
    "stages[0].geometry.fuel_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[0].geometry.fuel_tank.fill_fraction": _NOTE_TANK_FILL,
    # ---- 二级（1× Merlin Vacuum）----
    "stages[1].index": _NOTE_INDEX,
    "stages[1].diameter_m": "公开资料：二级直径 3.7 m（与芯级同径）",
    "stages[1].length_m": "公开资料（量级）：二级约 12.6 m",
    "stages[1].wall_thickness_m": _NOTE_WALL,
    "stages[1].structure_coefficient": (
        "由公开干重约 4,000 kg / 推进剂 107,500 kg 反算：σ = 4,000/111,500 ≈ 0.0359"
    ),
    "stages[1].fill_fraction": _NOTE_FILL,
    "stages[1].engine_count": "公开资料：二级 1 台 Merlin Vacuum",
    "stages[1].engine_height_m": "工程惯例估算（含大膨胀比喷管延伸段）",
    "stages[1].engine.chamber_pressure_pa": (
        "公开资料（量级）：与 Merlin 1D 同核心，室压 ≈ 9.7 MPa（用户指南未单列）"
    ),
    "stages[1].engine.expansion_ratio": "公开资料：Merlin Vacuum 面积比 165",
    "stages[1].engine.efficiency_factor": _NOTE_EFFICIENCY,
    "stages[1].engine.thrust_sea_level_n": (
        "工程惯例估算（真空喷管海平面为非工作点）：按真空流量 ≈ 287 kg/s 与外推比冲"
        " 250 s 反推 ≈ 705 kN；仅满足 Schema 必填，不得用于性能判定"
    ),
    "stages[1].engine.thrust_vacuum_n": "公开资料：真空推力 ≈ 981 kN",
    "stages[1].engine.isp_sea_level_s": (
        "工程惯例估算（真空喷管海平面为非工作点）：外推比冲 250 s，不得用于性能判定"
    ),
    "stages[1].engine.isp_vacuum_s": "公开资料：真空比冲 348 s",
    "stages[1].engine.mixture_ratio": "公开资料（用户指南口径）：LOX/RP-1 混合比 2.34",
    "stages[1].geometry.oxidizer_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[1].geometry.oxidizer_tank.fill_fraction": _NOTE_TANK_FILL,
    "stages[1].geometry.fuel_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[1].geometry.fuel_tank.fill_fraction": _NOTE_TANK_FILL,
}


# ---------------------------------------------------------------------------
# Saturn V（Apollo 配置，3 级：LOX/RP-1 + LOX/LH2）
# ---------------------------------------------------------------------------

# 公开分项质量（kg）：S-IC 130,400 + 2,149,500；S-II 36,400 + 443,000；S-IVB 13,500 +
# 106,600。分项合计 ≈ 3,019 t，与 §13.2 标称 2,970 t 差 ~1.7%（资料级差异，口径同上）。
_SV_SIC_DRY_KG = 130_400.0
_SV_SIC_PROP_KG = 2_149_500.0
_SV_SII_DRY_KG = 36_400.0
_SV_SII_PROP_KG = 443_000.0
_SV_SIVB_DRY_KG = 13_500.0
_SV_SIVB_PROP_KG = 106_600.0


def _f1() -> Engine:
    """F-1：单台海平面推力最大的燃气发生器循环发动机（公开手册口径）。"""
    return Engine(
        model="F-1",
        cycle="gas_generator",
        chamber_pressure_pa=7.0e6,
        expansion_ratio=16.0,
        efficiency_factor=1.0,
        thrust_sea_level_n=6_770_000.0,
        thrust_vacuum_n=7_770_000.0,
        isp_sea_level_s=263.0,
        isp_vacuum_s=304.0,
        mixture_ratio=2.27,
    )


def _j2() -> Engine:
    """J-2：氢氧燃气发生器循环高空发动机，S-II 与 S-IVB 同型（可再启动）。"""
    return Engine(
        model="J-2",
        cycle="gas_generator",
        chamber_pressure_pa=3.0e6,
        expansion_ratio=27.5,
        efficiency_factor=1.0,
        # 海平面值按真空流量（ṁ ≈ 250 kg/s）与公开海平面比冲 200 s 反推（≈ 490 kN）——
        # 仅满足 Schema 必填，SOURCED_FIELDS 已声明不得用于性能判定。
        thrust_sea_level_n=490_000.0,
        thrust_vacuum_n=1_033_000.0,
        isp_sea_level_s=200.0,
        isp_vacuum_s=421.0,
        mixture_ratio=5.0,
    )


def saturnv_vehicle() -> Vehicle:
    """构造 Saturn V 模板（每次调用返回全新实例，且已过产品校验器门禁）。

    S-II 与 S-IVB 为氢氧级：按公开事实建模**共底**（§5.9 口径 2③ 要求 LH₂ 侧加隔热层）；
    储箱排列按 §5.9 非铁律逐级给出（S-IC / S-II 氧在上，S-IVB 氢在上）。
    """
    stage1 = Stage(
        index=1,
        propellant="LOX/RP-1",
        diameter_m=10.1,
        length_m=42.1,
        wall_thickness_m=0.012,
        material="al-2219",
        structure_coefficient=_SV_SIC_DRY_KG / (_SV_SIC_DRY_KG + _SV_SIC_PROP_KG),
        fill_fraction=1.0,
        engine_count=5,
        engine=_f1(),
        engine_height_m=5.6,
        interstage_type="cold_staging",
        isp_source="default",
        geometry=_geometry(material="al-2219", wall_thickness_m=0.012, fill_fraction=1.0),
    )
    stage2 = Stage(
        index=2,
        propellant="LOX/LH2",
        diameter_m=10.1,
        length_m=24.9,
        wall_thickness_m=0.010,
        material="al-2219",
        structure_coefficient=_SV_SII_DRY_KG / (_SV_SII_DRY_KG + _SV_SII_PROP_KG),
        fill_fraction=1.0,
        engine_count=5,
        engine=_j2(),
        engine_height_m=3.4,
        interstage_type="cold_staging",
        isp_source="default",
        geometry=_geometry(
            material="al-2219",
            wall_thickness_m=0.010,
            fill_fraction=1.0,
            common_bulkhead=True,
            insulation_m=0.05,
        ),
    )
    stage3 = Stage(
        index=3,
        propellant="LOX/LH2",
        diameter_m=6.6,
        length_m=17.8,
        wall_thickness_m=0.006,
        material="al-2219",
        structure_coefficient=_SV_SIVB_DRY_KG / (_SV_SIVB_DRY_KG + _SV_SIVB_PROP_KG),
        fill_fraction=1.0,
        engine_count=1,
        engine=_j2(),
        engine_height_m=3.4,
        interstage_type="none",
        isp_source="default",
        geometry=_geometry(
            material="al-2219",
            wall_thickness_m=0.006,
            fill_fraction=1.0,
            common_bulkhead=True,
            insulation_m=0.05,
            tank_arrangement="fuel_upper",
        ),
    )
    return _gate(
        Vehicle(
            name="Saturn V",
            stages=(stage1, stage2, stage3),
            payload_mass_kg=140_000.0,
            fairing_diameter_m=6.6,
            material="al-2219",
            propellant="LOX/RP-1",
            mission=Mission(
                orbit_type="LEO",
                altitude_m=185_000.0,
                inclination_deg=32.5,
                launch_site=LaunchSite(
                    name="Kennedy Space Center LC-39A",
                    latitude_deg=28.6,
                    altitude_m=3.0,
                    azimuth_deg=90.0,
                ),
            ),
        )
    )


SATURNV_NOTE = (
    "三级构型：S-IC 5× F-1（LOX/RP-1），S-II 5× J-2 与 S-IVB 1× J-2（LOX/LH2，共底）。"
    "来源：公开资料整理；关键参数与 §13.2 基准表同源；M4 基准回归将校验运力误差。"
    "公开运力取 LEO 设计值 140 t 档（阿波罗任务实际约 118 t，取值口径见 §13.2）；"
    "数值仅在 sourced_fields 有出处标注时方可视为已核对（§1.4-4）；载入后全部字段保持可编辑。"
)

SATURNV_SOURCED_FIELDS: dict[str, str] = {
    "payload_mass_kg": (
        "公开 LEO 运力对照值（§13.2）：140 t（LEO 设计档；阿波罗任务实际约 118 t）"
    ),
    "fairing_diameter_m": (
        "公开资料：飞船-登月舱适配器（SLA）最大直径 6.6 m（真实构型即小于最大级直径）"
    ),
    "mission.altitude_m": "公开资料：阿波罗任务停泊轨道约 185 km（工程惯例取值）",
    "mission.inclination_deg": "公开资料：肯尼迪航天中心向东发射的常用倾角 32.5°",
    "mission.launch_site.latitude_deg": "公开资料：肯尼迪航天中心 LC-39A 北纬约 28.6°",
    "mission.launch_site.altitude_m": _NOTE_SITE_ALT,
    "mission.launch_site.azimuth_deg": _NOTE_AZIMUTH,
    # ---- S-IC（5× F-1）----
    "stages[0].index": _NOTE_INDEX,
    "stages[0].diameter_m": "公开资料：S-IC 直径 10.1 m",
    "stages[0].length_m": "公开资料：S-IC 级长约 42.1 m",
    "stages[0].wall_thickness_m": _NOTE_WALL,
    "stages[0].structure_coefficient": (
        "由公开干重 130,400 kg / 推进剂 2,149,500 kg 反算：σ = 130,400/2,279,900 ≈ 0.0572"
    ),
    "stages[0].fill_fraction": _NOTE_FILL,
    "stages[0].engine_count": "公开资料：S-IC 装 5 台 F-1",
    "stages[0].engine_height_m": "公开资料（量级）：F-1 含喷管高约 5.6 m",
    "stages[0].engine.chamber_pressure_pa": "公开资料：F-1 室压 ≈ 7 MPa",
    "stages[0].engine.expansion_ratio": "公开资料：海平面型喷管面积比 16",
    "stages[0].engine.efficiency_factor": _NOTE_EFFICIENCY,
    "stages[0].engine.thrust_sea_level_n": "公开资料：单机海平面推力 ≈ 6,770 kN",
    "stages[0].engine.thrust_vacuum_n": "公开资料：单机真空推力 ≈ 7,770 kN",
    "stages[0].engine.isp_sea_level_s": "公开资料：海平面比冲 263 s",
    "stages[0].engine.isp_vacuum_s": "公开资料：真空比冲 304 s",
    "stages[0].engine.mixture_ratio": "公开资料：F-1 混合比 ≈ 2.27",
    "stages[0].geometry.oxidizer_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[0].geometry.oxidizer_tank.fill_fraction": _NOTE_TANK_FILL,
    "stages[0].geometry.fuel_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[0].geometry.fuel_tank.fill_fraction": _NOTE_TANK_FILL,
    # ---- S-II（5× J-2，共底）----
    "stages[1].index": _NOTE_INDEX,
    "stages[1].diameter_m": "公开资料：S-II 直径 10.1 m",
    "stages[1].length_m": "公开资料：S-II 级长约 24.9 m",
    "stages[1].wall_thickness_m": _NOTE_WALL,
    "stages[1].structure_coefficient": (
        "由公开干重 36,400 kg / 推进剂 443,000 kg 反算：σ = 36,400/479,400 ≈ 0.0759"
    ),
    "stages[1].fill_fraction": _NOTE_FILL,
    "stages[1].engine_count": "公开资料：S-II 装 5 台 J-2",
    "stages[1].engine_height_m": "公开资料（量级）：J-2 含喷管高约 3.4 m",
    "stages[1].engine.chamber_pressure_pa": "公开资料：J-2 室压 ≈ 3 MPa",
    "stages[1].engine.expansion_ratio": "公开资料：J-2 面积比 27.5",
    "stages[1].engine.efficiency_factor": _NOTE_EFFICIENCY,
    "stages[1].engine.thrust_sea_level_n": (
        "工程惯例估算（高空型喷管海平面外推）：按真空流量 ≈ 250 kg/s 与海平面比冲 200 s"
        " 反推 ≈ 490 kN；仅满足 Schema 必填，不得用于性能判定"
    ),
    "stages[1].engine.thrust_vacuum_n": "公开资料：单机真空推力 ≈ 1,033 kN",
    "stages[1].engine.isp_sea_level_s": (
        "公开资料（口径之一）：海平面比冲 200 s（高空喷管海平面为非工作点）"
    ),
    "stages[1].engine.isp_vacuum_s": "公开资料：真空比冲 421 s",
    "stages[1].engine.mixture_ratio": "公开资料：J-2 混合比 5.0",
    "stages[1].geometry.oxidizer_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[1].geometry.oxidizer_tank.fill_fraction": _NOTE_TANK_FILL,
    "stages[1].geometry.fuel_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[1].geometry.fuel_tank.fill_fraction": _NOTE_TANK_FILL,
    "stages[1].geometry.fuel_tank.common_bulkhead_insulation_m": (
        "工程惯例估算（S-II 共底设计为公开事实；隔热层厚度未获权威数值）"
    ),
    # ---- S-IVB（1× J-2，共底）----
    "stages[2].index": _NOTE_INDEX,
    "stages[2].diameter_m": "公开资料：S-IVB 直径 6.6 m",
    "stages[2].length_m": "公开资料：S-IVB 级长约 17.8 m",
    "stages[2].wall_thickness_m": _NOTE_WALL,
    "stages[2].structure_coefficient": (
        "由公开干重 13,500 kg / 推进剂 106,600 kg 反算：σ = 13,500/120,100 ≈ 0.1124"
    ),
    "stages[2].fill_fraction": _NOTE_FILL,
    "stages[2].engine_count": "公开资料：S-IVB 装 1 台 J-2",
    "stages[2].engine_height_m": "公开资料（量级）：J-2 含喷管高约 3.4 m",
    "stages[2].engine.chamber_pressure_pa": "公开资料：J-2 室压 ≈ 3 MPa",
    "stages[2].engine.expansion_ratio": "公开资料：J-2 面积比 27.5",
    "stages[2].engine.efficiency_factor": _NOTE_EFFICIENCY,
    "stages[2].engine.thrust_sea_level_n": (
        "工程惯例估算（高空型喷管海平面外推）：按真空流量 ≈ 250 kg/s 与海平面比冲 200 s"
        " 反推 ≈ 490 kN；仅满足 Schema 必填，不得用于性能判定"
    ),
    "stages[2].engine.thrust_vacuum_n": "公开资料：单机真空推力 ≈ 1,033 kN",
    "stages[2].engine.isp_sea_level_s": (
        "公开资料（口径之一）：海平面比冲 200 s（高空喷管海平面为非工作点）"
    ),
    "stages[2].engine.isp_vacuum_s": "公开资料：真空比冲 421 s",
    "stages[2].engine.mixture_ratio": "公开资料：J-2 混合比 5.0",
    "stages[2].geometry.oxidizer_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[2].geometry.oxidizer_tank.fill_fraction": _NOTE_TANK_FILL,
    "stages[2].geometry.fuel_tank.wall_thickness_m": _NOTE_TANK_WALL,
    "stages[2].geometry.fuel_tank.fill_fraction": _NOTE_TANK_FILL,
    "stages[2].geometry.fuel_tank.common_bulkhead_insulation_m": (
        "工程惯例估算（S-IVB 共底设计为公开事实；隔热层厚度未获权威数值）"
    ),
}


# ---------------------------------------------------------------------------
# 注册表（模板元数据 + 工厂 + 出处表）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RocketTemplate:
    """一个内置模板：元数据 + 出处表 + Vehicle 工厂。

    ``stage_propellant_mass_kg`` **不是** Vehicle 字段（Schema 里没有它的位置——
    §6.2 由 M4 定尺求解给出）：这里随模板保存公开加注量，供 §13.2 基准回归
    （GLOW 同源门禁）与 M4 运力回归共用同一份分项数据。
    """

    id: str
    name: str
    aliases: tuple[str, ...]
    stage_count: int
    note: str
    reference_payload_leo_kg: float
    sourced_fields: dict[str, str]
    stage_propellant_mass_kg: tuple[float, ...]
    build_vehicle: Callable[[], Vehicle]

    @property
    def match_keys(self) -> frozenset[str]:
        """规范化后的匹配键（OI-34：模板名 + 别名；id 规范化后与名同形，无需单列）。"""
        return frozenset(
            key
            for key in (normalize_name(self.name), *(normalize_name(a) for a in self.aliases))
            if key
        )


_TEMPLATES: tuple[RocketTemplate, ...] = (
    RocketTemplate(
        id="falcon-9",
        name="Falcon 9",
        aliases=("Falcon9", "F9", "猎鹰9", "猎鹰九号"),
        stage_count=2,
        note=FALCON9_NOTE,
        reference_payload_leo_kg=22_800.0,
        sourced_fields=FALCON9_SOURCED_FIELDS,
        stage_propellant_mass_kg=(_F9_S1_PROP_KG, _F9_S2_PROP_KG),
        build_vehicle=falcon9_vehicle,
    ),
    RocketTemplate(
        id="saturn-v",
        name="Saturn V",
        aliases=("Saturn-V", "SaturnV", "土星五号", "土星5号"),
        stage_count=3,
        note=SATURNV_NOTE,
        reference_payload_leo_kg=140_000.0,
        sourced_fields=SATURNV_SOURCED_FIELDS,
        stage_propellant_mass_kg=(_SV_SIC_PROP_KG, _SV_SII_PROP_KG, _SV_SIVB_PROP_KG),
        build_vehicle=saturnv_vehicle,
    ),
)


def get_template_list() -> tuple[RocketTemplate, ...]:
    """全部内置模板（清单用；完整参数按 id 取 :func:`get_template`）。"""
    return _TEMPLATES


def get_template(template_id: str) -> RocketTemplate:
    """按 id 取模板；未知 id 抛 :class:`ParamsError`（API 层映射为 422）。"""
    for record in _TEMPLATES:
        if record.id == template_id:
            return record
    known = "、".join(record.id for record in _TEMPLATES)
    raise ParamsError(
        f"未知模板 id：{template_id!r}",
        suggestion=f"先调 GET /api/templates 获取清单；当前可用模板：{known}",
    )


def match_template(name: str) -> RocketTemplate | None:
    """OI-34 名称匹配：规范化后与「模板名 + 别名」做**精确等值**。

    不做模糊 / 编辑距离匹配（宁漏勿错——误命中的代价是整套错误参数）。
    空串 / 纯空白返回 ``None`` 而**不报错**：名称栏防抖会在输入过程中频繁发出空查询。
    """
    key = normalize_name(name)
    if not key:
        return None
    for record in _TEMPLATES:
        if key in record.match_keys:
            return record
    return None
