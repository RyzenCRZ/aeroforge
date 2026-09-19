"""约束与校验（规格 §6.3）。

范围
----
本模块只放 **M2 就能求值** 的约束——判据全部来自 Schema 本身的字段，不需要 M4 的
Δv / 运力 / 喷管型面结果。需要那些结果的判定（例如 §6.3 举例的"喷管出口直径 < 级直径"）
在 :mod:`aeroforge.params.diagnostics` 的规则集里登记为**显式 deferred**，
而不是从这里悄悄缺席——"未验证"必须与"已验证"同等可见。

四类约束（§6.3）直接体现在判定码前缀上，便于前端分组渲染与机检：
``HARD_`` 硬约束（拒绝）· ``ENGINEER_`` 工程约束（警告）·
``COMPAT_`` 相容约束（警告）· ``SAFETY_`` 安全边界（警告）。

⚠ 字段路径一律用**数组下标**（``stages[0]``）而不是级号：它与 pydantic 的 ``loc``
以及 :func:`aeroforge.params.schema.to_diagnostics` 产出的路径同构，前端才能用同一张
"路径 → 控件"映射把两类错误渲染到同一处。级号写进 ``message`` 供人阅读。
"""

from __future__ import annotations

from aeroforge.params.propellants import fuel_is_lh2
from aeroforge.params.report import Diagnostic
from aeroforge.params.schema import Stage, Vehicle

#: 需要高度才成立的目标轨道（§8.6 的轨道需求表）。
_ALTITUDE_ORBITS = frozenset({"LEO", "SSO", "GEO"})
#: 用近/远地点描述的目标轨道。
_APSIS_ORBITS = frozenset({"GTO"})

#: 比冲来源标为 default 时，级值与发动机标称值的允许偏差（相对）。超出即视为
#: "改了值却没标 custom"——这是 UI 最容易漏的一步，故必须提示。
_ISP_CONSISTENCY_TOL = 5e-3


def _hard(code: str, field_path: str, message: str, suggestion: str) -> Diagnostic:
    return Diagnostic(
        level="hard", code=code, field_path=field_path, message=message, suggestion=suggestion
    )


def _warn(code: str, field_path: str, message: str, suggestion: str) -> Diagnostic:
    return Diagnostic(
        level="warning", code=code, field_path=field_path, message=message, suggestion=suggestion
    )


def _check_stage_index(vehicle: Vehicle) -> list[Diagnostic]:
    """级序号必须是 1..N 且不重复——否则"第 i 级"在 UI、装配树与质量预算里各指一处。"""
    indices = [stage.index for stage in vehicle.stages]
    expected = list(range(1, len(indices) + 1))
    if sorted(indices) == expected:
        return []
    return [
        _hard(
            "HARD_STAGE_INDEX",
            "stages",
            f"级序号必须自 1 起连续且不重复；实际为 {indices}",
            f"把它改成 {expected}（index 1 = 第一级，自下而上）",
        )
    ]


def _check_fill_fraction(
    stage: Stage, position: int, field_path: str, fill: float
) -> list[Diagnostic]:
    """加注比例上限 1.0（OI-03）：超过必须显式启用「最大允许加注量」，不得静默放行。"""
    if fill <= 1.0:
        return []
    if stage.max_fill_mass_kg is not None:
        return []
    return [
        _hard(
            "HARD_FILL_OVERFILL",
            field_path,
            f"加注比例 {fill} > 1.0（第 {stage.index} 级），且未给出「最大允许加注量」",
            "把加注比例降到 ≤ 1.0；若确属超装（贮箱留有膨胀空间）"
            "请显式填写 max_fill_mass_kg 以声明该构型（OI-03）",
        )
    ]


