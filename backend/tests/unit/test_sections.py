"""SectionBand 下发端点（POST /api/geometry/sections，§5.9 / §11.10，M5 第二片）。

覆盖口径（任务交付 3/4）：
- falcon-9 模板的 band 序列 = §5.9 九段序（自上而下；共底关闭无 common_bulkhead）；
- 储箱排列翻转（fuel_upper ⇒ fuel_first，燃料箱条带在数组更前）；
- 液面 ≤ 箱段高；**内核对拍**：build123d 实际体积 / 截面积反算液面，与下发值
  差 ≤ 1 mm（M5 验收判据）；
- labels 覆盖 §5.9 共性 7 全清单（总长 / 各箱长 / 前后裙高 / 级间（舱）高 /
  推力结构高 / 直径）；
- 共底：common_bulkhead band 出现 + saving_m 与装配树一致 + LH₂ 侧 insulation 标志；
- 助推器条带同构、级号 0；
- 三 Schema 增补（avionics / intertank / fairing_height）：显式值生效 + None 现状
  不变（canonical 纪律回归：缺省字段不进字节，既有输入缓存键不变）；
- reserved 复核：修复后装配树质量贡献总和 vs §8.4 几何解析账 rel < 5%（同源闭合）；
- 端点 200 形状 + 422（契约违约 / 分区铺不满）。
"""

from __future__ import annotations

from typing import Any

import build123d as bd
import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.geometry.assembly import build_assembly, plan_stage
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import (
    Aero,
    LaunchSite,
    Mission,
    Stage,
    Vehicle,
    canonical_json,
)
from aeroforge.params.templates import cz5_vehicle, falcon9_vehicle
from aeroforge.perf.mass import dry_mass_geometric_kg
from tests.unit.test_assembly import _stage, _vehicle

#: 液面内核对拍门禁（M5 验收判据：≤ 1 mm）。
LIQUID_LEVEL_TOL_M = 1e-3

#: 装配树 vs §8.4 账的闭合门禁（同源后应精确为 0，门禁放宽到 5%）。
RECONCILIATION_REL_TOL = 0.05


def _raw_vehicle(name: str, stage: Any, *, propellant: str = "LOX/RP-1") -> Vehicle:
    """绕过 _vehicle 硬违反门禁的裸车辆（负例 / LH₂ 共底夹具专用，同 test_assembly 口径）。"""
    return Vehicle(
        name=name,
        stages=(stage,),
        payload_mass_kg=8_000.0,
        material="al-2219",
        propellant=propellant,  # type: ignore[arg-type]
        aero=Aero(drag_coefficient=0.3),
        mission=Mission(
            orbit_type="LEO",
            altitude_m=200_000.0,
            inclination_deg=28.5,
            launch_site=LaunchSite(
                name="Cape Canaveral", latitude_deg=28.5, altitude_m=3.0, azimuth_deg=90.0
            ),
        ),
    )


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as instance:
        return instance


