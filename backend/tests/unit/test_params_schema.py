"""§6.1 Schema 分层与字段级错误翻译（M2 验收项 1：非法参数被拒并给字段级建议）。

本文件只测**结构层**（类型 / 枚举 / 值域 / 必填）与它到 §6.3 六字段裁定的翻译；
跨字段的领域约束在 ``test_params_diagnostics.py`` 与 §6.5 规则集里测。
"""

from __future__ import annotations

import inspect
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from aeroforge.params import schema as schema_module
from aeroforge.params.report import Diagnostic
from aeroforge.params.schema import (
    ParamsModel,
    Vehicle,
    errors_to_diagnostics,
    to_diagnostics,
)


def _raw(vehicle: Vehicle) -> dict[str, Any]:
    """取一份可变形的原始 dict（``model_dump`` 会深拷贝，改它不影响夹具）。"""
    return vehicle.model_dump(mode="json")


def _diagnostics(payload: dict[str, Any]) -> list[Diagnostic]:
    with pytest.raises(ValidationError) as excinfo:
        Vehicle.model_validate(payload)
    return to_diagnostics(excinfo.value)


def test_diagnostic_shape_is_six_fields(single_stage_vehicle: Vehicle) -> None:
    """裁定结构必须是**六字段**：OI-11 把 ``impact`` 判给 M4，多一个字段即越界。

    这条断言的价值在于"防回归"——M2 期间最容易发生的事就是有人为了"更完整"
    顺手把 ``impact`` 加回来，而它必须由 §8.7 敏感度实算，不得凭空给百分比。
    """
    assert set(Diagnostic.model_fields) == {
        "level",
        "code",
        "field_path",
        "message",
        "suggestion",
    }


def test_fastapi_body_prefix_does_not_leak_into_field_paths(single_stage_vehicle: Vehicle) -> None:
    """FastAPI 的 ``loc`` 多一层 ``body`` 前缀，必须剥掉（实测缺陷的防回归）。

    pydantic 直接校验给的 loc 是 ``('stages', 0, 'engine', 'mixture_ratio')``，
    而 FastAPI 的 ``RequestValidationError`` 给的是 ``('body', 'stages', 0, ...)``。
    若不剥，API 下发的路径就比 §6.3 约束层多一层前缀，前端"路径 → 控件"映射对不上——
    偏偏这个错误本身看起来完全正常（"有路径、有建议"），故只有机检能拦住它。
    """
    payload = _raw(single_stage_vehicle)
    payload["stages"][0]["engine"]["mixture_ratio"] = -1.0
    with pytest.raises(ValidationError) as excinfo:
        Vehicle.model_validate(payload)

    from_pydantic = to_diagnostics(excinfo.value)
    from_fastapi = errors_to_diagnostics(
        [{**error, "loc": ("body", *error["loc"])} for error in excinfo.value.errors()]
    )

    assert [item.field_path for item in from_fastapi] == [item.field_path for item in from_pydantic]
    assert from_pydantic[0].field_path == "stages[0].engine.mixture_ratio"


def test_body_level_error_falls_back_to_the_vehicle_root() -> None:
    """只有位置前缀、没有字段名时（请求体整体不是对象）落在 ``vehicle``，不得变成空串。"""
    items = errors_to_diagnostics(
        [{"type": "model_type", "loc": ("body",), "msg": "需要对象", "input": "{}"}]
    )

    assert [item.field_path for item in items] == ["vehicle"]


def test_unknown_field_is_rejected_with_spelling_suggestion(single_stage_vehicle: Vehicle) -> None:
    """拼错的字段必须被拒——静默吞掉等于把用户的输入丢进黑洞（R-28）。"""
    payload = _raw(single_stage_vehicle)
    payload["stages"][0]["structure_coeficient"] = 0.05  # 少一个 f
    items = _diagnostics(payload)

    assert [item.code for item in items] == ["PARAMS_EXTRA_FORBIDDEN"]
    assert items[0].level == "hard"
    assert "拼写" in items[0].suggestion