def _check_stage(stage: Stage, position: int) -> list[Diagnostic]:
    prefix = f"stages[{position}]"
    items: list[Diagnostic] = []

    items += _check_fill_fraction(stage, position, f"{prefix}.fill_fraction", stage.fill_fraction)
    items += _check_fill_fraction(
        stage,
        position,
        f"{prefix}.geometry.oxidizer_tank.fill_fraction",
        stage.geometry.oxidizer_tank.fill_fraction,
    )
    items += _check_fill_fraction(
        stage,
        position,
        f"{prefix}.geometry.fuel_tank.fill_fraction",
        stage.geometry.fuel_tank.fill_fraction,
    )

    geometry = stage.geometry
    if geometry.common_bulkhead and geometry.common_bulkhead_type is None:
        items.append(
            _hard(
                "HARD_BULKHEAD_TYPE_MISSING",
                f"{prefix}.geometry.common_bulkhead_type",
                f"第 {stage.index} 级启用了共底，但未给出共底类型",
                "填 insulated_sandwich（隔热夹层，§5.9 共性 3 的常见做法）或 plain",
            )
        )
    if not geometry.common_bulkhead and geometry.common_bulkhead_type is not None:
        items.append(
            _warn(
                "ENGINEER_BULKHEAD_TYPE_UNUSED",
                f"{prefix}.geometry.common_bulkhead_type",
                f"第 {stage.index} 级未启用共底，共底类型不会生效",
                "忽略此项，或打开 common_bulkhead 使该字段生效",
            )
        )

    for role, tank, tank_path in (
        ("氧化剂箱", geometry.oxidizer_tank, f"{prefix}.geometry.oxidizer_tank"),
        ("燃料箱", geometry.fuel_tank, f"{prefix}.geometry.fuel_tank"),
    ):
        expected: str = "common_bulkhead" if geometry.common_bulkhead else "separate"
        if tank.tank_type != expected:
            items.append(
                _hard(
                    "HARD_TANK_TYPE_MISMATCH",
                    f"{tank_path}.tank_type",
                    f"第 {stage.index} 级 {role} 的类型 {tank.tank_type!r} 与共底开关"
                    f"（common_bulkhead={geometry.common_bulkhead}）不一致",
                    f"二者必须一致：把 tank_type 改为 {expected!r}，或调整共底开关",
                )
            )
        if tank.diameter_m is not None and tank.diameter_m > stage.diameter_m:
            items.append(
                _hard(
                    "COMPAT_TANK_DIAMETER_EXCEEDS_STAGE",
                    f"{tank_path}.diameter_m",
                    f"第 {stage.index} 级 {role} 直径 {tank.diameter_m} m 大于级直径 "
                    f"{stage.diameter_m} m",
                    "把贮箱直径降到 ≤ 级直径（省略该字段即继承级直径）",
                )
            )
        # 安全边界（§6.3）：壁厚必须小于半径，否则"壳"比"腔"还厚，几何无意义
        if tank.wall_thickness_m >= stage.diameter_m / 2.0:
            items.append(
                _hard(
                    "SAFETY_TANK_WALL_TOO_THICK",
                    f"{tank_path}.wall_thickness_m",
                    f"第 {stage.index} 级 {role} 壁厚 {tank.wall_thickness_m} m ≥ 半径 "
                    f"{stage.diameter_m / 2.0} m",
                    "壁厚应远小于半径；请核对单位是否误填（内部一律 SI，米）",
                )
            )

    if stage.wall_thickness_m >= stage.diameter_m / 2.0:
        items.append(
            _hard(
                "SAFETY_STAGE_WALL_TOO_THICK",
                f"{prefix}.wall_thickness_m",
                f"第 {stage.index} 级壁厚 {stage.wall_thickness_m} m ≥ "
                f"半径 {stage.diameter_m / 2.0} m",
                "壁厚应远小于半径；请核对单位是否误填（内部一律 SI，米）",
            )
        )

    # 相容约束（§6.3）：发动机必须装得进这一级
    if stage.length_m < stage.engine_height_m:
        items.append(
            _hard(
                "COMPAT_ENGINE_EXCEEDS_STAGE",
                f"{prefix}.length_m",
                f"第 {stage.index} 级高度 {stage.length_m} m 小于发动机高度 "
                f"{stage.engine_height_m} m（含喷管）",
                "加长该级或换更短的发动机（发动机高度口径：安装基准面 → 喷管出口端面）",
            )
        )

    # §5.9 口径 2③：共底且 LH₂ 侧**强制**加隔热层——液氢沸点 20 K，
    # 共享隔板不隔温会把另一侧推进剂冻住（这不是"可选优化"）
    if (
        geometry.common_bulkhead
        and fuel_is_lh2(stage.propellant)
        and geometry.fuel_tank.common_bulkhead_insulation_m is None
    ):
        items.append(
            _hard(
                "HARD_BULKHEAD_INSULATION_MISSING",
                f"{prefix}.geometry.fuel_tank.common_bulkhead_insulation_m",
                f"第 {stage.index} 级共底且燃料为液氢，但未给出共底隔热层厚度",
                "填写隔热层厚度（m）；液氢侧共底必须隔温（§5.9 四项建模口径 2③）",
            )
        )

    engine = stage.engine
    if stage.isp_source == "default":
        for label, stage_value, engine_value, path in (
            ("真空比冲", stage.isp_vacuum_s, engine.isp_vacuum_s, f"{prefix}.isp_vacuum_s"),
            (
                "海平面比冲",
                stage.isp_sea_level_s,
                engine.isp_sea_level_s,
                f"{prefix}.isp_sea_level_s",
            ),
        ):
            if abs(stage_value - engine_value) > _ISP_CONSISTENCY_TOL * engine_value:
                items.append(
                    _warn(
                        "ENGINEER_ISP_SOURCE_MISMATCH",
                        path,
                        f"第 {stage.index} 级{label} {stage_value} s 与发动机标称值 "
                        f"{engine_value} s 不一致，但来源标为 default",
                        "若确实要覆写发动机标称值，请把 isp_source 改为 custom",
                    )
                )

    # 相容约束（§1.7.3 OI-30）：固体级的液体构型字段语义不成立。
    # 判**警告**而非硬约束：Schema 是多级共用的一份结构，固体助推级与液体芯级并存时
    # 不得因助推级是固体就判整箭非法；但也**不得**默认沉默——沉默等于让用户以为
    # 那些字段对固体级也生效（"没报错 ≠ 正确"）。
    if engine.propellant_phase == "solid":
        items.append(
            _warn(
                "COMPAT_SOLID_PHASE_LIQUID_FIELDS",
                f"{prefix}.engine.propellant_phase",
                f"第 {stage.index} 级相态为固体，但同级仍带着共底 / 储箱排列 / 混合比 O/F / "
                "加注比例——固体药柱无贮箱、无 O/F，这些字段对该级不生效",
                "若该级确为固体（药柱 + 壳体），忽略本提示即可；保留这些字段是为多级火箭"
                "共用同一 Schema（液体级仍需它们）",
            )
        )

    return items


