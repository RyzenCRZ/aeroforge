"""内置模板库与 OI-34 名称匹配（规格 §11.5 ⑤ / §13.2 同源门禁 / OI-34）。

覆盖：清单与元数据形状、模板 Vehicle 过产品校验器（与夹具同一调用方式）、
``sourced_fields`` 对数值字段路径的全覆盖（含 ``boosters[i].stage.…``，OI-36）、
§13.2 同源门禁（级数 / 助推器数 / GLOW / 对照运力）、匹配口径（等值、宁漏勿错）、
三个端点契约、与起始箭互不影响。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from aeroforge.api.main import app
from aeroforge.errors import ParamsError
from aeroforge.params import template as skeleton
from aeroforge.params import templates
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.dag import propagate_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Vehicle

_TEMPLATE_IDS = {"falcon-9", "saturn-v", "cz-5", "falcon-heavy"}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """进入 lifespan 的客户端（与 test_api_params 的用法一致）。"""
    with TestClient(app) as instance:
        yield instance


def _glow_kg(template_id: str) -> float:
    """§13.2 门禁用：按模板自带的公开分项加注量传播 DAG，取 GLOW。

    推进剂质量不是 Schema 字段（§6.2 由 M4 定尺求解给出），故门禁把它作为图的输入
    提供——GLOW = 各级干质量（由 σ 派生）+ 推进剂 + 载荷。带助推器的构型
    （OI-36）再把各助推器组的 ``单枚公开加注量 / (1−σ)``（推进剂 + 干重）按组数量
    并入——0 级段不是串联级，DAG 不建它的节点（§8.5），但 GLOW 是整箭质量，
    门禁必须含它。
    """
    record = templates.get_template(template_id)
    vehicle = record.build_vehicle()
    masses = {i + 1: m for i, m in enumerate(record.stage_propellant_mass_kg)}
    result = propagate_vehicle(vehicle, propellant_mass_kg=masses)
    assert "vehicle.glow_kg" in result.values, f"GLOW 未算出：deferred={result.deferred}"
    glow = result.values["vehicle.glow_kg"]
    for group, propellant in zip(vehicle.boosters, record.booster_propellant_mass_kg, strict=True):
        glow += group.count * propellant / (1.0 - group.stage.structure_coefficient)
    return glow


def _numeric_leaf_paths(model: BaseModel, prefix: str = "") -> set[str]:
    """收集模型上全部**数值叶子字段**的 field_path（与 §6.3 的路径口径同构）。"""
    paths: set[str] = set()
    for name, value in model:
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, BaseModel):
            paths |= _numeric_leaf_paths(value, path)
        elif isinstance(value, (tuple, list)):
            for index, item in enumerate(value):
                if isinstance(item, BaseModel):
                    paths |= _numeric_leaf_paths(item, f"{path}[{index}]")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            paths.add(path)
    return paths


# ---------------------------------------------------------------------------
# 模块层：清单 / 工厂 / 校验器门禁 / 出处表
# ---------------------------------------------------------------------------


def test_list_contains_both_templates_with_metadata() -> None:
    records = templates.get_template_list()
    assert {record.id for record in records} == _TEMPLATE_IDS
    for record in records:
        assert record.name
        assert record.aliases and all(record.aliases)
        assert record.stage_count == len(record.stage_propellant_mass_kg)
        assert record.reference_payload_leo_kg > 0
        assert record.sourced_fields, "出处表不得为空"
        # §11.5 ⑤ 规则 2 / §13.2 同源声明必须原样写进 note
        assert "来源：公开资料整理" in record.note
        assert "§13.2 基准表同源" in record.note
        assert "M4 基准回归" in record.note


def test_get_template_unknown_id_raises_params_error() -> None:
    # 未知 id → ParamsError（API 层映射 422）；falcon-heavy / cz-5 已随
    # M4 Booster Schema（OI-36）入库，不再是"待补齐"的缺席者
    with pytest.raises(ParamsError):
        templates.get_template("does-not-exist")


@pytest.mark.parametrize("template_id", sorted(_TEMPLATE_IDS))
def test_template_vehicle_passes_the_product_validator(template_id: str) -> None:
    """模板门禁：构造完成的 Vehicle 过产品自己的约束校验器（无硬违反）。"""
    record = templates.get_template(template_id)
    vehicle = record.build_vehicle()
    assert not has_hard(check_vehicle(vehicle)), f"{template_id} 违反硬约束"
    # 工厂语义：每次调用返回全新实例
    assert vehicle is not record.build_vehicle()


@pytest.mark.parametrize("template_id", sorted(_TEMPLATE_IDS))
def test_sourced_fields_cover_every_numeric_field(template_id: str) -> None:
    """出处表必须覆盖模板自身 Vehicle 的**每一个**数值字段路径（抽验 + 全量）。"""
    record = templates.get_template(template_id)
    paths = _numeric_leaf_paths(record.build_vehicle())
    assert paths, "未找到任何数值字段（walker 自身失效）"
    missing = sorted(paths - record.sourced_fields.keys())
    assert not missing, f"数值字段缺出处条目：{missing}"
    # 路径口径抽验：与 §6.3 / §6.5 的 field_path 逐字一致
    assert "stages[0].engine.mixture_ratio" in record.sourced_fields
    assert "payload_mass_kg" in record.sourced_fields


def test_falcon9_is_the_section_13_2_baseline() -> None:
    record = templates.get_template("falcon-9")
    vehicle = record.build_vehicle()
    assert len(vehicle.stages) == 2
    assert vehicle.payload_mass_kg == 22_800.0
    assert record.reference_payload_leo_kg == 22_800.0
    # 公开分项质量（干重含回收硬件）合计 ≈ 571 t，与 §13.2 标称 549 t 在公开来源中本就
    # 不闭合（标称 GLOW 对应典型任务剖面）。门禁取 5%：校验同一量级与同一来源族；
    # 精确运力误差按 §13.2 归 M4 回归（阈值 < 15%）。
    assert _glow_kg("falcon-9") == pytest.approx(549_000.0, rel=0.05)


def test_saturnv_is_the_section_13_2_baseline() -> None:
    record = templates.get_template("saturn-v")
    vehicle = record.build_vehicle()
    assert len(vehicle.stages) == 3
    assert vehicle.payload_mass_kg == 140_000.0
    assert record.reference_payload_leo_kg == 140_000.0
    # 分项合计 ≈ 3,019 t，与 §13.2 标称 2,970 t 差 ~1.7%（资料级差异）；口径同上。
    assert _glow_kg("saturn-v") == pytest.approx(2_970_000.0, rel=0.05)


def test_cz5_is_the_section_13_2_baseline_with_boosters() -> None:
    """§13.2：长征五号 = 芯级 2 级串联 + 4× 并联助推器（级号 0，OI-36）。"""
    record = templates.get_template("cz-5")
    vehicle = record.build_vehicle()
    assert len(vehicle.stages) == 2  # 芯级串联数（助推器记级号 0，不入 stages）
    assert sum(group.count for group in vehicle.boosters) == 4
    assert vehicle.boosters[0].layout == "radial_even"  # M4 仅周向均布（OI-36 ④）
    assert vehicle.payload_mass_kg == 25_000.0
    assert record.reference_payload_leo_kg == 25_000.0
    # 分项合计 ≈ 841 t，与 §13.2 标称 867 t 差 ~3%（资料级差异）；口径同 Falcon 9。
    assert _glow_kg("cz-5") == pytest.approx(867_000.0, rel=0.05)


def test_falcon_heavy_is_the_section_13_2_baseline_with_boosters() -> None:
    """§13.2：Falcon Heavy = 芯级 + 2× 侧级助推器（与 F9 一级同构，级号 0）。"""
    record = templates.get_template("falcon-heavy")
    vehicle = record.build_vehicle()
    assert len(vehicle.stages) == 2
    assert sum(group.count for group in vehicle.boosters) == 2
    # 侧级与 F9 一级同构（公开构型事实）：同直径 / 同发动机 / 同 σ
    side = vehicle.boosters[0].stage
    assert (side.diameter_m, side.engine_count) == (
        vehicle.stages[0].diameter_m,
        vehicle.stages[0].engine_count,
    )
    assert side.structure_coefficient == vehicle.stages[0].structure_coefficient
    assert vehicle.payload_mass_kg == 63_800.0
    assert record.reference_payload_leo_kg == 63_800.0
    # 三芯同构分项合计 ≈ 1,485 t，与 §13.2 标称 1,420 t 差 ~4.6%（资料级差异，口径同上）。
    assert _glow_kg("falcon-heavy") == pytest.approx(1_420_000.0, rel=0.05)


# ---------------------------------------------------------------------------
# OI-34 名称匹配（等值口径，宁漏勿错）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_id"),
    [
        ("falcon 9", "falcon-9"),
        ("FALCON—9 ", "falcon-9"),  # 大写 + 破折号变体 + 尾随空白
        ("F9", "falcon-9"),
        ("猎鹰九号", "falcon-9"),
        ("ＦＡＬＣＯＮ－９", "falcon-9"),  # 全角字母 / 数字 / 连字符
        ("Saturn V", "saturn-v"),
        ("Saturn-V", "saturn-v"),
        ("土星五号", "saturn-v"),
        ("土星5号", "saturn-v"),
        ("长征五号", "cz-5"),
        ("LONG MARCH 5", "cz-5"),  # 大写变体
        ("胖五", "cz-5"),  # 昵称
        ("Falcon Heavy", "falcon-heavy"),
        ("falcon—heavy ", "falcon-heavy"),  # 小写 + 破折号变体 + 尾随空白
        ("猎鹰重型", "falcon-heavy"),
    ],
)
def test_match_hits_only_exact_normalized_names(name: str, expected_id: str) -> None:
    record = templates.match_template(name)
    assert record is not None
    assert record.id == expected_id


@pytest.mark.parametrize(
    "name",
    ["", "   ", "falcon-9x", "猎鹰", "Saturn", "猎鹰 9 号", "长征六号", "Falcon X"],
)
def test_near_miss_names_and_empty_never_match(name: str) -> None:
    """宁漏勿错：近似名一律不命中；空串 / 纯空白不报错、返回 None。"""
    assert templates.match_template(name) is None


def test_falcon_9_query_does_not_leak_to_falcon_heavy() -> None:
    """ "falcon-9" 规范化后**只**命中 falcon-9——入库 falcon-heavy 不得吞掉它。"""
    assert templates.match_template("falcon-9") is not None
    assert templates.match_template("falcon-9").id == "falcon-9"  # type: ignore[union-attr]


def test_normalize_name_is_the_single_normalization_point() -> None:
    assert templates.normalize_name("Ｆａｌｃｏｎ　９") == "falcon9"  # 全角→半角（NFKC）
    assert templates.normalize_name("FALCON—9 ") == "falcon9"  # 大小写 + 破折号 + 空白
    assert templates.normalize_name("Saturn-V") == "saturnv"  # 连字符变体
    assert templates.normalize_name("") == ""  # 空串归空串，由 match_template 判未命中


# ---------------------------------------------------------------------------
# API 层：三个端点 + 与起始箭互不影响
# ---------------------------------------------------------------------------


def test_templates_list_endpoint_shape(client: TestClient) -> None:
    response = client.get("/api/templates")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"templates"}
    assert {item["id"] for item in body["templates"]} == _TEMPLATE_IDS
    for item in body["templates"]:
        assert set(item) == {
            "id",
            "name",
            "aliases",
            "stage_count",
            "note",
            "reference_payload_leo_kg",
        }
        assert item["note"]


def test_template_detail_endpoint_returns_full_params(client: TestClient) -> None:
    response = client.get("/api/templates/saturn-v")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "id",
        "name",
        "note",
        "aliases",
        "reference_payload_leo_kg",
        "sourced_fields",
        "vehicle",
    }
    assert body["id"] == "saturn-v"
    assert body["vehicle"]["name"] == "Saturn V"
    assert body["sourced_fields"], "详情必须随下发逐控件出处"
    # 响应里的 vehicle 必须能原样重建为合法 Vehicle（前端拿到即可直接提交诊断）
    vehicle = Vehicle.model_validate(body["vehicle"])
    assert not has_hard(check_vehicle(vehicle))
    assert len(vehicle.stages) == 3


def test_template_detail_endpoint_carries_boosters(client: TestClient) -> None:
    """捆绑构型模板（OI-36）的详情：boosters 与其出处表随响应原样下发。"""
    response = client.get("/api/templates/cz-5")
    assert response.status_code == 200
    body = response.json()
    vehicle = Vehicle.model_validate(body["vehicle"])
    assert not has_hard(check_vehicle(vehicle))
    assert len(vehicle.boosters) == 1
    assert vehicle.boosters[0].count == 4
    # 出处表路径口径抽验：与 §6.3 的 field_path 逐字一致（含助推器侧级路径）
    assert "boosters[0].stage.diameter_m" in body["sourced_fields"]
    assert "boosters[0].stage.engine.thrust_vacuum_n" in body["sourced_fields"]


def test_template_detail_unknown_id_is_a_business_error(client: TestClient) -> None:
    response = client.get("/api/templates/does-not-exist")
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "PARAMS_CONSTRAINT_VIOLATION"
    assert error["stage"] == "params"
    assert error["suggestion"]


def test_match_endpoint_hits_and_misses(client: TestClient) -> None:
    hit = client.get("/api/templates/match", params={"name": "土星五号"})
    assert hit.status_code == 200
    body = hit.json()
    assert set(body) == {"matched", "template_id", "name", "note"}
    assert body["matched"] is True
    assert body["template_id"] == "saturn-v"
    assert body["name"] == "Saturn V"
    assert body["note"]

    hit_cz5 = client.get("/api/templates/match", params={"name": "长征五号"}).json()
    assert hit_cz5["matched"] is True
    assert hit_cz5["template_id"] == "cz-5"
    assert hit_cz5["name"] == "CZ-5"

    miss = client.get("/api/templates/match", params={"name": "Falcon Super Heavy"}).json()
    assert miss["matched"] is False
    assert miss["template_id"] is None
    assert miss["name"] is None


def test_match_endpoint_with_empty_name_is_not_an_error(client: TestClient) -> None:
    """缺省 name 参数 → matched=false，不报错（前端防抖会频繁发出空查询）。"""
    response = client.get("/api/templates/match")
    assert response.status_code == 200
    body = response.json()
    assert body["matched"] is False
    assert body["template_id"] is None


def test_skeleton_endpoint_is_unaffected(client: TestClient) -> None:
    """模板库与起始箭互不影响：骨架端点仍 200，label 原样不变，且不混入模板清单。"""
    response = client.get("/api/params/template")
    assert response.status_code == 200
    body = response.json()
    assert body["template_id"] == skeleton.TEMPLATE_ID
    assert body["label"] == skeleton.LABEL
    assert "未经来源核对" in body["label"]

    listed = {item["id"] for item in client.get("/api/templates").json()["templates"]}
    assert skeleton.TEMPLATE_ID not in listed
