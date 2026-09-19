"""界面可编辑的起始箭（规格 §1.7.3 OI-32 / §11.5 模板载入流的前置）。

⚠ 这**不是** §11.5 / OI-29 的内置示例火箭模板
------------------------------------------------
模板库（Falcon 9 / 长征五号 / 土星五号）归 **M3**，且 §11.5 规则 2 要求其参数
「必须来自公开资料并在 ``provenance`` 中标出来源」、§13.2 要求「模板与基准同源」。
本模块给出的是一份**显式标注为「未经来源核对」的示例骨架**，唯一目的是让参数面板
本轮就能真跑通「编辑 → 诊断 → 路径定位」这条链，**不得**被当成已核对的型号数据。

数值口径（§1.4-4 数值必须可溯源）
--------------------------------
- 能从规格内取到的**一律取自规格**，逐条记入 :data:`SOURCED_FIELDS`（含出处文字）；
- 取不到的只能给占位值，且**不进入** :data:`SOURCED_FIELDS`——界面据「是否在
  :data:`SOURCED_FIELDS` 里」把每个数分成「有出处」与「占位值」两类，
  **不允许**把占位值呈现为已验证结论。

骨架的取值刻意选在**全部硬约束与 §6.5 可判规则的合格区间内**（长径比、结构质量比、
壁厚、加注比例），这样界面一打开看到的是"能跑通的正常态"，而不是一屏拒绝。
留白同样刻意：``aero`` / ``fairing_diameter_m`` / ``sequence`` / ``recovery`` **不给**
——它们缺失时会如实产出 ``ENGINEER_AERO_DEFAULTED`` 之类的警告，正好让"未填写"
以警告的形式可见，而不是被悄悄补上一组看着合理的数。
"""

from __future__ import annotations

from aeroforge.params.schema import (
    Engine,
    Geometry,
    LaunchSite,
    Mission,
    Stage,
    Tank,
    Vehicle,
)

#: 骨架标识（界面标题栏与 provenance 用）。
TEMPLATE_ID = "skeleton-1stage-lox-rp1"

#: 界面显示名。名称里**必须**留下"未经核对"字样，避免被当成型号数据。
LABEL = "示例骨架 · 单级 LOX/RP-1（未经来源核对）"

#: 随响应下发的说明。界面须原样呈现（不得改写为更"肯定"的措辞）。
NOTE = (
    "这是一份未经来源核对的示例骨架，用于填写参数并试跑诊断；"
    "除 sourced_fields 列出的项之外，全部数值均为占位值，不构成任何设计结论（规格 §1.4-4）。"
    "完整的内置示例模板（公开型号，参数逐条核对并标注来源）按 OI-29 在 M3 交付。"
)

#: 字段路径 → 出处。路径口径与 §6.3 / §6.5 的 ``field_path`` **逐字一致**，
#: 故界面可以用同一张"路径 → 控件"映射把它们标到对应控件上。
SOURCED_FIELDS: dict[str, str] = {
    "stages[0].propellant": "规格 §3.2：CEA 黄金测试锚点组合 LOX / RP-1",
    "propellant": "规格 §3.2：CEA 黄金测试锚点组合 LOX / RP-1",
    "stages[0].engine.mixture_ratio": "规格 §3.2：LOX/RP-1 黄金锚点 O/F = 2.56",
    "stages[0].engine.chamber_pressure_pa": "规格 §3.2：LOX/RP-1 黄金锚点 Pc = 68.9 bar",
    "stages[0].engine.expansion_ratio": "规格 §3.2：黄金锚点工况 ε = 40",
    "stages[0].engine.isp_vacuum_s": "规格 §3.2：LOX/RP-1 黄金锚点真空比冲 356.6 s",
    "stages[0].engine.isp_sea_level_s": "规格 §3.2：LOX/RP-1 黄金锚点海平面比冲 339.1 s",
    "mission.orbit_type": "规格 §13.2：基准火箭的目标轨道列（LEO）",
}


def skeleton_vehicle() -> Vehicle:
    """构造示例骨架（每次调用返回新实例；模型本身 frozen，可安全共享）。"""
    # 黄金锚点取值（§3.2）：室压 68.9 bar、膨胀比 40、O/F 2.56、Isp 356.6 / 339.1 s
    chamber_pressure_pa = 68.9e5
    expansion_ratio = 40.0
    mixture_ratio = 2.56
    isp_vacuum_s = 356.6
    isp_sea_level_s = 339.1

    tank = Tank(
        tank_type="separate",
        wall_thickness_m=0.005,
        material="al-2219",
        fill_fraction=0.95,
        feed_system="pump_fed",
    )
    stage = Stage(
        index=1,
        propellant="LOX/RP-1",
        diameter_m=2.0,
        length_m=20.0,
        wall_thickness_m=0.005,
        material="al-2219",
        structure_coefficient=0.05,
        fill_fraction=0.95,
        engine_count=1,
        engine=Engine(
            model="示例发动机（占位型号）",
            propellant_phase="liquid",
            cycle="gas_generator",
            chamber_pressure_pa=chamber_pressure_pa,
            expansion_ratio=expansion_ratio,
            efficiency_factor=0.98,
            thrust_sea_level_n=700_000.0,
            thrust_vacuum_n=800_000.0,
            isp_sea_level_s=isp_sea_level_s,
            isp_vacuum_s=isp_vacuum_s,
            mixture_ratio=mixture_ratio,
        ),
        engine_height_m=2.0,
        # 级间段描述的是"该级与其**上级**之间的分离段"，最上级只能是 none（§5.9 共性 2）
        interstage_type="none",
        # 唯一权威（QA-1，v0.6.2）：default 语义下级层省略 isp_*，取发动机标称值
        isp_source="default",
        geometry=Geometry(oxidizer_tank=tank, fuel_tank=tank.model_copy()),
    )
    return Vehicle(
        name=LABEL,
        stages=(stage,),
        payload_mass_kg=1_000.0,
        material="al-2219",
        propellant="LOX/RP-1",
        # aero / fairing / profile / sequence / recovery 一律留空：缺失会如实变成警告
        mission=Mission(
            orbit_type="LEO",
            altitude_m=200_000.0,
            inclination_deg=28.5,
            launch_site=LaunchSite(
                name="示例发射场（占位）",
                latitude_deg=28.5,
                altitude_m=3.0,
                azimuth_deg=90.0,
            ),
        ),
    )