def _check_mission(vehicle: Vehicle) -> list[Diagnostic]:
    mission = vehicle.mission
    items: list[Diagnostic] = []

    if mission.launch_site is None and mission.launch_site_id is None:
        items.append(
            _hard(
                "HARD_LAUNCH_SITE_MISSING",
                "mission",
                "既未内联发射场，也未给出发射场引用——纬度是自转加成与转向损失的唯一输入",
                "填写 mission.launch_site（或给出 launch_site_id）：缺少纬度时运力无法计算（§8.6）",
            )
        )
    elif mission.launch_site is not None and mission.launch_site_id is not None:
        items.append(
            _warn(
                "COMPAT_LAUNCH_SITE_AMBIGUOUS",
                "mission.launch_site_id",
                "同时给出了内联发射场与发射场引用，二者可能不一致",
                "只保留一个：本次计算以内联的 mission.launch_site 为准",
            )
        )

    if mission.orbit_type in _ALTITUDE_ORBITS and mission.altitude_m is None:
        items.append(
            _hard(
                "HARD_ORBIT_ALTITUDE_MISSING",
                "mission.altitude_m",
                f"目标轨道 {mission.orbit_type} 需要轨道高度，但未给出",
                "填写 altitude_m（m）；该值同时决定轨道速度与 Δv 需求（§8.6）",
            )
        )
    if mission.orbit_type in _APSIS_ORBITS and (
        mission.perigee_altitude_m is None or mission.apogee_altitude_m is None
    ):
        items.append(
            _hard(
                "HARD_ORBIT_APSIS_MISSING",
                "mission.perigee_altitude_m",
                f"目标轨道 {mission.orbit_type} 需要用近地点与远地点描述，但至少缺一项",
                "同时填写 perigee_altitude_m 与 apogee_altitude_m（m）",
            )
        )
    if (
        mission.perigee_altitude_m is not None
        and mission.apogee_altitude_m is not None
        and mission.perigee_altitude_m > mission.apogee_altitude_m
    ):
        items.append(
            _hard(
                "HARD_ORBIT_APSIS_ORDER",
                "mission.perigee_altitude_m",
                f"近地点高度 {mission.perigee_altitude_m} m 大于远地点高度 "
                f"{mission.apogee_altitude_m} m",
                "交换两值——近地点必须不高于远地点",
            )
        )

    if vehicle.fairing_diameter_m is not None:
        max_stage_diameter = max(stage.diameter_m for stage in vehicle.stages)
        if vehicle.fairing_diameter_m < max_stage_diameter:
            items.append(
                _warn(
                    "COMPAT_FAIRING_SMALLER_THAN_STAGE",
                    "fairing_diameter_m",
                    f"整流罩直径 {vehicle.fairing_diameter_m} m 小于最大级直径 "
                    f"{max_stage_diameter} m",
                    "整流罩需罩住载荷与上面级，通常不小于最大级直径；请确认该值",
                )
            )

    if vehicle.aero is None:
        items.append(
            _warn(
                "ENGINEER_AERO_DEFAULTED",
                "aero",
                "未提供气动参数（Cd / 参考面积），将按默认值处理",
                "如需得到可溯源的气动阻力损失，请显式填写 aero（二者均为工程惯例值，非权威来源）",
            )
        )

    return items


