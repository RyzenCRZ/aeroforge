"""§6.3 四类约束（硬约束 / 工程约束 / 相容约束 / 安全边界）。

M2 验收项「非法参数被拒并给字段级建议」的**领域侧**：结构错由
:mod:`aeroforge.params.schema` 拦（见 ``test_params_schema.py``），
本文件管"结构合法但工程上不成立"的那些。

判定码按类别带前缀（``HARD_`` / ``ENGINEER_`` / ``COMPAT_`` / ``SAFETY_``），
故这里逐类各取代表用例，并额外钉一条：**字段路径一律用数组下标**——
它与 pydantic 的 ``loc`` 同构，前端才能用同一张"路径 → 控件"映射渲染两类错误。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import Diagnostic, has_hard
from aeroforge.params.schema import Vehicle

#: 字段路径的合法形态（``stages[0].geometry.fuel_tank.wall_thickness_m`` / ``mission``）。
_PATH_RE = re.compile(r"^[a-z_]+(\[\d+\])?(\.[a-z_]+)*$")


def _mutate(vehicle: Vehicle, mutate: Callable[[dict[str, Any]], None]) -> Vehicle:
    """把夹具转成原始 dict、改一处、再校验回来——"负例只改一处"的标准写法。"""
    payload = vehicle.model_dump(mode="json")
    mutate(payload)
    return Vehicle.model_validate(payload)


def _diagnostics(vehicle: Vehicle) -> list[Diagnostic]:
    return check_vehicle(vehicle)


def _codes(vehicle: Vehicle) -> list[str]:
    return [item.code for item in _diagnostics(vehicle)]


def _by_code(vehicle: Vehicle, code: str) -> list[Diagnostic]:
    return [item for item in _diagnostics(vehicle) if item.code == code]


def test_positive_fixture_is_completely_clean(single_stage_vehicle: Vehicle) -> None:
    """正例夹具必须**一条裁定都不出**：它的默认值落在全部工程判据的合格区间内。

    这条比"没有硬违反"更严——若夹具默认值漂到警告区间（例如长径比改到 40），
    后续"只应该报这一条"的负例就会多出一条噪声，而人往往先怀疑产品代码。
    """
    assert _diagnostics(single_stage_vehicle) == []


def test_stage_index_must_be_continuous(single_stage_vehicle: Vehicle) -> None:
    vehicle = _mutate(
        single_stage_vehicle, lambda payload: payload["stages"][0].update({"index": 2})
    )

    items = _by_code(vehicle, "HARD_STAGE_INDEX")
    assert len(items) == 1
    assert items[0].field_path == "stages"
    assert has_hard(items)


def test_overfill_requires_the_declared_max_fill(single_stage_vehicle: Vehicle) -> None:
    """OI-03：加注比例 > 1.0 必须显式启用「最大允许加注量」，不得静默放行。"""
    overfilled = _mutate(
        single_stage_vehicle,
        lambda payload: payload["stages"][0].update({"fill_fraction": 1.05}),
    )
    items = _by_code(overfilled, "HARD_FILL_OVERFILL")
    assert [item.field_path for item in items] == ["stages[0].fill_fraction"]

    declared = _mutate(
        single_stage_vehicle,
        lambda payload: payload["stages"][0].update(
            {"fill_fraction": 1.05, "max_fill_mass_kg": 480_000.0}
        ),
    )
    assert _by_code(declared, "HARD_FILL_OVERFILL") == []


def test_overfill_is_reported_per_tank(single_stage_vehicle: Vehicle) -> None:
    vehicle = _mutate(
        single_stage_vehicle,
        lambda payload: payload["stages"][0]["geometry"]["oxidizer_tank"].update(
            {"fill_fraction": 1.2}
        ),
    )

    items = _by_code(vehicle, "HARD_FILL_OVERFILL")
    assert [item.field_path for item in items] == ["stages[0].geometry.oxidizer_tank.fill_fraction"]


def test_launch_site_is_required(single_stage_vehicle: Vehicle) -> None:
    """纬度是自转加成与转向损失的唯一输入，缺了它运力算不出来（§8.6）。"""
    inline = _mutate(
        single_stage_vehicle, lambda payload: payload["mission"].update({"launch_site": None})
    )
    items = _by_code(inline, "HARD_LAUNCH_SITE_MISSING")
    assert len(items) == 1
    assert items[0].field_path == "mission"

    referenced = _mutate(
        single_stage_vehicle,
        lambda payload: payload["mission"].update(
            {"launch_site": None, "launch_site_id": "cape-canaveral"}
        ),
    )
    assert _by_code(referenced, "HARD_LAUNCH_SITE_MISSING") == []
    assert _by_code(referenced, "COMPAT_LAUNCH_SITE_AMBIGUOUS") == []


def test_both_launch_site_forms_warn(single_stage_vehicle: Vehicle) -> None:
    vehicle = _mutate(
        single_stage_vehicle,
        lambda payload: payload["mission"].update({"launch_site_id": "cape-canaveral"}),
    )

    items = _by_code(vehicle, "COMPAT_LAUNCH_SITE_AMBIGUOUS")
    assert len(items) == 1
    assert items[0].level == "warning"
    assert "内联" in items[0].message


def test_orbit_requirements_depend_on_the_orbit_type(single_stage_vehicle: Vehicle) -> None:
    """轨道要素齐备性随 ``orbit_type`` 变：圆轨道要高度，转移轨道要近 / 远地点。"""
    missing_altitude = _mutate(
        single_stage_vehicle, lambda payload: payload["mission"].update({"altitude_m": None})
    )
    assert [
        item.field_path for item in _by_code(missing_altitude, "HARD_ORBIT_ALTITUDE_MISSING")
    ] == ["mission.altitude_m"]

    # LEO 只要求高度，不要求近 / 远地点
    assert _by_code(missing_altitude, "HARD_ORBIT_APSIS_MISSING") == []

    gto = _mutate(
        single_stage_vehicle,
        lambda payload: payload["mission"].update({"orbit_type": "GTO", "altitude_m": None}),
    )
    assert _by_code(gto, "HARD_ORBIT_ALTITUDE_MISSING") == []
    assert len(_by_code(gto, "HARD_ORBIT_APSIS_MISSING")) == 1

    reversed_apsis = _mutate(
        single_stage_vehicle,
        lambda payload: payload["mission"].update(
            {
                "orbit_type": "GTO",
                "perigee_altitude_m": 35_786_000.0,
                "apogee_altitude_m": 200_000.0,
            }
        ),
    )
    assert [item.field_path for item in _by_code(reversed_apsis, "HARD_ORBIT_APSIS_ORDER")] == [
        "mission.perigee_altitude_m"
    ]


def test_common_bulkhead_requires_type_and_matching_tanks(single_stage_vehicle: Vehicle) -> None:
    def enable(payload: dict[str, Any]) -> None:
        payload["stages"][0]["geometry"].update({"common_bulkhead": True})

    vehicle = _mutate(single_stage_vehicle, enable)

    assert len(_by_code(vehicle, "HARD_BULKHEAD_TYPE_MISSING")) == 1
    mismatches = _by_code(vehicle, "HARD_TANK_TYPE_MISMATCH")
    assert [item.field_path for item in mismatches] == [
        "stages[0].geometry.oxidizer_tank.tank_type",
        "stages[0].geometry.fuel_tank.tank_type",
    ]


def test_common_bulkhead_type_without_the_switch_warns(single_stage_vehicle: Vehicle) -> None:
    vehicle = _mutate(
        single_stage_vehicle,
        lambda payload: payload["stages"][0]["geometry"].update({"common_bulkhead_type": "plain"}),
    )

    items = _by_code(vehicle, "ENGINEER_BULKHEAD_TYPE_UNUSED")
    assert len(items) == 1
    assert not has_hard(items)


def test_lh2_common_bulkhead_requires_insulation(single_stage_vehicle: Vehicle) -> None:
    """§5.9 口径 2③：共底且液氢侧**强制**隔温（液氢 20 K，共享隔板不隔温会冻住另一侧）。"""

    def to_lh2(payload: dict[str, Any]) -> None:
        stage = payload["stages"][0]
        stage["propellant"] = "LOX/LH2"
        geometry = stage["geometry"]
        geometry.update({"common_bulkhead": True, "common_bulkhead_type": "insulated_sandwich"})
        geometry["oxidizer_tank"].update({"tank_type": "common_bulkhead"})
        geometry["fuel_tank"].update({"tank_type": "common_bulkhead"})

    vehicle = _mutate(single_stage_vehicle, to_lh2)

    items = _by_code(vehicle, "HARD_BULKHEAD_INSULATION_MISSING")
    assert [item.field_path for item in items] == [
        "stages[0].geometry.fuel_tank.common_bulkhead_insulation_m"
    ]

    def with_insulation(payload: dict[str, Any]) -> None:
        to_lh2(payload)
        payload["stages"][0]["geometry"]["fuel_tank"]["common_bulkhead_insulation_m"] = 0.02

    assert (
        _by_code(_mutate(single_stage_vehicle, with_insulation), "HARD_BULKHEAD_INSULATION_MISSING")
        == []
    )


def test_tank_diameter_must_not_exceed_stage(single_stage_vehicle: Vehicle) -> None:
    vehicle = _mutate(
        single_stage_vehicle,
        lambda payload: payload["stages"][0]["geometry"]["oxidizer_tank"].update(
            {"diameter_m": 4.0}
        ),
    )

    items = _by_code(vehicle, "COMPAT_TANK_DIAMETER_EXCEEDS_STAGE")
    assert [item.field_path for item in items] == ["stages[0].geometry.oxidizer_tank.diameter_m"]


def test_wall_thickness_must_stay_below_the_radius(single_stage_vehicle: Vehicle) -> None:
    """安全边界：壁厚 ≥ 半径时"壳"比"腔"还厚，几何无意义。"""

    def thicken(payload: dict[str, Any]) -> None:
        stage = payload["stages"][0]
        stage["wall_thickness_m"] = 2.0
        stage["geometry"]["oxidizer_tank"]["wall_thickness_m"] = 1.9

    vehicle = _mutate(single_stage_vehicle, thicken)

    assert [item.field_path for item in _by_code(vehicle, "SAFETY_TANK_WALL_TOO_THICK")] == [
        "stages[0].geometry.oxidizer_tank.wall_thickness_m"
    ]
    assert [item.field_path for item in _by_code(vehicle, "SAFETY_STAGE_WALL_TOO_THICK")] == [
        "stages[0].wall_thickness_m"
    ]


def test_engine_must_fit_inside_the_stage(single_stage_vehicle: Vehicle) -> None:
    vehicle = _mutate(
        single_stage_vehicle, lambda payload: payload["stages"][0].update({"length_m": 2.0})
    )

    items = _by_code(vehicle, "COMPAT_ENGINE_EXCEEDS_STAGE")
    assert [item.field_path for item in items] == ["stages[0].length_m"]


def test_default_isp_must_match_the_engine_nominal(single_stage_vehicle: Vehicle) -> None:
    """来源标 default 却与发动机标称值不一致 = "改了值却没改来源"，必须提示（UI 最容易漏）。"""
    vehicle = _mutate(
        single_stage_vehicle, lambda payload: payload["stages"][0].update({"isp_vacuum_s": 330.0})
    )

    items = _by_code(vehicle, "ENGINEER_ISP_SOURCE_MISMATCH")
    assert [item.field_path for item in items] == ["stages[0].isp_vacuum_s"]

    # 标为 custom 即视为用户有意覆写，不再提示
    custom = _mutate(
        single_stage_vehicle,
        lambda payload: payload["stages"][0].update(
            {"isp_vacuum_s": 330.0, "isp_source": "custom"}
        ),
    )
    assert _by_code(custom, "ENGINEER_ISP_SOURCE_MISMATCH") == []


def test_missing_aero_and_small_fairing_warn(single_stage_vehicle: Vehicle) -> None:
    """气动缺失按默认处理但必须留痕；整流罩小于最大级直径要提示确认。"""

    def strip_aero(payload: dict[str, Any]) -> None:
        payload.update({"aero": None, "fairing_diameter_m": 2.5})

    items = _diagnostics(_mutate(single_stage_vehicle, strip_aero))

    assert [item.code for item in items] == [
        "COMPAT_FAIRING_SMALLER_THAN_STAGE",
        "ENGINEER_AERO_DEFAULTED",
    ]
    assert not has_hard(items)


def test_recovery_requires_known_stage_and_method(single_stage_vehicle: Vehicle) -> None:
    def enable(payload: dict[str, Any]) -> None:
        payload.update({"recovery": {"enabled": True, "stage_indices": [3]}})

    vehicle = _mutate(single_stage_vehicle, enable)
    codes = _codes(vehicle)

    assert "HARD_RECOVERY_STAGE_UNKNOWN" in codes
    assert "HARD_RECOVERY_METHOD_MISSING" in codes

    def empty(payload: dict[str, Any]) -> None:
        payload.update({"recovery": {"enabled": True, "stage_indices": []}})

    assert "HARD_RECOVERY_STAGE_MISSING" in _codes(_mutate(single_stage_vehicle, empty))


def test_recovery_disabled_reports_nothing(single_stage_vehicle: Vehicle) -> None:
    """未启用回收时不得对回收层报任何裁定——否则用户会被"没勾选的功能"打扰。"""
    vehicle = _mutate(
        single_stage_vehicle,
        lambda payload: payload.update({"recovery": {"enabled": False, "stage_indices": [9]}}),
    )

    assert [item.code for item in _diagnostics(vehicle)] == []


def test_every_field_path_uses_array_indices(single_stage_vehicle: Vehicle) -> None:
    """路径必须与 pydantic 的 ``loc`` 同构（``stages[0]`` 而非 ``stages.0``）。"""

    def break_everything(payload: dict[str, Any]) -> None:
        payload["stages"][0].update({"index": 7, "fill_fraction": 1.5, "length_m": 1.0})
        payload["mission"].update({"launch_site": None, "altitude_m": None})
        payload.update({"aero": None})

    items = _diagnostics(_mutate(single_stage_vehicle, break_everything))

    assert len(items) >= 4, f"用例本身没造出足够多的违反：{[item.code for item in items]}"
    for item in items:
        assert _PATH_RE.match(item.field_path), f"字段路径形态不对：{item.field_path}"
        assert item.message.strip() and item.suggestion.strip()
    assert {item.level for item in items} <= {"hard", "warning"}


def test_constraints_never_raise(single_stage_vehicle: Vehicle) -> None:
    """约束层只**返回**裁定，不抛异常——拒绝与否由调用方（API 层）决定。

    这条划的是分层：若约束层自己抛，API 就只能得到一句异常消息，
    字段级清单会在跨层传递中丢掉（本项目已踩过的坑）。
    """
    vehicle = _mutate(
        single_stage_vehicle,
        lambda payload: payload["stages"][0].update({"index": 3, "fill_fraction": 2.0}),
    )

    items = _diagnostics(vehicle)

    assert len(items) >= 2
    assert len({(item.code, item.field_path) for item in items}) == len(items), (
        "同一处违反不得产出重复裁定（前端会渲染成两条缺陷）"
    )


def test_levels_are_only_hard_or_warning(single_stage_vehicle: Vehicle) -> None:
    """§6.3 只分「拒绝」（硬约束）与「警告」（工程 / 相容 / 安全边界）两级，刻意不设 info。"""
    hard_case = _mutate(
        single_stage_vehicle,
        lambda payload: payload["stages"][0].update({"fill_fraction": 1.5}),
    )
    warning_case = _mutate(
        single_stage_vehicle, lambda payload: payload["stages"][0].update({"isp_vacuum_s": 330.0})
    )

    assert {item.level for item in _diagnostics(hard_case)} == {"hard"}
    assert {item.level for item in _diagnostics(warning_case)} == {"warning"}