def _sections(client: TestClient, vehicle: Vehicle) -> dict[str, Any]:
    response = client.post(
        "/api/geometry/sections", json={"vehicle": vehicle.model_dump(mode="json")}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _band_map(stage_entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """section 名 → 条带（前端按名寻址的同一读法）。"""
    return {band["section"]: band for band in stage_entry["bands"]}


# ---------------------------------------------------------------------------
# band 序列 = §5.9 九段序 + 储箱排列翻转
# ---------------------------------------------------------------------------


def test_falcon9_band_sequence_follows_section_order(client: TestClient) -> None:
    """falcon-9：级内条带 = 九段序（自上而下）；共底关闭 ⇒ 无 common_bulkhead band。"""
    body = _sections(client, falcon9_vehicle())
    assert len(body["stages"]) == 2 and body["boosters"] == []
    # 一级（无整流罩挂载）：前裙 → 氧箱 → 级间舱 → 燃料箱 → 推力结构 → 发动机舱
    assert [band["section"] for band in body["stages"][0]["bands"]] == [
        "forward_skirt",
        "ox_tank",
        "intertank",
        "fuel_tank",
        "thrust_structure",
        "engine_bay",
    ]
    # 顶级前置整流罩 / 载荷适配器（§5.9 表第 1/2 段）
    assert [band["section"] for band in body["stages"][1]["bands"]][:2] == ["fairing", "adapter"]
    for stage_entry in body["stages"]:
        assert "common_bulkhead" not in _band_map(stage_entry), "共底关闭时不得出现隔板 band"
    # 条带高度恰铺满：Σ(全部芯级 band) = dimensions.total_length_m
    total = sum(band["length_m"] for s in body["stages"] for band in s["bands"])
    assert total == pytest.approx(body["dimensions"]["total_length_m"], rel=1e-12)


def test_tank_order_flip_reads_field(client: TestClient) -> None:
    """储箱排列翻转（§5.9 非铁律）：fuel_upper ⇒ tank_order=fuel_first 且燃料箱条带在前。"""
    flipped = _sections(client, _vehicle((_stage(1, tank_arrangement="fuel_upper"),)))
    normal = _sections(client, _vehicle((_stage(1, tank_arrangement="oxidizer_upper"),)))
    assert flipped["stages"][0]["tank_order"] == "fuel_first"
    assert normal["stages"][0]["tank_order"] == "oxidizer_first"
    flipped_sections = [band["section"] for band in flipped["stages"][0]["bands"]]
    normal_sections = [band["section"] for band in normal["stages"][0]["bands"]]
    assert flipped_sections.index("fuel_tank") < flipped_sections.index("ox_tank")
    assert normal_sections.index("ox_tank") < normal_sections.index("fuel_tank")
    assert set(flipped_sections) == set(normal_sections), "分区集合不变，只有 5/7 段次序对调"


def test_boosters_bands_are_isomorphic_with_level_zero(client: TestClient) -> None:
    """助推器条带与芯级同构（九段齐备），级号 0，标注前缀 b<组序>。"""
    body = _sections(client, cz5_vehicle())
    assert len(body["boosters"]) == 1
    booster = body["boosters"][0]
    assert booster["level"] == 0
    assert booster["stage_index"] == 0
    assert {band["section"] for band in booster["bands"]} == {
        "forward_skirt",
        "ox_tank",
        "intertank",
        "fuel_tank",
        "thrust_structure",
        "engine_bay",
    }
    core_keys = {label["key"] for label in body["dimensions"]["labels"]}
    assert "b0_ox_tank_length" in core_keys and "b0_fuel_tank_length" in core_keys


# ---------------------------------------------------------------------------
# 液面：≤ 箱段高 + build123d 内核对拍（≤ 1 mm）
# ---------------------------------------------------------------------------


def _kernel_liquid_level_m(radius_m: float, band_length_m: float, fill_fraction: float) -> float:
    """内核反算液面（m）：build123d 实际体积 ÷ 实际截面积 × 加注比例。

    条带实体为柱段（装配树外模线），截面积取内核实测的端面面积——
    不用解析 πr²，保证对拍是「几何内核 vs 下发值」而非「公式 vs 公式」。
    """
    solid = bd.Solid.make_cylinder(radius_m, band_length_m)
    section_area = float(solid.faces().sort_by(bd.Axis.Z)[0].area)
    return float(fill_fraction * float(solid.volume) / section_area)


def _tank_radius_m(stage: Stage, role: str) -> float:
    tank = stage.geometry.oxidizer_tank if role == "oxidizer" else stage.geometry.fuel_tank
    return (tank.diameter_m or stage.diameter_m) / 2.0


def test_liquid_level_within_band_height(client: TestClient) -> None:
    """液面必须落在 [0, 箱段高] 内（气枕区 = 同色 15% 透明，§11.10）。"""
    body = _sections(client, falcon9_vehicle())
    for stage_entry in body["stages"]:
        for band in stage_entry["bands"]:
            if band["liquid_level_m"] is None:
                continue
            assert 0.0 < band["liquid_level_m"] <= band["length_m"], (
                f"{band['section']} 液面 {band['liquid_level_m']} 越界（箱段高 {band['length_m']}）"
            )


def test_liquid_level_kernel_cross_check_falcon9_stage1_ox(client: TestClient) -> None:
    """内核对拍（M5 验收判据）：falcon-9 一级氧箱，反算液面与下发值差 ≤ 1 mm。"""
    vehicle = falcon9_vehicle()
    body = _sections(client, vehicle)
    band = _band_map(body["stages"][0])["ox_tank"]
    stage = vehicle.stages[0]
    kernel = _kernel_liquid_level_m(
        _tank_radius_m(stage, "oxidizer"),
        band["length_m"],
        stage.geometry.oxidizer_tank.fill_fraction,
    )
    deviation_m = abs(kernel - band["liquid_level_m"])
    assert deviation_m <= LIQUID_LEVEL_TOL_M, (
        f"内核 {kernel:.6f} m vs 下发 {band['liquid_level_m']:.6f} m"
    )


def test_liquid_level_kernel_cross_check_two_stage_fixture(client: TestClient) -> None:
    """内核对拍（合成两级夹具）：两级氧箱逐一 ≤ 1 mm。"""
    vehicle = _vehicle((_stage(1), _stage(2, length_m=12.6)))
    body = _sections(client, vehicle)
    for position, stage in enumerate(vehicle.stages):
        band = _band_map(body["stages"][position])["ox_tank"]
        kernel = _kernel_liquid_level_m(
            _tank_radius_m(stage, "oxidizer"),
            band["length_m"],
            stage.geometry.oxidizer_tank.fill_fraction,
        )
        assert abs(kernel - band["liquid_level_m"]) <= LIQUID_LEVEL_TOL_M


# ---------------------------------------------------------------------------
# 尺寸标注全清单（§5.9 共性 7）
# ---------------------------------------------------------------------------


def test_labels_cover_commonality_seven_checklist(client: TestClient) -> None:
    """labels 覆盖共性 7 全清单：总长 / 各箱长 / 前后裙高 / 级间舱高 / 推力结构高 / 直径。"""
    body = _sections(client, falcon9_vehicle())
    keys = {label["key"] for label in body["dimensions"]["labels"]}
    assert {"total_length", "max_diameter", "fairing_diameter"} <= keys
    for prefix in ("s1", "s2"):
        assert {
            f"{prefix}_ox_tank_length",
            f"{prefix}_fuel_tank_length",
            f"{prefix}_forward_skirt_height",
            f"{prefix}_thrust_structure_height",
            f"{prefix}_intertank_height",
        } <= keys
    # 数值文本与量测同源（后端下发，前端只排版）
    by_key = {label["key"]: label["text"] for label in body["dimensions"]["labels"]}
    assert by_key["total_length"] == f"{body['dimensions']['total_length_m']:.2f} m"
    assert by_key["max_diameter"] == f"{body['dimensions']['max_diameter_m']:.2f} m"


def test_labels_and_bands_absent_for_degenerate_sections(client: TestClient) -> None:
    """无整流罩 ⇒ 无 fairing/adapter band 与 fairing_diameter 标注；缺省仪器舱 0 高不进数组。"""
    body = _sections(client, _vehicle((_stage(1),), fairing_diameter_m=None))
    sections = [band["section"] for band in body["stages"][0]["bands"]]
    assert "fairing" not in sections and "adapter" not in sections and "avionics" not in sections
    keys = {label["key"] for label in body["dimensions"]["labels"]}
    assert "fairing_diameter" not in keys
    assert body["dimensions"]["fairing_diameter_m"] is None


# ---------------------------------------------------------------------------
# 共底：band 出现 + saving 与装配树一致 + LH₂ 侧隔热标志
# ---------------------------------------------------------------------------


def test_common_bulkhead_band_saving_and_insulation(client: TestClient) -> None:
    """共底开启：common_bulkhead band 出现，saving_m 与装配树一致；LH₂ 侧 insulation=true。"""
    vehicle = _vehicle(
        (_stage(1, common_bulkhead=True, flatness_ratio=0.6),), fairing_diameter_m=None
    )
    body = _sections(client, vehicle)
    band = _band_map(body["stages"][0])["common_bulkhead"]
    assembly = build_assembly(vehicle)
    assert band["bulkhead_saving_m"] == pytest.approx(assembly.saving_by_stage[1], rel=1e-12)
    # 级间舱与隔板互斥（§5.9 表第 6 段二选一）
    assert "intertank" not in _band_map(body["stages"][0])
    # 隔板段高度 = 隔板矢高（装配树分区事实；saving = 两封头矢高和 − 隔板矢高）
    assert band["length_m"] == pytest.approx(0.6 * 3.7 / 2.0, rel=1e-12)
    # LOX/RP-1 共底：无 LH₂ 侧，隔热标志 false
    assert band["insulation"] is False


def test_common_bulkhead_lh2_insulation_flag_and_warning(client: TestClient) -> None:
    """LH₂ 侧共底：insulation=true（有隔热层）。"""
    stage = _stage(1, common_bulkhead=True, propellant="LOX/LH2")
    vehicle = _raw_vehicle(
        "LH2 共底箭",
        stage.model_copy(
            update={
                "geometry": stage.geometry.model_copy(
                    update={
                        "fuel_tank": stage.geometry.fuel_tank.model_copy(
                            update={"common_bulkhead_insulation_m": 0.05}
                        )
                    }
                )
            }
        ),
        propellant="LOX/LH2",
    )
    body = _sections(client, vehicle)
    assert _band_map(body["stages"][0])["common_bulkhead"]["insulation"] is True
    assert body["warnings"] == []


def test_bulkhead_interference_maps_to_422(client: TestClient) -> None:
    """分区不可行（共底矢高干涉）⇒ 422 GEOMETRY_INVALID（错误体走 §10.3）。"""
    bad = _stage(1, length_m=7.5, flatness_ratio=0.9, common_bulkhead=True)
    vehicle = _raw_vehicle("干涉箭", bad)
    response = client.post(
        "/api/geometry/sections", json={"vehicle": vehicle.model_dump(mode="json")}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "GEOMETRY_INVALID"


# ---------------------------------------------------------------------------
# 三 Schema 增补：显式值生效 + None 现状不变（canonical 纪律回归）
# ---------------------------------------------------------------------------


def test_schema_additions_explicit_values_take_effect(client: TestClient) -> None:
    """显式仪器舱 / 级间舱 / 整流罩高生效（高度对拍）：显式值优先于派生 / 惯例常量。"""
    vehicle = _vehicle(
        (
            _stage(1, avionics_height_m=1.2, intertank_height_m=2.5),
            _stage(2, length_m=12.6, avionics_height_m=0.6),
        )
    )
    body = _sections(client, vehicle)
    first = _band_map(body["stages"][0])
    assert first["avionics"]["length_m"] == pytest.approx(1.2, rel=1e-12)
    assert first["intertank"]["length_m"] == pytest.approx(2.5, rel=1e-12)
    # 顶级次序（§5.9）：整流罩 → 适配器 → 仪器舱 → 前裙
    second_sections = [band["section"] for band in body["stages"][1]["bands"]]
    assert second_sections[:4] == ["fairing", "adapter", "avionics", "forward_skirt"]
    # 显式级间舱 / 仪器舱占高后，箱段相应缩短（分区仍恰好铺满级长）
    layout = plan_stage(vehicle.stages[0])
    dome = 0.5 * 3.7 / 2.0
    assert layout.l_ox + layout.l_fuel == pytest.approx(
        vehicle.stages[0].length_m - vehicle.stages[0].engine_height_m - (dome + 2.5 + dome) - 1.2,
        rel=1e-9,
    )


def test_schema_additions_none_keeps_current_behaviour_and_bytes() -> None:
    """None = 现状：仪器舱 0 高不产带、级间舱 = 封头矢高和；canonical 字节不含新增字段
    （既有输入的缓存键逐字节不变，§9.2）。"""
    legacy = _vehicle((_stage(1),), fairing_diameter_m=None)
    # 布局与派生公式逐一对拍（None 路径与第一片行为一致）
    layout = plan_stage(legacy.stages[0])
    dome = 0.5 * 3.7 / 2.0
    assert [band.section for band in layout.bands if band.section == "avionics"] == []
    assert layout.h_mid == pytest.approx(2 * dome, rel=1e-12)
    # canonical：省略字段与显式 None 得到同一字节，且字节中不含新字段名
    omitted = _stage(1)
    explicit_none = omitted.model_copy(
        update={"avionics_height_m": None, "intertank_height_m": None}
    )
    vehicle_omitted = _vehicle((omitted,), fairing_diameter_m=None)
    vehicle_explicit_none = _vehicle((explicit_none,), fairing_diameter_m=None)
    assert canonical_json(vehicle_omitted) == canonical_json(vehicle_explicit_none)
    assert "avionics_height_m" not in canonical_json(vehicle_omitted)
    assert "intertank_height_m" not in canonical_json(vehicle_omitted)
    assert "fairing_height_m" not in canonical_json(vehicle_omitted)
    # 显式值进字节（可被缓存键区分），且过约束校验器（ge=0 值域合法）
    explicit = _stage(1, avionics_height_m=1.2)
    assert "avionics_height_m" in explicit.model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )
    assert not has_hard(check_vehicle(_vehicle((explicit,), fairing_diameter_m=None)))


def test_explicit_fairing_height_overrides_convention(client: TestClient) -> None:
    """整流罩高：显式值优先；None 保持工程惯例常量（现状值）。"""
    explicit = falcon9_vehicle().model_copy(update={"fairing_height_m": 8.0})
    body = _sections(client, explicit)
    assert _band_map(body["stages"][1])["fairing"]["length_m"] == pytest.approx(8.0, rel=1e-12)
    default = _sections(client, falcon9_vehicle())
    # 惯例常量：min(max(2.2×5.2, 5), 20) = 11.44 m（assembly 现状值不变）
    assert _band_map(default["stages"][1])["fairing"]["length_m"] == pytest.approx(11.44, rel=1e-12)


def test_fairing_height_default_hint_only_in_diagnostics() -> None:
    """惯例值提示只进诊断查询（ENGINEER_FAIRING_HEIGHT_DEFAULTED），不进 sections warnings。"""
    vehicle = falcon9_vehicle()
    codes = {item.code for item in check_vehicle(vehicle)}
    assert "ENGINEER_FAIRING_HEIGHT_DEFAULTED" in codes
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        body = _sections(client, vehicle)
    assert all("惯例" not in warning for warning in body["warnings"])


# ---------------------------------------------------------------------------
# reserved 复核：装配树质量贡献 vs §8.4 几何解析账（同源闭合 rel < 5%）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [falcon9_vehicle, cz5_vehicle],
    ids=["falcon-9", "cz-5"],
)
def test_assembly_mass_reconciles_with_geometric_account(factory: Any) -> None:
    """修复后两账同源：逐级分区质量总和 vs dry_mass_geometric_kg rel < 5%（实测 0）。"""
    vehicle = factory()
    for stage in vehicle.stages:
        layout = plan_stage(stage)
        section_mass = sum(band.mass_kg for band in layout.bands)
        account = dry_mass_geometric_kg(stage)
        assert account > 0.0
        assert section_mass == pytest.approx(account, rel=RECONCILIATION_REL_TOL), (
            f"第 {stage.index} 级装配账 {section_mass:.3f} kg vs §8.4 账 {account:.3f} kg"
        )


def test_endpoint_shape_and_request_validation(client: TestClient) -> None:
    """200 响应形状（契约定死）+ 契约违约 422。"""
    body = _sections(client, falcon9_vehicle())
    assert set(body) == {"stages", "boosters", "dimensions", "warnings", "provenance"}
    for stage_entry in body["stages"]:
        assert set(stage_entry) == {
            "stage_index",
            "level",
            "bands",
            "delivery_pipe_routing",
            "tank_order",
        }
        for band in stage_entry["bands"]:
            assert {"section", "label_zh", "length_m"} <= set(band)
    assert set(body["dimensions"]) == {
        "total_length_m",
        "max_diameter_m",
        "fairing_diameter_m",
        "labels",
    }
    assert body["stages"][0]["stage_index"] == 0 and body["stages"][0]["level"] == 1
    # 契约违约（未知字段 / 缺 stages）⇒ 422
    vehicle = falcon9_vehicle()
    bad_payload = {"vehicle": vehicle.model_dump(mode="json"), "extra": 1}
    assert client.post("/api/geometry/sections", json=bad_payload).status_code == 422
    assert client.post("/api/geometry/sections", json={"vehicle": {}}).status_code == 422