def _check_recovery(vehicle: Vehicle) -> list[Diagnostic]:
    recovery = vehicle.recovery
    if recovery is None or not recovery.enabled:
        return []
    items: list[Diagnostic] = []
    known = {stage.index for stage in vehicle.stages}
    unknown = [index for index in recovery.stage_indices if index not in known]
    if unknown:
        items.append(
            _hard(
                "HARD_RECOVERY_STAGE_UNKNOWN",
                "recovery.stage_indices",
                f"回收级号 {unknown} 在该飞行器中不存在（现有级号：{sorted(known)}）",
                "改为该飞行器实际存在的级号",
            )
        )
    if not recovery.stage_indices:
        items.append(
            _hard(
                "HARD_RECOVERY_STAGE_MISSING",
                "recovery.stage_indices",
                "启用了回收，但未指定回收哪一级",
                "填写 stage_indices（例如 [1] 表示回收第一级）",
            )
        )
    if recovery.method is None:
        items.append(
            _hard(
                "HARD_RECOVERY_METHOD_MISSING",
                "recovery.method",
                "启用了回收，但未给出回收方式",
                "填 parachute（伞降）或 propulsive（动力反推着陆）",
            )
        )
    return items


def check_vehicle(vehicle: Vehicle) -> list[Diagnostic]:
    """跑完 §6.3 的四类约束，返回字段级裁定（可能为空列表）。"""
    items: list[Diagnostic] = []
    items += _check_stage_index(vehicle)
    for position, stage in enumerate(vehicle.stages):
        items += _check_stage(stage, position)
    items += _check_mission(vehicle)
    items += _check_recovery(vehicle)
    return items