def test_field_path_mirrors_array_index(single_stage_vehicle: Vehicle) -> None:
    """字段路径必须是 ``stages[0].engine.mixture_ratio`` 形态（与前端控件映射同构）。"""
    payload = _raw(single_stage_vehicle)
    payload["stages"][0]["engine"]["mixture_ratio"] = -1.0
    items = _diagnostics(payload)

    assert len(items) == 1, f"嵌套字段错不应连带出容器级错误，实际：{items}"
    assert items[0].field_path == "stages[0].engine.mixture_ratio"
    assert items[0].level == "hard"


def test_empty_stages_is_reported_as_a_length_error(single_stage_vehicle: Vehicle) -> None:
    """真正的"一级都没有"必须如实报出来——那是 ``PARAMS_TOO_SHORT``，不能一并滤掉。"""
    payload = _raw(single_stage_vehicle)
    payload["stages"] = []
    items = _diagnostics(payload)

    assert [item.code for item in items] == ["PARAMS_TOO_SHORT"]
    assert items[0].field_path == "stages"


def test_bound_error_spells_out_the_bound(single_stage_vehicle: Vehicle) -> None:
    """上下界类错误必须把界值写进建议里，否则用户只知道"不合法"、不知道"该填多少"。"""
    payload = _raw(single_stage_vehicle)
    payload["stages"][0]["structure_coefficient"] = 1.5
    items = _diagnostics(payload)

    assert items[0].code == "PARAMS_LESS_THAN"
    assert "< 1.0" in items[0].suggestion


def test_enum_error_points_at_the_enum(single_stage_vehicle: Vehicle) -> None:
    """枚举错要给"取值必须来自该字段的枚举清单"，而不是一句"取值非法"。"""
    payload = _raw(single_stage_vehicle)
    payload["mission"]["orbit_type"] = "低地球轨道"
    items = _diagnostics(payload)

    assert items[0].field_path == "mission.orbit_type"
    assert "枚举" in items[0].suggestion


def test_every_suggestion_is_non_empty(single_stage_vehicle: Vehicle) -> None:
    """§10.3：每一条裁定都必须带可操作建议——"未知错误"式响应视为实现缺陷。"""
    payload = _raw(single_stage_vehicle)
    payload["stages"] = "不是数组"
    payload["payload_mass_kg"] = "二十吨"
    payload["material"] = 42

    for item in _diagnostics(payload):
        assert item.suggestion.strip(), f"{item.field_path} 的建议为空"
        assert item.message.strip(), f"{item.field_path} 的说明为空"


def test_vehicle_round_trips_through_json(single_stage_vehicle: Vehicle) -> None:
    """Schema 版本随飞行器存储（§6.1），故序列化往返必须无损。"""
    raw = single_stage_vehicle.model_dump_json()
    assert Vehicle.model_validate_json(raw) == single_stage_vehicle


def test_canonical_json_omits_booster_and_flatness_defaults(
    single_stage_vehicle: Vehicle,
) -> None:
    """缓存纪律（§9.2，OI-36/OI-37）：新增字段**不得**改变既有输入的 canonical 字节。

    基线 vehicle（无助推器、无扁度）的 canonical JSON 与"未加这两个字段前"的键集合
    等价——直接断言序列化结果中不含 ``boosters`` / ``flatness_ratio`` 键；同时断言
    「显式取默认值」与「省略该字段」得到**同一字节**（二者是同一个模型，本就该命中
    同一份产物）。
    """
    import json

    from aeroforge.params.schema import Booster, canonical_json

    payload = json.loads(canonical_json(single_stage_vehicle))
    assert "boosters" not in payload, "基线 vehicle 的 canonical JSON 不得出现 boosters 键"
    for stage in payload["stages"]:
        assert "flatness_ratio" not in stage, (
            "无扁度输入的 canonical JSON 不得出现 flatness_ratio 键"
        )

    # 显式空助推器列表 / 显式 None 扁度 == 省略（同一字节）
    explicit_empty = single_stage_vehicle.model_copy(update={"boosters": []})
    assert canonical_json(explicit_empty) == canonical_json(single_stage_vehicle)

    explicit_none_stage = single_stage_vehicle.stages[0].model_copy(update={"flatness_ratio": None})
    explicit_none = single_stage_vehicle.model_copy(update={"stages": (explicit_none_stage,)})
    assert canonical_json(explicit_none) == canonical_json(single_stage_vehicle)

    # 带助推器的构型必须**改变**字节（不同构型不得共享同一份产物）
    boosterized = single_stage_vehicle.model_copy(
        update={"boosters": [Booster(stage=single_stage_vehicle.stages[0], count=2)]}
    )
    assert canonical_json(boosterized) != canonical_json(single_stage_vehicle)
    # 扁度显式取值同样改变字节
    flat_stage = single_stage_vehicle.stages[0].model_copy(update={"flatness_ratio": 0.4})
    assert canonical_json(
        single_stage_vehicle.model_copy(update={"stages": (flat_stage,)})
    ) != canonical_json(single_stage_vehicle)

    # canonical 形态本身：键序固定、无空白（sort_keys + 紧凑分隔符）
    text = canonical_json(single_stage_vehicle)
    assert text == json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert ": " not in text and ", " not in text


