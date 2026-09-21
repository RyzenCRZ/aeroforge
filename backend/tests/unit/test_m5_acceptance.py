"""M5 验收门禁（规格 §16 M5 行验收判据的机器判定，M5 收官片）。

五项门禁逐条落点：
1. **几何 vs 回归质量 <20%**：逐级几何解析干重 vs GCAT σ 推算干重（:func:`cross_check`
   的固化）。⚠ M6 前置专项①（分部位物理干重模型：承压/轴压双路壁厚取大 + 发动机
   T/W 推算 + 非贮箱分数闭环）细化后 **9/11 级转绿（dev 0.05%–17.4%）**；余 2 级为
   公开分项质量本底越模型颗粒度，按 §13.2 口径以 ``xfail(strict=True)`` 如实登记
   （模型再改进使断言转绿，strict 标记即反过来失败，强制清账；不放宽断言、不消音）。
2. **分区枚举一致性（无漏件无错序）**：assembly_tree 节点分区集合 == sections bands
   分区集合（逐级对比；falcon-9 + cz-5 + 合成两级）。对比域 = §5.9 的轴向分区
   （含级间段扩展枚举）——尾翼 / 助推器 / 喷管为**非轴向**扩展件，不在 band 域内。
3. **STEP 可被 FreeCAD 打开的机器判据**：导出的 STEP 用 OCP ``STEPControl_Reader``
   读回，实体数 > 0 且总体积与内核实体体积 rel < 0.1%（FreeCAD 目视验证登记留白）。
4. **回收质量代价可解释**：sequence 响应代价三项分解齐全且加总 = 总代价。
5. **三基准复绿**：直接复跑既有 test_m4_benchmarks 的断言（F9 / CZ-5 / 土星五号）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.geometry import _BAND_SECTION_NAMES
from aeroforge.api.main import app
from aeroforge.geometry.assembly import (
    SECTION_ORDER,
    build_assembly,
    vehicle_node_index,
)
from aeroforge.geometry.revolve import export_step
from aeroforge.params.schema import Booster, Stage, Vehicle
from aeroforge.params.templates import (
    cz5_vehicle,
    falcon9_vehicle,
    falcon_heavy_vehicle,
    saturnv_vehicle,
)
from aeroforge.perf.mass import cross_check
from tests.unit.test_assembly import _stage, _vehicle
from tests.unit.test_m4_benchmarks import test_m4_benchmark_leo_within_15_percent

#: §16 M5 验收判据：几何质量与回归质量偏差 < 20%。
M5_DRY_MASS_TOLERANCE = 0.20

#: STEP 读回对拍门禁（§16.3 的解析-内核 0.1% 同族判据）。
STEP_VOLUME_REL_TOL = 1e-3


# ---------------------------------------------------------------------------
# 门禁 1：几何 vs 回归质量 <20%（逐级；越限级如实登记，不放宽、不消音）
# ---------------------------------------------------------------------------


def _all_template_stages() -> list[tuple[str, int, Stage]]:
    """四模板（含 falcon-heavy 的侧级）全部芯级 + 助推器级的枚举。"""
    items: list[tuple[str, int, Stage]] = []
    for template_id, factory in (
        ("falcon-9", falcon9_vehicle),
        ("cz-5", cz5_vehicle),
        ("saturn-v", saturnv_vehicle),
        ("falcon-heavy", falcon_heavy_vehicle),
    ):
        vehicle = factory()
        for stage in vehicle.stages:
            items.append((template_id, stage.index, stage))
        for position, booster in enumerate(vehicle.boosters):
            items.append((template_id, -(position + 1), booster.stage))  # b<position>
    return items


#: 已登记的越限级（§13.2 口径的失败清单）：M6 前置专项①质量模型细化后 9/11 级
#: 转绿（dev 0.05%–17.4%），余 2 级越限经 **M6 数据专项（2026-09-21）复核**均为
#: **模型颗粒度**而非数据本底——
#: cz-5 芯二级：旧 σ=0.0535（干重 1.3 t 低于两台 YF-75D 自重，物理不可能）已修正为
#: 公开权威口径 4.0 t（σ=0.1481，GCAT stages.tsv 记录 6,700 kg 口径不同——很
#: 可能含级间段，不取）；修正后偏差 72.3% → 44.0%，残差源于几何模型的非贮箱分数
#: 与大膨胀比喷管发动机账偏轻；
#: saturn-v S-IVB：数据本底无误（GCAT 记录 13,300 kg ≈ 公开 13.5 t 含仪器舱，
#: σ=0.1124），偏差 33.4% 源于仪器舱/底推分离装置等支持系统超出「贮箱壁 + 发动机
#: + 分数闭环」的模型颗粒度。
#: ⚠ strict xfail：模型再改进使任一级转绿时，本清单必须同步收缩（否则该参数失败）。
_REGISTERED_OVER_THRESHOLD = frozenset(
    {
        ("cz-5", 2),
        ("saturn-v", 3),
    }
)


@pytest.mark.parametrize(
    ("template_id", "stage_key"),
    [
        pytest.param(
            key,
            key,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "登记（§13.2 口径失败清单，M6 数据专项 2026-09-21 复核后余留）："
                    "越限源于几何模型颗粒度而非数据本底——cz-5 芯二级干重已修为公开 "
                    "~4 t（σ=0.1481，残差 44.0%）；saturn-v S-IVB 本底无误（GCAT 13,300 kg），"
                    "支持系统占比远超模型颗粒度（残差 33.4%）"
                ),
            ),
        )
        for key in sorted(_REGISTERED_OVER_THRESHOLD)
    ],
)
def test_m5_dry_mass_within_20_percent(template_id: str, stage_key: tuple[str, int]) -> None:
    """§16 M5 验收：逐级几何解析干重 vs σ 推算干重偏差 <20%（越限级 xfail 登记）。"""
    template_id_, stage_number = stage_key
    stage = next(
        item
        for tid, number, item in _all_template_stages()
        if tid == template_id_ and number == stage_number
    )
    outcome = cross_check(stage)
    assert outcome.relative_deviation < M5_DRY_MASS_TOLERANCE, (
        f"{template_id_} 级 {stage_number} 几何解析干重 {outcome.m_dry_geometric_kg:.0f} kg "
        f"vs σ 推算干重 {outcome.m_dry_sigma_kg:.0f} kg，偏差 "
        f"{outcome.relative_deviation:.1%} ≥ {M5_DRY_MASS_TOLERANCE:.0%}（§16 M5 门禁）"
    )


def test_m5_dry_mass_failure_list_is_exhaustively_registered() -> None:
    """失败清单机检：当前越限级集合 == 已登记集合——新越限（或转绿）必须显式清账。"""
    current = {
        (template_id, stage_key)
        for template_id, stage_key, stage in _all_template_stages()
        if cross_check(stage).relative_deviation >= M5_DRY_MASS_TOLERANCE
    }
    assert current == _REGISTERED_OVER_THRESHOLD, (
        f"越限清单漂移：新越限 {sorted(current - _REGISTERED_OVER_THRESHOLD)}、"
        f"已转绿 {sorted(_REGISTERED_OVER_THRESHOLD - current)}——"
        "请同步 _REGISTERED_OVER_THRESHOLD（§13.2 口径：如实登记，不消音）"
    )


# ---------------------------------------------------------------------------
# 门禁 2：分区枚举一致性（assembly_tree 节点分区集合 == sections bands 分区集合）
# ---------------------------------------------------------------------------


def _synthetic_two_stage() -> Vehicle:
    return _vehicle((_stage(1, length_m=18.0), _stage(2, length_m=9.0)))


def _axial_sections_by_level(nodes: dict[str, Any]) -> dict[int, set[str]]:
    """装配树节点的**轴向分区**集合（按级序）：尾翼/助推器/喷管为非轴向扩展件，不在域内。"""
    by_level: dict[int, set[str]] = {}
    for node in nodes.values():
        if node.section in ("fin", "booster", "nozzle"):
            continue
        by_level.setdefault(node.stage_index, set()).add(node.section)
    return by_level


@pytest.mark.parametrize(
    "factory",
    [falcon9_vehicle, cz5_vehicle, _synthetic_two_stage],
)
def test_m5_partition_enumeration_consistent_between_tree_and_sections(
    factory: Any,
) -> None:
    """assembly_tree 节点分区集合 == sections bands 分区集合（逐级，无漏件无错序）。"""
    vehicle = factory()
    assembly = build_assembly(vehicle)
    tree_sections = _axial_sections_by_level(assembly.nodes)

    client = TestClient(app)
    response = client.post(
        "/api/geometry/sections", json={"vehicle": vehicle.model_dump(mode="json")}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()

    for entry in body["stages"]:
        level = int(entry["level"])
        band_array = [band["section"] for band in entry["bands"]]
        band_sections = set(band_array)
        tree = {_BAND_SECTION_NAMES.get(s, s) for s in tree_sections.get(level, set())}
        assert tree == band_sections, (
            f"level={level} 分区枚举不一致：树多出 {tree - band_sections}、"
            f"band 多出 {band_sections - tree}（§16 M5：无漏件、无错序）"
        )
        # 错序机检：band 序列（自上而下）必须落在 §5.9 权威次序的相对序上
        order = {
            _BAND_SECTION_NAMES.get(name, name): index for index, name in enumerate(SECTION_ORDER)
        }
        positions = [order[b] for b in band_array]
        assert positions == sorted(positions), (
            f"level={level} band 序列错序：{band_array}（§5.9 权威次序）"
        )


# ---------------------------------------------------------------------------
# 门禁 3：STEP 可被 FreeCAD 打开的机器判据（OCP STEPControl_Reader 读回对拍）
# ---------------------------------------------------------------------------


def test_m5_step_roundtrip_readable_and_volume_matched(tmp_path: Any) -> None:
    """STEP 读回：实体数 > 0 且总体积与内核实体体积 rel < 0.1%（FreeCAD 目视留白）。"""
    from OCP.BRepGProp import BRepGProp  # type: ignore[import-untyped]  # 内核读取仅在测试路径
    from OCP.GProp import GProp_GProps  # type: ignore[import-untyped]
    from OCP.IFSelect import IFSelect_ReturnStatus  # type: ignore[import-untyped]
    from OCP.STEPControl import STEPControl_Reader  # type: ignore[import-untyped]
    from OCP.TopAbs import TopAbs_ShapeEnum  # type: ignore[import-untyped]
    from OCP.TopExp import TopExp_Explorer  # type: ignore[import-untyped]

    vehicle = falcon9_vehicle()
    assembly = build_assembly(vehicle)
    target = tmp_path / "m5.step"
    export_step(assembly.root, target)

    reader = STEPControl_Reader()
    status = reader.ReadFile(str(target))
    assert status == IFSelect_ReturnStatus.IFSelect_RetDone, "STEP 文件解析失败"
    transfer_count = reader.TransferRoots()
    assert transfer_count > 0, "STEP 无可传输根（FreeCAD 将打开空文档）"
    shape = reader.OneShape()

    # 实体数 > 0（SOLID 遍历）
    explorer = TopExp_Explorer(shape, TopAbs_ShapeEnum.TopAbs_SOLID)
    solid_count = 0
    while explorer.More():
        solid_count += 1
        explorer.Next()
    assert solid_count > 0, "STEP 读回后不含任何实体"

    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    # OCCT 内部单位是 mm（reader 把文件声明的 M 换算进来）：体积为 mm³，÷1e9 回 m³
    step_volume_m3 = props.Mass() / 1e9
    assert step_volume_m3 > 0.0
    rel = abs(step_volume_m3 - assembly.volume) / assembly.volume
    assert rel < STEP_VOLUME_REL_TOL, (
        f"STEP 读回体积 {step_volume_m3:.6f} m³ vs 内核 {assembly.volume:.6f} m³，"
        f"rel={rel:.3e} ≥ {STEP_VOLUME_REL_TOL:g}（§16 M5 机器判据）"
    )


# ---------------------------------------------------------------------------
# 门禁 4：回收质量代价可解释（三项分解齐全且加总 = 总代价）
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _recovered_f9() -> Vehicle:
    from aeroforge.params.schema import Recovery

    return falcon9_vehicle().model_copy(
        update={
            "recovery": Recovery(
                enabled=True,
                stage_indices=(1,),
                method="propulsive",
                landing_propellant_margin_fraction=0.10,
                system_mass_kg=2_000.0,
                reinforcement_mass_kg=1_500.0,
            ),
        }
    )


def test_m5_recovery_costs_itemized_and_summing() -> None:
    """§8.9 规则 3：三项独立代价（系统 / 增强结构 / 着陆预留）齐全且加总 = 总代价。"""
    from aeroforge.perf.capacity import anchored_dv_km_s, vehicle_ledger
    from aeroforge.perf.losses import DEFAULT_LAUNCH_SITE
    from aeroforge.perf.sequence import apply_sequence
    from aeroforge.perf.solver import solve

    vehicle = _recovered_f9()
    dv_km_s, _source, _w = anchored_dv_km_s(
        vehicle, vehicle_ledger(vehicle), "LEO", DEFAULT_LAUNCH_SITE
    )
    # 与 _sequence_report 同口径：anchored_dv_km_s 返回 km/s，solve 吃 m/s
    sizing = solve(vehicle, dv_km_s * 1000.0)
    report = apply_sequence(vehicle, sizing)
    costs = report.recovery
    assert costs is not None, "回收已启用：三项代价必须下发"
    assert costs.system_mass_kg > 0.0 and costs.reinforcement_mass_kg > 0.0
    assert costs.landing_propellant_kg > 0.0
    assert costs.inert_cost_kg == pytest.approx(
        costs.system_mass_kg + costs.reinforcement_mass_kg, rel=1e-12
    )
    total_cost = costs.inert_cost_kg + costs.landing_propellant_kg
    assert total_cost == pytest.approx(
        costs.system_mass_kg + costs.reinforcement_mass_kg + costs.landing_propellant_kg,
        rel=1e-12,
    )
    assert report.capacity_penalty_kg > 0.0, "回收点火的运力代价必须 > 0（§8.9：直接减少运力）"
    assert report.payload_capacity_expendable_kg > report.payload_capacity_recoverable_kg


# ---------------------------------------------------------------------------
# 门禁 5：三基准复绿（直接复跑既有 test_m4_benchmarks 断言）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template_id", "factory"),
    [
        ("falcon-9", falcon9_vehicle),
        ("cz-5", cz5_vehicle),
        ("saturn-v", saturnv_vehicle),
    ],
)
def test_m5_three_benchmarks_stay_green(template_id: str, factory: Any) -> None:
    """三基准 LEO 运力误差 <15%（M4 门禁复跑——级间段切出后模板重标不得使其漂移）。"""
    test_m4_benchmark_leo_within_15_percent(template_id, factory)


# ---------------------------------------------------------------------------
# 分离时序几何联动（交付 3）：surviving_nodes 单调缩减、末态 = 末级节点集
# ---------------------------------------------------------------------------


def _sequence_report(vehicle: Vehicle) -> Any:
    from aeroforge.perf.capacity import anchored_dv_km_s, vehicle_ledger
    from aeroforge.perf.losses import DEFAULT_LAUNCH_SITE
    from aeroforge.perf.sequence import apply_sequence
    from aeroforge.perf.solver import solve

    dv_km_s, _source, _w = anchored_dv_km_s(
        vehicle, vehicle_ledger(vehicle), "LEO", DEFAULT_LAUNCH_SITE
    )
    # anchored_dv_km_s 的口径是 km/s，solve 吃 m/s——单位换算在此显式发生
    # （修正既有缺陷：裸传 km/s 值在纯串联构型下会退化为 ~10 m/s 的退化定尺）
    sizing = solve(vehicle, dv_km_s * 1000.0)
    return apply_sequence(vehicle, sizing)


def test_m5_surviving_nodes_shrink_monotonically() -> None:
    """F9 全时序：surviving_nodes 单调缩减；抛罩删 fairing、分离删该级节点组。"""
    report = _sequence_report(falcon9_vehicle())
    node_index = vehicle_node_index(falcon9_vehicle())
    assert set(report.events[0].surviving_nodes) == set(node_index), "点火时全部节点存留"

    previous: set[str] | None = None
    for event in report.events:
        surviving = set(event.surviving_nodes)
        if previous is not None:
            assert surviving <= previous, (
                f"事件 {event.event} 后存留节点集合必须单调缩减（新增了 {surviving - previous}）"
            )
        previous = surviving

    by_event = {event.event + str(event.stage_index or ""): event for event in report.events}
    assert by_event  # 时序事件按 (事件类型, 级号) 可寻址
    jettison = next(e for e in report.events if e.event == "fairing_jettison")
    assert "s2-fairing" not in jettison.surviving_nodes
    assert "s2-fairing" in report.events[0].surviving_nodes
    separation = next(e for e in report.events if e.event == "stage_separation")
    assert not any(name.startswith("s1-") for name in separation.surviving_nodes), (
        "一级分离后其节点组必须离场"
    )
    orbit = next(e for e in report.events if e.event == "orbit_insertion")
    final = set(orbit.surviving_nodes)
    expected_final = {
        name
        for name, (stage_of, section) in node_index.items()
        if stage_of == 2 and section != "fairing"
    }
    assert final == expected_final, "末态 = 末级节点集（减已抛罩）"


def test_m5_surviving_nodes_boosters_leave_with_first_stage() -> None:
    """助推器节点随芯一级分离（分离时刻默认 = 芯一级关机，§6.1 Booster 层）。

    走真实锚定 ΔV 链（与 :func:`_sequence_report` 同通路）：M6 前置专项②
    修复求解器外层迭代稳健性后，M5 收官片「显式 ΔV 8 000 m/s 绕行」解除。
    """
    vehicle = _vehicle((_stage(1, length_m=24.0), _stage(2, length_m=9.0))).model_copy(
        update={"boosters": [Booster(stage=_stage(1, length_m=12.0), count=2)]}
    )
    report = _sequence_report(vehicle)
    ignition = report.events[0]
    assert any(name.startswith("booster-") for name in ignition.surviving_nodes), (
        "点火时助推器节点在栈上"
    )
    separation = next(e for e in report.events if e.event == "stage_separation")
    assert not any(name.startswith("booster-") for name in separation.surviving_nodes), (
        "芯一级分离后助推器节点必须离场"
    )
    assert not any(name.startswith("s1-") for name in separation.surviving_nodes)


def test_m5_sections_carries_interstage_band_and_label(client: TestClient) -> None:
    """sections 下发：显式级间段 → `interstage` band（§5.9 共性 2 / §11.10 枚举）+ 尺寸标注。"""
    vehicle = _vehicle(
        (_stage(1, length_m=24.0), _stage(2, length_m=19.2, interstage_height_m=6.6))
    )
    response = client.post(
        "/api/geometry/sections", json={"vehicle": vehicle.model_dump(mode="json")}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    second = body["stages"][1]
    band_names = [band["section"] for band in second["bands"]]
    assert "interstage" in band_names, "二级的级间段 band 必须下发"
    interstage = next(b for b in second["bands"] if b["section"] == "interstage")
    assert interstage["length_m"] == pytest.approx(6.6, rel=1e-9)
    labels = {label["key"]: label["text"] for label in body["dimensions"]["labels"]}
    assert "s2_interstage_height" in labels, "§5.9 共性 7：级间段高必须进入尺寸标注清单"
    assert "interstage" not in [b["section"] for b in body["stages"][0]["bands"]], (
        "一级未声明级间段：不切出（现状字节不变）"
    )


def test_m5_constraints_booster_layout_checks() -> None:
    """约束引擎：径向偏移干涉（硬）/ 角位个数不匹配（硬）/ 间隙过密（warning）。"""
    from aeroforge.params.constraints import check_vehicle

    base = _vehicle((_stage(1, length_m=24.0), _stage(2, length_m=9.0)))
    booster = Booster(stage=_stage(1, length_m=12.0), count=2)

    # 硬：径向偏移使助推器与芯级干涉（芯级 3.7/2 + 助推器 3.7/2 = 3.7 > 3.0）
    interfering = base.model_copy(
        update={
            "boosters": [
                booster.model_copy(update={"radial_offset_m": 3.0, "angles_deg": (0.0, 180.0)})
            ]
        }
    )
    codes = [item.code for item in check_vehicle(interfering)]
    assert "HARD_BOOSTER_INVALID" in codes

    # 硬：角位个数 ≠ count
    mismatched = base.model_copy(
        update={
            "boosters": [Booster(stage=_stage(1, length_m=12.0), count=3, angles_deg=(0.0, 120.0))]
        }
    )
    assert any(
        item.code == "HARD_BOOSTER_INVALID" and "angles_deg" in item.field_path
        for item in check_vehicle(mismatched)
    )

    # warning：角位过密（表面间隙 < 0.05 m；径向偏移 4.3 ≥ 芯级 2.3 + 助推器 1.85 不干涉）
    crowded = base.model_copy(
        update={
            "boosters": [
                booster.model_copy(update={"radial_offset_m": 4.3, "angles_deg": (0.0, 8.0)})
            ]
        }
    )
    items = check_vehicle(crowded)
    assert any(item.code == "ENGINEER_BOOSTER_CLEARANCE" for item in items), (
        "相邻助推器间隙 < 0.05 m 必须 warning（工程惯例最小间隙）"
    )

    # 缺省布局：无新诊断（canonical 字节稳定）
    assert not [
        item
        for item in check_vehicle(base.model_copy(update={"boosters": [booster]}))
        if item.code in ("HARD_BOOSTER_INVALID", "ENGINEER_BOOSTER_CLEARANCE")
    ]


# ---------------------------------------------------------------------------
# 模板 m_prop ±0.05% 复跑（级间段切出 + fill 重标后的标定门禁）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template_id", "factory"),
    [
        ("falcon-9", falcon9_vehicle),
        ("cz-5", cz5_vehicle),
        ("saturn-v", saturnv_vehicle),
        ("falcon-heavy", falcon_heavy_vehicle),
    ],
)
def test_m5_template_propellant_mass_hits_public_values(template_id: str, factory: Any) -> None:
    """逐级几何解析推进剂质量命中公开分项 ±0.05%（2026-09-20/21 模板重标门禁）。"""
    from aeroforge.perf.mass import propellant_mass_kg

    public_stage_prop: dict[str, tuple[float, ...]] = {
        "falcon-9": (411_000.0, 107_500.0),
        "cz-5": (158_000.0, 23_000.0),  # 芯一 + 芯二；助推器走 boosters 账
        "saturn-v": (2_149_500.0, 443_000.0, 106_600.0),
        "falcon-heavy": (411_000.0, 107_500.0),
    }
    vehicle = factory()
    expected = public_stage_prop[template_id]
    for stage, public_kg in zip(vehicle.stages, expected, strict=True):
        m_prop = propellant_mass_kg(stage)
        deviation = abs(m_prop - public_kg) / public_kg
        assert deviation < 5e-4, (
            f"{template_id} 级 {stage.index} m_prop {m_prop:.0f} kg vs 公开 {public_kg:.0f} kg"
            f"（偏差 {deviation:.3%} ≥ 0.05%）——模板标定漂移"
        )
