"""九段分区装配树（§5.9 / §5.5 / OI-33 / OI-37）+ 共底 + 扁度 + 尾翼 + 车辆形态 API。

覆盖口径（任务交付 2/3/4/6）：
- 节点名稳定枚举（falcon-9 的树样例）、退化分区无节点（0 高 avionics、无整流罩）；
- ``assembly_tree`` 下发形状（级序 / 分区 / z / 质量贡献 / 材料 / 参数来源）；
- 质量贡献加总 ≈ 级质量（§8.4 几何解析账对拍，M5 第二片同源后 rel 5%）；
- 储箱排列翻转（fuel_upper ⇒ 燃料箱在上——读字段不硬编码）；
- 共底：四校验 + 容积守恒 0.5% + 矢高超限报错 + saving 下发 + LH₂ 缺隔热警告；
- 扁度：级层 flatness 生效（封头矢高手算对拍）+ None 兜底 0.5；
- 尾翼：数量域校验、周向均布角度、体积 > 0 进 metrics；
- GLB 场景图契约（根不持 mesh、分区节点各持 mesh、booster 纳编）；
- 车辆形态 API：作业链 → metrics（assembly_tree / common_bulkhead_saving_m /
  fins / boosters）→ 二次构建缓存命中。
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from typing import Any, Literal, cast

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.geometry.assembly import (
    AssemblyError,
    build_assembly,
    plan_stage,
)
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import (
    Aero,
    Booster,
    Engine,
    Geometry,
    LaunchSite,
    Mission,
    Stage,
    Tank,
    Vehicle,
)
from aeroforge.params.templates import falcon9_vehicle
from aeroforge.perf.mass import dome_height_m, dry_mass_geometric_kg, tank_dry_masses_kg

_JOB_TIMEOUT_S = 60.0


def _engine() -> Engine:
    return Engine(
        model="test-engine",
        cycle="gas_generator",
        chamber_pressure_pa=9.7e6,
        expansion_ratio=16.0,
        efficiency_factor=0.98,
        thrust_sea_level_n=845_000.0,
        thrust_vacuum_n=981_000.0,
        isp_sea_level_s=282.0,
        isp_vacuum_s=311.0,
        mixture_ratio=2.36,
    )


def _tank(tank_type: Literal["separate", "common_bulkhead"] = "separate") -> Tank:
    return Tank(
        tank_type=tank_type,
        wall_thickness_m=0.005,
        material="al-2219",
        fill_fraction=0.95,
        feed_system="pump_fed",
    )


def _stage(
    index: int = 1,
    *,
    length_m: float = 41.2,
    flatness_ratio: float | None = None,
    common_bulkhead: bool = False,
    tank_arrangement: str = "oxidizer_upper",
    fins: dict[str, Any] | None = None,
    propellant: str = "LOX/RP-1",
    avionics_height_m: float | None = None,
    intertank_height_m: float | None = None,
    interstage_height_m: float | None = None,
) -> Stage:
    geometry_kwargs: dict[str, Any] = {
        "common_bulkhead": common_bulkhead,
        "tank_arrangement": tank_arrangement,
        "oxidizer_tank": _tank("common_bulkhead" if common_bulkhead else "separate"),
        "fuel_tank": _tank("common_bulkhead" if common_bulkhead else "separate"),
    }
    if common_bulkhead:
        geometry_kwargs["common_bulkhead_type"] = "insulated_sandwich"
    if fins:
        geometry_kwargs["fins_enabled"] = True
        geometry_kwargs.update(fins)
    return Stage(
        index=index,
        propellant=propellant,  # type: ignore[arg-type]
        diameter_m=3.7,
        length_m=length_m,
        wall_thickness_m=0.005,
        material="al-2219",
        structure_coefficient=0.05,
        fill_fraction=0.95,
        engine_count=9,
        engine=_engine(),
        engine_height_m=2.9,
        interstage_type="none",
        isp_source="default",
        flatness_ratio=flatness_ratio,
        avionics_height_m=avionics_height_m,
        intertank_height_m=intertank_height_m,
        interstage_height_m=interstage_height_m,
        geometry=Geometry(**geometry_kwargs),
    )


def _vehicle(stages: tuple[Stage, ...], *, fairing_diameter_m: float | None = 4.6) -> Vehicle:
    """合法车辆（过产品校验器——夹具纪律，教训 D 类）。"""
    vehicle = Vehicle(
        name="装配测试箭",
        stages=stages,
        payload_mass_kg=13_000.0,
        material="al-2219",
        propellant="LOX/RP-1",
        aero=Aero(drag_coefficient=0.3),
        fairing_diameter_m=fairing_diameter_m,
        mission=Mission(
            orbit_type="LEO",
            altitude_m=200_000.0,
            inclination_deg=28.5,
            launch_site=LaunchSite(
                name="Cape Canaveral", latitude_deg=28.5, altitude_m=3.0, azimuth_deg=90.0
            ),
        ),
    )
    violations = check_vehicle(vehicle)
    assert not has_hard(violations), f"夹具违反硬约束：{violations}"
    return vehicle


# ---------------------------------------------------------------------------
# 节点枚举与场景图契约
# ---------------------------------------------------------------------------


def test_falcon9_tree_node_names_are_stable() -> None:
    """falcon-9 的装配树：两级 × 七分区 + 顶级 adapter/fairing + 喷管 + 级间段。

    M5 第四片口径：二级级间段 6.6 m 切出（``s2-interstage``，发动机舱段被级间段
    包容——``s2-engine-bay`` 消失）；喷管钟形自推进参数派生（一级 9 管
    ``s1-nozzle-<k>`` 周向布置、二级单管 ``s2-nozzle``）。
    """
    assembly = build_assembly(falcon9_vehicle())
    expected = {
        "s1-engine-bay",
        "s1-thrust-structure",
        "s1-fuel-tank",
        "s1-intertank",
        "s1-ox-tank",
        "s1-forward-skirt",
        "s2-interstage",
        "s2-thrust-structure",
        "s2-fuel-tank",
        "s2-intertank",
        "s2-ox-tank",
        "s2-forward-skirt",
        "s2-adapter",
        "s2-fairing",
        *(f"s1-nozzle-{k}" for k in range(9)),
        "s2-nozzle",
    }
    assert set(assembly.nodes) == expected, (
        f"falcon-9 树与期望不符：多出 {set(assembly.nodes) - expected}，"
        f"缺少 {expected - set(assembly.nodes)}"
    )
    # §5.9 权威次序（自上而下）：fairing → adapter → … → engine_bay
    top = max(assembly.nodes.values(), key=lambda node: node.z_start_m)
    assert top.section == "fairing"
    assert assembly.nodes["s2-fairing"].z_start_m > assembly.nodes["s2-adapter"].z_start_m
    assert assembly.nodes["s2-adapter"].z_start_m > assembly.nodes["s2-forward-skirt"].z_start_m
    # 氧箱在上（F9 默认排列）：s?-ox-tank 的 z 大于同级 fuel-tank
    assert assembly.nodes["s1-ox-tank"].z_start_m > assembly.nodes["s1-fuel-tank"].z_start_m
    # 级间段位于二级布局最底部（顶接 s2 发动机占位下缘、底接 s1 前裙上缘）
    interstage = assembly.nodes["s2-interstage"]
    assert interstage.length_m == pytest.approx(6.6, rel=1e-12)
    assert interstage.section == "interstage"
    assert interstage.z_start_m == pytest.approx(42.6, rel=1e-9)
    assert "s2-engine-bay" not in assembly.nodes, "级间段包容发动机时发动机舱段为 0 高"
    # 喷管节点：metadata 带派生来源；一级 9 管周向、二级单管轴心
    nozzle = assembly.nodes["s2-nozzle"]
    assert nozzle.section == "nozzle"
    assert any("thrust_vacuum_n" in f for f in nozzle.source_fields)
    assert "C_F=1.65" in (nozzle.note or "")
    s1_nozzle0 = next(child for child in assembly.root.children if child.label == "s1-nozzle-0")
    assert s1_nozzle0.center().X > 0.0, "多管发动机周向布置（首管自 +X 起）"


def test_no_fairing_yields_no_fairing_nodes() -> None:
    """退化分区（该级无此部件）不产出节点：无整流罩 ⇒ 无 fairing/adapter。"""
    assembly = build_assembly(_vehicle((_stage(1),), fairing_diameter_m=None))
    assert "s1-fairing" not in assembly.nodes
    assert "s1-adapter" not in assembly.nodes
    assert "s1-avionics" not in assembly.nodes, "0 高分区（§5.9 允许）不应产出节点"


def test_glb_scene_graph_contract(tmp_path: object) -> None:
    """OI-33 契约的 M5 形态：根 vehicle 不持 mesh，分区节点各持一个 mesh。"""
    from pathlib import Path

    from aeroforge.geometry.revolve import export_glb, read_glb_json

    assembly = build_assembly(_vehicle((_stage(1),)))
    target = Path(str(tmp_path)) / "assembly.glb"
    export_glb(assembly.root, target, deflection=0.01, angular=0.5)
    gltf = read_glb_json(target)
    names = {str(node.get("name", "")): node for node in gltf.get("nodes", [])}
    assert "vehicle" in names
    assert names["vehicle"].get("mesh") is None, "根节点不得持 mesh（叠加渲染缺陷）"
    for label in assembly.nodes:
        assert label in names, f"GLB 缺节点 {label}（前端显隐将静默失效）"
        assert names[label].get("mesh") is not None, f"节点 {label} 缺 mesh"


# ---------------------------------------------------------------------------
# assembly_tree 元数据与质量账
# ---------------------------------------------------------------------------


def test_assembly_tree_metadata_and_mass_account() -> None:
    """assembly_tree 形状 + 质量贡献加总 ≈ 级质量（§8.4 对拍，M5 第二片同源后 rel 5%）。"""
    stage = _stage(1)
    vehicle = _vehicle((stage,), fairing_diameter_m=None)
    assembly = build_assembly(vehicle)
    for node in assembly.nodes.values():
        assert node.stage_index == 1
        assert node.length_m > 0.0
        assert node.material == "al-2219"
        assert node.source_fields, "每个部件必须携带参数来源（§5.5）"
        assert node.mass_kg >= 0.0
        assert node.z_start_m >= 0.0
    # 质量账：M5 第二片 reserved 口径差复核裁定——两账消费同一份 §5.9 分区高度
    # （含共底隔板干重），同源后应精确闭合（门禁放宽到 5% 防实现漂移）
    section_mass = sum(node.mass_kg for node in assembly.nodes.values())
    account = dry_mass_geometric_kg(stage)
    assert section_mass == pytest.approx(account, rel=0.05), (
        f"质量贡献加总 {section_mass:.3f} kg vs §8.4 账 {account:.3f} kg 偏差超 5%"
    )
    # 账目完整性：两箱质量按 §8.4 同式分列（bookkeeping 不重不漏）
    layout = plan_stage(stage)
    ox_mass, fuel_mass = tank_dry_masses_kg(stage, reserved_m=layout.reserved_m)
    assert assembly.nodes["s1-ox-tank"].mass_kg == pytest.approx(ox_mass, rel=1e-12)
    assert assembly.nodes["s1-fuel-tank"].mass_kg == pytest.approx(fuel_mass, rel=1e-12)


# ---------------------------------------------------------------------------
# 三 Schema 增补（M5 第二片留白清偿）：显式值生效 + None 现状不变
# ---------------------------------------------------------------------------


def test_explicit_avionics_height_adds_top_band_and_node() -> None:
    """显式仪器舱高：plan_stage 产带（级顶）+ build_assembly 产节点 s1-avionics。"""
    stage = _stage(1, avionics_height_m=1.2)
    layout = plan_stage(stage)
    avionics = [band for band in layout.bands if band.section == "avionics"]
    assert len(avionics) == 1
    assert avionics[0].length == pytest.approx(1.2, rel=1e-12)
    assert avionics[0].z_end == pytest.approx(layout.height, rel=1e-12), (
        "仪器舱在级顶（§5.9 第 3 段）"
    )
    # 分区仍恰好铺满级长：仪器舱占高由箱段让出
    dome = dome_height_m(3.7, None)
    expected_tanks = stage.length_m - stage.engine_height_m - (2 * dome + 2 * dome) - 1.2
    assert layout.l_ox + layout.l_fuel == pytest.approx(expected_tanks, rel=1e-9)
    assembly = build_assembly(_vehicle((stage,), fairing_diameter_m=None))
    assert "s1-avionics" in assembly.nodes
    assert assembly.nodes["s1-avionics"].length_m == pytest.approx(1.2, rel=1e-12)


def test_explicit_intertank_height_overrides_derivation() -> None:
    """显式级间舱高：第 6 分区取显式值（用户权威），箱段相应缩短；None 保持派生。"""
    explicit = plan_stage(_stage(1, intertank_height_m=2.5))
    derived = plan_stage(_stage(1))
    assert explicit.bands[3].section == "intertank"
    assert explicit.bands[3].length == pytest.approx(2.5, rel=1e-12)
    assert derived.bands[3].length == pytest.approx(2 * dome_height_m(3.7, None), rel=1e-12)
    assert explicit.l_ox + explicit.l_fuel == pytest.approx(
        derived.l_ox + derived.l_fuel - (2.5 - derived.h_mid), rel=1e-9
    ), "显式级间舱加高由两箱柱段让出"
    assert explicit.height == pytest.approx(_stage(1).length_m, rel=1e-12)


def test_explicit_intertank_below_dome_sum_raises() -> None:
    """显式级间舱高小于两箱相邻封头矢高和 ⇒ 封头干涉，AssemblyError（§5.5 校验 3 同判据）。"""
    with pytest.raises(AssemblyError, match=r"级间舱高.*小于两箱相邻封头矢高和"):
        plan_stage(_stage(1, intertank_height_m=0.5))


# ---------------------------------------------------------------------------
# 级间段（§5.9 共性 2，M5 第四片）：Schema + 切出 + canonical 纪律
# ---------------------------------------------------------------------------


def test_interstage_band_cut_at_stage_bottom() -> None:
    """显式级间段：band 位于本级布局最底部（顶接发动机舱段下缘、底接下级前裙）。"""
    stage = _stage(2, length_m=19.2, interstage_height_m=6.6)
    layout = plan_stage(stage)
    interstage = [band for band in layout.bands if band.section == "interstage"]
    assert len(interstage) == 1
    assert interstage[0].length == pytest.approx(6.6, rel=1e-12)
    assert interstage[0].z_start == pytest.approx(0.0, abs=1e-12), "级间段在本级最底部"
    # 各分区高度和仍 = length_m（从 19.2 内划出，非加高）
    assert sum(band.length for band in layout.bands) == pytest.approx(stage.length_m, rel=1e-9)
    # 发动机舱段被级间段包容（4.5 < 6.6 → engine_bay 0 高不产带）
    assert all(band.section != "engine_bay" for band in layout.bands)


def test_interstage_partially_houses_engine() -> None:
    """级间段小于发动机高：发动机舱段保留余量，贮箱只让出净差值。"""
    stage = _stage(1, length_m=20.0, interstage_height_m=1.0)
    layout = plan_stage(stage)
    engine_bay = [band for band in layout.bands if band.section == "engine_bay"]
    assert len(engine_bay) == 1
    assert engine_bay[0].length == pytest.approx(stage.engine_height_m - 1.0, rel=1e-12)
    assert sum(band.length for band in layout.bands) == pytest.approx(stage.length_m, rel=1e-9)


def test_interstage_none_keeps_status_quo() -> None:
    """None = 不切出（现状）：无级间段 band，分区与既有口径一致。"""
    stage = _stage(1)
    layout = plan_stage(stage)
    assert all(band.section != "interstage" for band in layout.bands)
    engine_bay = [band for band in layout.bands if band.section == "engine_bay"]
    assert engine_bay[0].length == pytest.approx(stage.engine_height_m, rel=1e-12)


def test_interstage_none_canonical_bytes_unchanged() -> None:
    """§9.2 canonical 纪律：缺省 interstage_height_m 不进既有输入的字节。"""
    stage = _stage(1)
    payload = stage.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    assert "interstage_height_m" not in payload
    explicit = _stage(1, interstage_height_m=6.6).model_dump(
        mode="json", exclude_none=True, exclude_defaults=True
    )
    assert "interstage_height_m" in explicit


def test_interstage_node_in_assembly_tree() -> None:
    """装配树：显式级间段产出独立节点 s<级>-interstage（发动机舱段被包容）。"""
    stage = _stage(2, length_m=19.2, interstage_height_m=6.6)
    assembly = build_assembly(_vehicle((_stage(1), stage), fairing_diameter_m=None))
    assert "s2-interstage" in assembly.nodes
    node = assembly.nodes["s2-interstage"]
    assert node.stage_index == 2
    assert node.length_m == pytest.approx(6.6, rel=1e-12)
    assert "s2-engine-bay" not in assembly.nodes
    # 纯数值枚举与内核路径一致（意图断言在 build_assembly 内部已强制，这里再显式核对）
    from aeroforge.geometry.assembly import vehicle_node_index

    assert "s2-interstage" in vehicle_node_index(
        _vehicle((_stage(1), stage), fairing_diameter_m=None)
    )


def test_explicit_fairing_height_overrides_convention_constant() -> None:
    """整流罩高：Vehicle.fairing_height_m 显式值优先；None 保持惯例常量（现状值）。"""
    default = build_assembly(_vehicle((_stage(1),)))
    explicit = build_assembly(_vehicle((_stage(1),)).model_copy(update={"fairing_height_m": 8.0}))
    assert default.nodes["s1-fairing"].length_m == pytest.approx(
        min(max(2.2 * 4.6, 5.0), 20.0), rel=1e-12
    )
    assert explicit.nodes["s1-fairing"].length_m == pytest.approx(8.0, rel=1e-12)


def test_tank_arrangement_flip_reads_field() -> None:
    """储箱排列翻转（§5.9 非铁律）：fuel_upper ⇒ 燃料箱在上（z 更大）。"""
    flipped = build_assembly(
        _vehicle((_stage(1, tank_arrangement="fuel_upper"),), fairing_diameter_m=None)
    )
    normal = build_assembly(
        _vehicle((_stage(1, tank_arrangement="oxidizer_upper"),), fairing_diameter_m=None)
    )
    assert flipped.nodes["s1-fuel-tank"].z_start_m > flipped.nodes["s1-ox-tank"].z_start_m, (
        "fuel_upper 未生效：燃料箱应在上"
    )
    assert normal.nodes["s1-ox-tank"].z_start_m > normal.nodes["s1-fuel-tank"].z_start_m, (
        "oxidizer_upper 未生效：氧箱应在上"
    )
    # 分区**集合**不变（§5.9 的 9 段固定），只有 5/7 段的上下次序对调
    assert set(flipped.nodes) == set(normal.nodes)


# ---------------------------------------------------------------------------
# 共底（§5.5 四校验 + 扁度生效）
# ---------------------------------------------------------------------------


def _common_bulkhead_vehicle(**stage_kwargs: Any) -> Vehicle:
    return _vehicle((_stage(1, common_bulkhead=True, **stage_kwargs),), fairing_diameter_m=None)


def test_common_bulkhead_four_checks_and_saving() -> None:
    assembly = build_assembly(_common_bulkhead_vehicle(flatness_ratio=0.6))
    # M5 第四片：隔板从内嵌曲面升级为独立薄壳节点（名与 sections 的 common_bulkhead 对齐）
    assert "s1-common-bulkhead" in assembly.nodes
    assert "s1-intertank" not in assembly.nodes, "共底开启时第 6 分区应为隔板段而非级间舱"
    bulkhead_solid = next(
        child for child in assembly.root.children if child.label == "s1-common-bulkhead"
    )
    # 薄壳体积远小于实心穹顶（壳厚 = 两侧壁厚和的工程近似）
    full_dome = 2.0 / 3.0 * math.pi * (3.7 / 2.0) ** 2 * (0.6 * 3.7 / 2.0)
    assert 0.0 < bulkhead_solid.volume < full_dome * 0.2
    by_name = {check.check: check for check in assembly.checks}
    g1 = by_name["共底校验 1：隔板-柱段 G1"]
    assert g1.severity == "pass" and g1.value == pytest.approx(0.0, abs=1e-9)
    conservation = by_name["共底校验 2：容积守恒"]
    assert conservation.severity == "pass"
    assert conservation.relative_error is not None
    assert conservation.relative_error <= 5e-3, "容积守恒超 0.5% 门禁"
    height_check = by_name["共底校验 3：矢高 ≤ 允许值"]
    assert height_check.severity == "pass"
    # saving 公式反算：两只相邻封头矢高和 − 隔板矢高（等径时 = f·D/2）
    assert assembly.saving_by_stage == {1: pytest.approx(0.6 * 3.7 / 2.0, rel=1e-12)}


def test_common_bulkhead_conservation_is_kernel_vs_analytic() -> None:
    """守恒检查的实质：内核实测隔板体积与解析穹顶公式对拍（§5.7 形态）。"""
    assembly = build_assembly(_common_bulkhead_vehicle(flatness_ratio=0.5))
    conservation = next(check for check in assembly.checks if check.check == "共底校验 2：容积守恒")
    assert "内核实测" in conservation.detail or "隔板占体" in conservation.detail
    # 内核实测隔板体积 = (2/3)πR²h_b（椭球穹顶闭式解）
    h_b = 0.5 * 3.7 / 2.0
    expected_cap = 2.0 / 3.0 * math.pi * (3.7 / 2.0) ** 2 * h_b
    assert conservation.value is not None and conservation.expected is not None
    assert conservation.value == pytest.approx(conservation.expected, rel=5e-3)
    assert expected_cap > 0.0


def test_bulkhead_sagitta_over_limit_raises() -> None:
    """矢高超限（§5.5 校验 3）：短级 + 大扁度 ⇒ 隔板与上下箱干涉，生成期报错。"""
    with pytest.raises(AssemblyError, match=r"矢高.*超过允许值"):
        plan_stage(_stage(1, length_m=7.5, flatness_ratio=0.9, common_bulkhead=True))


def test_common_bulkhead_lh2_insulation_warning() -> None:
    """共底且 LH₂ 侧缺隔热层：装配层 warning 留痕（参数层另有 HARD 裁定）。

    ⚠ 该夹具**刻意**绕过 `_vehicle` 的无硬违反断言：LOX/LH₂ + 共底 + 无隔热层
    在参数层就是 HARD_BULKHEAD_INSULATION_MISSING（既有裁定），本测试验证的是
    装配层的 warning 双保险——两条通路对同一缺陷都要留痕。
    """
    stage = _stage(1, common_bulkhead=True, propellant="LOX/LH2")
    vehicle = Vehicle(
        name="LH2 共底测试箭",
        stages=(stage,),
        payload_mass_kg=8_000.0,
        material="al-2219",
        propellant="LOX/LH2",
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
    violations = check_vehicle(vehicle)
    assert any(item.code == "HARD_BULKHEAD_INSULATION_MISSING" for item in violations)
    assembly = build_assembly(vehicle)
    insulation = next(
        check for check in assembly.checks if check.check == "共底校验 4：LH₂ 侧隔热层"
    )
    assert insulation.severity == "warn"
    assert "液氢" in insulation.detail


def test_flatness_priority_and_hand_computed_dome_height() -> None:
    """扁度三级优先级（OI-37）+ 封头矢高手算对拍。

    级层 flatness（None 兜底 0.5）直接决定前裙高（= 上箱顶封头矢高）：
    h = f × D / 2——0.5 ⇒ 0.925 m、0.6 ⇒ 1.11 m。显式剖面段的优先级见
    meridian 通路（ellipse 段的 length 即显式矢高，天然最优先）。
    """
    default = build_assembly(_vehicle((_stage(1),), fairing_diameter_m=None))
    explicit = build_assembly(_vehicle((_stage(1, flatness_ratio=0.6),), fairing_diameter_m=None))
    assert default.nodes["s1-forward-skirt"].length_m == pytest.approx(
        dome_height_m(3.7, None), rel=1e-12
    )
    assert explicit.nodes["s1-forward-skirt"].length_m == pytest.approx(
        dome_height_m(3.7, 0.6), rel=1e-12
    )
    # 手算对拍：f=0.6、D=3.7 ⇒ h = 0.6 × 3.7 / 2 = 1.11 m
    assert explicit.nodes["s1-forward-skirt"].length_m == pytest.approx(1.11, rel=1e-12)
    # 共底矢高同源：h_b = f × D / 2（节点名 M5 第四片对齐 sections：common-bulkhead）
    assembly = build_assembly(_common_bulkhead_vehicle(flatness_ratio=0.6))
    assert assembly.nodes["s1-common-bulkhead"].length_m == pytest.approx(1.11, rel=1e-12)


# ---------------------------------------------------------------------------
# 尾翼（§5.5）
# ---------------------------------------------------------------------------


_FINS: dict[str, Any] = {
    "fin_airfoil": "double_wedge",
    "fin_span_m": 1.8,
    "fin_root_chord_m": 2.4,
    "fin_tip_chord_m": 1.2,
    "fin_sweep_deg": 20.0,
    "fin_count": 4,
    "fin_roll_deg": 45.0,
}


def test_fins_nodes_volumes_and_even_distribution() -> None:
    """尾翼：fin-<k> 节点齐全、体积 > 0、周向均布角度 = roll + 360°k/n。"""
    assembly = build_assembly(_vehicle((_stage(1, fins=_FINS),), fairing_diameter_m=None))
    for index in range(4):
        assert f"fin-{index}" in assembly.nodes
        node = assembly.nodes[f"fin-{index}"]
        assert node.section == "fin"
        assert node.stage_index == 1
    assert assembly.fins is not None
    assert assembly.fins["count"] == 4
    assert assembly.fins["total_volume_m3"] > 0.0
    assert assembly.fins["per_fin_volume_m3"] > 0.0
    # 周向均布的方位角核对：fin-0 在 45°（+X+Y 象限）、fin-1 在 135°（−X+Y），
    # fin-2 在 225°、fin-3 在 315°——用质心落象限验证（roll=45°、步距 90°）
    quadrant_x = (1, -1, -1, 1)
    quadrant_y = (1, 1, -1, -1)
    for index in range(4):
        solid = next(child for child in assembly.root.children if child.label == f"fin-{index}")
        center = solid.center()
        assert center.X * quadrant_x[index] > 0.0, f"fin-{index} 方位角不在期望象限"
        assert center.Y * quadrant_y[index] > 0.0, f"fin-{index} 方位角不在期望象限"
        box = solid.bounding_box()
        assert max(abs(box.min.X), abs(box.max.X)) > 3.7 / 2.0, "尾翼未伸出箭体（展长未生效）"


def test_fin_count_domain() -> None:
    """数量合法域 {0, 3, 4}：2 片被 Schema 校验器拒绝。"""
    with pytest.raises(ValueError, match="合法域"):
        _stage(1, fins={**_FINS, "fin_count": 2})
    # 3 片合法
    stage = _stage(1, fins={**_FINS, "fin_count": 3})
    assembly = build_assembly(_vehicle((stage,), fairing_diameter_m=None))
    assert all(f"fin-{k}" in assembly.nodes for k in range(3))


def test_fin_sweep_domain() -> None:
    with pytest.raises(ValueError, match="sweep"):
        _stage(1, fins={**_FINS, "fin_sweep_deg": 80.0})


def test_fin_params_required_when_enabled() -> None:
    with pytest.raises(ValueError, match="缺失"):
        Geometry(
            fins_enabled=True,
            oxidizer_tank=_tank(),
            fuel_tank=_tank(),
        )


# ---------------------------------------------------------------------------
# 助推器纳编 + 车辆形态 API（作业链 / metrics / 缓存）
# ---------------------------------------------------------------------------


def test_boosters_included_in_assembly() -> None:
    """booster-<k> 纳编（M4 形态沿用）：节点齐全、级号 0、径向在芯级之外。"""
    vehicle = _vehicle((_stage(1),))
    vehicle = vehicle.model_copy(
        update={"boosters": [Booster(stage=_stage(1), count=2, layout="radial_even")]}
    )
    assembly = build_assembly(vehicle)
    for index in range(2):
        assert f"booster-{index}" in assembly.nodes
        node = assembly.nodes[f"booster-{index}"]
        assert node.stage_index == 0, "助推器级号记 0（GCAT 记法）"
        assert node.section == "booster"
    assert assembly.boosters is not None
    assert assembly.boosters["count"] == 2
    assert assembly.boosters["total_booster_volume_m3"] > 0.0
    # 径向在芯级之外（周向均布 + 间隙）
    booster = next(child for child in assembly.root.children if child.label == "booster-0")
    assert booster.bounding_box().max.X > 3.7 / 2.0


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _await_job(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        body: dict[str, object] = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"作业 {job_id} 在 {_JOB_TIMEOUT_S}s 内未达终态")


def test_vehicle_build_api_metrics_and_cache_hit(client: TestClient) -> None:
    """车辆形态构建链：作业 → metrics（assembly_tree / saving / fins / boosters）→ 缓存命中。"""
    vehicle = _vehicle((_stage(1, fins=_FINS),))
    payload = {"vehicle": jsonable(vehicle)}
    first = client.post("/api/geometry/build", json=payload)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["cache_hit"] is False
    assert body["job_id"]

    job = _await_job(client, body["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    metrics = cast(dict[str, Any], job["metrics"])
    assert metrics["form"] == "vehicle"
    tree = metrics["assembly_tree"]
    assert isinstance(tree, dict) and tree
    for node in tree.values():
        assert {
            "stage_index",
            "section",
            "z_start_m",
            "length_m",
            "mass_kg",
            "material",
            "source_fields",
        } <= set(node)
    assert metrics["common_bulkhead_saving_m"] == {}
    assert metrics["fins"]["count"] == 4
    assert metrics["fins"]["total_volume_m3"] > 0.0
    assert metrics["spec_version"] == "0.7.1"

    second = client.post("/api/geometry/build", json=payload)
    assert second.status_code == 200
    assert second.json()["cache_hit"] is True
    assert second.json()["key"] == body["key"]
    assert second.json()["metrics"]["assembly_tree"] == tree


def test_vehicle_build_bulkhead_saving_in_metrics(client: TestClient) -> None:
    vehicle = _common_bulkhead_vehicle(flatness_ratio=0.5)
    payload = {"vehicle": jsonable(vehicle)}
    first = client.post("/api/geometry/build", json=payload).json()
    job = _await_job(client, first["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    metrics = cast(dict[str, Any], job["metrics"])
    assert metrics["common_bulkhead_saving_m"] == {"1": pytest.approx(0.5 * 3.7 / 2.0)}
    checks = metrics["checks"]
    assert any("容积守恒" in str(check["check"]) for check in checks)


def test_profile_form_still_works_alongside(client: TestClient) -> None:
    """剖面形态与车辆形态并存：既有 profile 请求的路径与键不受影响。

    ⚠ 剖面名取唯一值（canonical 含 name）：与 test_api_geometry 的同名夹具
    共享缓存会使首次构建即命中（job_id=None），走不到作业路径。
    """
    profile = {
        "name": "cyl-assembly-coexist",
        "base_radius": 1.0,
        "segments": [{"type": "line", "length": 3.0, "end_radius": 1.0}],
    }
    first = client.post("/api/geometry/build", json=profile).json()
    job = _await_job(client, first["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    second = client.post("/api/geometry/build", json=profile).json()
    assert second["cache_hit"] is True
    assert "assembly_tree" not in second["metrics"]


def jsonable(vehicle: Vehicle) -> dict[str, Any]:
    return vehicle.model_dump(mode="json")