def test_si_field_publishes_display_unit(single_stage_vehicle: Vehicle) -> None:
    """SI 字段必须把 ``unit`` / ``display_unit`` 下发到 OpenAPI（§6.1 / §6.4）。

    前端据此显示单位而**不自建换算表**；若这条元数据丢了，界面就只能硬编码，
    §6.4 的"换算只在 API 边界发生"随即失效。
    """
    schema = Vehicle.model_json_schema()
    payload_mass = schema["properties"]["payload_mass_kg"]
    assert payload_mass["unit"] == "kg"
    assert payload_mass["display_unit"] == "t"


def test_propellant_phase_defaults_to_liquid_without_breaking_existing_payloads(
    single_stage_vehicle: Vehicle,
) -> None:
    """§1.7.3 OI-30：相态默认 ``liquid``，**不带该字段的既有参数仍须原样通过**。

    这不是"顺手给个默认值"：默认值一旦缺失，这次 Schema 扩展就会让所有既有飞行器
    校验失败（与 OI-21～OI-26 同口径——扩展不得破坏既有参数）。
    """
    assert single_stage_vehicle.stages[0].engine.propellant_phase == "liquid"

    payload = single_stage_vehicle.model_dump(mode="json")
    for stage in payload["stages"]:
        stage["engine"].pop("propellant_phase")

    assert Vehicle.model_validate(payload).stages[0].engine.propellant_phase == "liquid"

    # 该字段是 §6.5 固体档的**唯一判据**，故必须出现在契约里（前端才能编辑它）
    properties = Vehicle.model_json_schema()["$defs"]["Engine"]["properties"]
    assert properties["propellant_phase"]["enum"] == ["liquid", "solid", "hybrid"]


def _declared_models() -> list[type[ParamsModel]]:
    """§6.1 里声明的全部 pydantic 模型（顺序固定，便于参数化测试的稳定 id）。"""
    return sorted(
        (
            member
            for _, member in inspect.getmembers(schema_module, inspect.isclass)
            if issubclass(member, ParamsModel) and member is not ParamsModel
        ),
        key=lambda model: model.__name__,
    )


@pytest.mark.parametrize("model", _declared_models(), ids=lambda model: model.__name__)
def test_nullable_fields_are_optional_in_the_contract(model: type[ParamsModel]) -> None:
    """声明为 ``X | None`` 的字段必须**可省略**（进 OpenAPI 的 ``required`` 即失败）。

    这条门禁来自一次实测缺陷：``float | None = Field(gt=0.0)`` 在 pydantic 里是**必填**——
    ``| None`` 只说明"值可以为 null"，不说明"键可以不给"。文档写着"省略 = 继承该级直径"，
    而契约却要求必填，前端照 schema 生成表单就再也没法省略该字段。
    修法：给出 ``default=None``。若确有"必须出现、但值可为 null"的字段，
    请在此显式登记例外并说明理由，而不是让它悄悄溜过。
    """
    required = set(model.model_json_schema().get("required", []))
    nullable = {
        name
        for name, field in model.model_fields.items()
        if type(None) in get_args(field.annotation)
    }
    offenders = sorted(nullable & required)
    assert not offenders, (
        f"{model.__name__} 的字段 {offenders} 声明为可空却在 OpenAPI 里是 required："
        "请给它们加 default（通常 default=None）"
    )
