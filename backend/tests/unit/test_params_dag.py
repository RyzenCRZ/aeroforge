"""依赖 DAG 与派生量（规格 §6.2 / §6.1「关键派生量」/ §5.9 箱体比例）。

对应 M2 验收项：
- 「循环依赖可检出」——本文件不只验"会报错"，还断言报错**带出环路径**；
- 「``V_ox/V_fuel`` 与 §5.9 的两次公开数据反算一致」——见文件末的参数化用例。

⚠ 本层最贵的缺陷形态是"缺输入时填一个看着合理的默认值"（§1.4-4 溯源红线），
故多条用例专门盯 ``deferred`` 账目：推不出来的节点必须**显式留痕**。
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from aeroforge.params import dag, propellants
from aeroforge.params.schema import Vehicle
from tests.conftest import _legal_vehicle, make_stage

#: 与规格「公式结果」列比对的容差。规格那一列的密度取自公开数据的 lb/gal 值
#: （LOX 9.50 / RP-1 6.70 lb/gal → 换算后约 1138 / 803 kg/m³），与内置物性表的标称值
#: （1141 / 810 kg/m³）**不同源**，故留 2% 容差（实测偏差：S-IC 0.7%、S-II < 0.1%）。
_FORMULA_TOL = 0.02
#: 与「实测容积比」列比对的容差。实测列来自公开的加注容积，其自身散布已近 3%
#: （S-II：公式 0.311 对实测 0.319），故给 5%——再收紧就是拿数据噪声当门禁。
_MEASURED_TOL = 0.05


# ---------------------------------------------------------------------------
# 图结构：建图即验环（§6.2）
# ---------------------------------------------------------------------------


def test_duplicate_node_is_rejected() -> None:
    graph = dag.DependencyGraph()
    graph.add(dag.input_node("a"))

    with pytest.raises(dag.DependencyError, match="重名"):
        graph.add(dag.input_node("a"))


def test_unknown_dependency_is_rejected() -> None:
    graph = dag.DependencyGraph()
    graph.add(dag.derived_node("b", ("missing",), lambda values: 0.0))

    with pytest.raises(dag.UnknownDependencyError, match="missing"):
        graph.topological_order()


def test_cycle_is_detected_with_the_cycle_path() -> None:
    """§6.2「有环即拒绝加载」：报错必须带出**闭合的环路径**，否则无从下手。"""
    graph = dag.DependencyGraph()
    graph.add(dag.derived_node("a", ("c",), lambda values: 0.0))
    graph.add(dag.derived_node("b", ("a",), lambda values: 0.0))
    graph.add(dag.derived_node("c", ("b",), lambda values: 0.0))

    with pytest.raises(dag.DependencyCycleError) as excinfo:
        graph.topological_order()

    cycle = excinfo.value.cycle
    assert cycle[0] == cycle[-1], f"环路径必须首尾闭合，实际：{cycle}"
    assert set(cycle) == {"a", "b", "c"}, f"环路径必须覆盖环上全部节点，实际：{cycle}"
    assert "→" in str(excinfo.value)


def test_topological_order_puts_dependencies_first(single_stage_vehicle: Vehicle) -> None:
    graph = dag.build_vehicle_graph(single_stage_vehicle)
    order = graph.topological_order()
    position = {name: index for index, name in enumerate(order)}

    assert len(order) == len(graph.names), "拓扑序必须覆盖全部节点"
    for name in order:
        for dep in graph.node(name).deps:
            assert position[dep] < position[name], f"{name} 的依赖 {dep} 排在其后"


def test_each_node_is_computed_at_most_once() -> None:
    """§6.2：单次传播内每个节点最多计算一次——菱形依赖是这条规则最容易失守的形状。"""
    calls: list[str] = []

    def compute(values: Mapping[str, float]) -> float:
        calls.append("merge")
        return values["left"] + values["right"]

    graph = dag.DependencyGraph()
    graph.add(dag.input_node("source"))
    graph.add(dag.derived_node("left", ("source",), lambda values: values["source"] * 2.0))
    graph.add(dag.derived_node("right", ("source",), lambda values: values["source"] * 3.0))
    graph.add(dag.derived_node("merge", ("left", "right"), compute))

    result = graph.propagate({"source": 1.0})

    assert calls == ["merge"], f"合流节点被重复计算：{calls}"
    assert result.values["merge"] == pytest.approx(5.0)
    assert result.order.index("source") < result.order.index("left")


# ---------------------------------------------------------------------------
# 传播账目：来源标记 / 缺输入 / 覆盖提示
# ---------------------------------------------------------------------------


def test_missing_input_is_deferred_with_the_reason(single_stage_vehicle: Vehicle) -> None:
    """缺输入不得填默认值：必须进 ``deferred`` 并写明缺什么（M2 的链条只走一半）。"""
    result = dag.propagate_vehicle(single_stage_vehicle)

    assert result.deferred["stage1.propellant_mass_kg"].startswith("未提供输入")
    assert result.deferred["stage1.oxidizer_tank_length_m"].startswith("缺少依赖：")
    assert "stage1.oxidizer_volume_m3" in result.deferred["stage1.oxidizer_tank_length_m"]
    # 仅依赖 σ 的派生量不受推进剂质量缺失的影响——这正是 §6.5 结构质量比规则
    # 能在 M2 判定的原因，若它也被连带 deferred，该规则就会静默变成"未判定"。
    assert result.values["stage1.dry_to_prop_ratio"] == pytest.approx(0.05 / 0.95)
    assert result.sources["stage1.dry_to_prop_ratio"] is dag.Source.DERIVED
    assert "stage1.dry_to_prop_ratio" not in result.deferred


def test_deferred_nodes_are_not_in_derived_values(single_stage_vehicle: Vehicle) -> None:
    """``derived_values`` 只含**真正算出**的量，不得把 deferred 的节点混进来。"""
    result = dag.propagate_vehicle(single_stage_vehicle)

    assert "stage1.oxidizer_tank_length_m" not in result.derived_values
    assert set(result.derived_values) & set(result.deferred) == set()
    assert all(result.sources[name] is dag.Source.DERIVED for name in result.derived_values), (
        "derived_values 里出现了非 derived 来源的节点"
    )


def test_omitted_stage_isp_resolves_to_engine_nominal(single_stage_vehicle: Vehicle) -> None:
    """QA-1（v0.6.1 续）：default 语义下级层省略 isp_*，DAG 取**发动机标称值**。

    判据用反证：夹具发动机标称 311.0 s，若解析错了（比如取 0 或缺输入），
    质量流量 = F_vac/(Isp·g₀) 立刻偏离。
    """
    result = dag.propagate_vehicle(single_stage_vehicle)

    assert result.values["stage1.isp_vacuum_s"] == pytest.approx(311.0)
    assert result.sources["stage1.isp_vacuum_s"] is dag.Source.USER

    # §6.1：ṁ = F/(Isp·g₀)（按**单机**真空推力计）
    expected_mass_flow = 981_000.0 / (311.0 * dag.G0)
    assert result.values["stage1.mass_flow_kg_s"] == pytest.approx(expected_mass_flow)


def test_custom_isp_without_values_is_rejected_before_the_graph(
    single_stage_vehicle: Vehicle,
) -> None:
    """绕过诊断直调 DAG 且 custom 缺值：必须炸而不是静默取发动机值（掩盖违约）。"""
    payload_vehicle = _legal_vehicle("custom 缺值", (make_stage(1, length_m=41.2),))
    mutated = payload_vehicle.model_copy(
        update={"stages": (payload_vehicle.stages[0].model_copy(update={"isp_source": "custom"}),)}
    )

    with pytest.raises(ValueError, match="custom"):
        dag.propagate_vehicle(mutated)


def test_provided_derived_node_keeps_user_source(single_stage_vehicle: Vehicle) -> None:
    """调用方直接给定派生节点时来源标 ``user``（§6.2），不得被静默重算覆盖。"""
    provided = dag.vehicle_inputs(single_stage_vehicle)
    provided["stage1.volumetric_ratio"] = 1.75

    result = dag.build_vehicle_graph(single_stage_vehicle).propagate(provided)

    assert result.sources["stage1.volumetric_ratio"] is dag.Source.USER
    assert result.values["stage1.volumetric_ratio"] == pytest.approx(1.75)


def test_unused_inputs_are_reported(single_stage_vehicle: Vehicle) -> None:
    """图外的输入必须被点名——否则"改了一个不生效的参数"会毫无反馈（R-28）。"""
    provided = dag.vehicle_inputs(single_stage_vehicle)
    provided["stage1.bogus"] = 1.0

    result = dag.build_vehicle_graph(single_stage_vehicle).propagate(provided)

    assert result.unused_inputs == ("stage1.bogus",)


def test_override_notice_names_the_upstream_chain(single_stage_vehicle: Vehicle) -> None:
    """§5.9 派生规则 2 / §6.2：直接给定派生节点必须提示将覆盖哪些上游约束。"""
    graph = dag.build_vehicle_graph(single_stage_vehicle)

    notice = graph.override_notice("stage1.oxidizer_tank_length_m")

    assert "覆盖其上游派生链" in notice
    for upstream in (
        "stage1.oxidizer_volume_m3",
        "stage1.tank_area_m2",
        "stage1.stage_diameter_m",
    ):
        assert upstream in notice, f"覆盖提示漏了上游 {upstream}：{notice}"
    assert graph.override_notice("stage1.stage_diameter_m").endswith("不会覆盖其他节点")


def test_fairing_diameter_is_only_registered_when_enabled(single_stage_vehicle: Vehicle) -> None:
    """未启用整流罩时不注册该输入：否则它会永远躺在 ``deferred`` 里变成一条噪声。"""
    assert "vehicle.fairing_diameter_m" not in dag.build_vehicle_graph(single_stage_vehicle).names

    with_fairing = single_stage_vehicle.model_copy(update={"fairing_diameter_m": 5.2})
    result = dag.propagate_vehicle(with_fairing)

    assert result.values["vehicle.max_diameter_m"] == pytest.approx(5.2)
    assert "vehicle.fairing_diameter_m" not in result.deferred


# ---------------------------------------------------------------------------
# 整箭层聚合（§6.1）
# ---------------------------------------------------------------------------


def test_vehicle_aggregates_cover_every_stage(two_stage_vehicle: Vehicle) -> None:
    masses = {1: 450_000.0, 2: 90_000.0}
    result = dag.propagate_vehicle(two_stage_vehicle, propellant_mass_kg=masses)

    total_length = sum(stage.length_m for stage in two_stage_vehicle.stages)
    assert result.values["vehicle.total_length_m"] == pytest.approx(total_length)
    assert result.values["vehicle.max_diameter_m"] == pytest.approx(3.7)

    dry = sum(mass * 0.05 / 0.95 for mass in masses.values())
    assert result.values["vehicle.dry_mass_kg"] == pytest.approx(dry)
    glow = dry + sum(masses.values()) + two_stage_vehicle.payload_mass_kg
    assert result.values["vehicle.glow_kg"] == pytest.approx(glow)
    # 起飞推重比按**一级**海平面总推力计（§6.5 表）
    thrust = 9 * 845_000.0
    assert result.values["vehicle.twr_liftoff"] == pytest.approx(thrust / (glow * dag.G0))


def test_two_stages_get_distinct_namespaces(two_stage_vehicle: Vehicle) -> None:
    """节点必须按级分命名空间：否则两级会互相覆盖，而拓扑序上完全看不出来。"""
    result = dag.propagate_vehicle(two_stage_vehicle, propellant_mass_kg={1: 450_000.0})

    assert "stage2.propellant_mass_kg" in result.deferred
    assert result.values["stage1.oxidizer_volume_m3"] > 0.0
    assert "stage2.oxidizer_volume_m3" not in result.values


# ---------------------------------------------------------------------------
# 纯函数（§6.1）
# ---------------------------------------------------------------------------


def test_mass_flow_and_mass_fractions_are_closed_form() -> None:
    assert dag.tank_area(3.7) == pytest.approx(3.141592653589793 * 3.7**2 / 4.0)
    assert dag.tank_length(10.0, 2.0) == pytest.approx(5.0)
    assert dag.mass_flow(981_000.0, 311.0) == pytest.approx(981_000.0 / (311.0 * dag.G0))
    assert dag.dry_to_prop_ratio(0.05) == pytest.approx(0.05 / 0.95)
    assert dag.propellant_mass_fraction(0.05) == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# §5.9 箱体比例：交叉核验 + 两次公开数据反算
# ---------------------------------------------------------------------------


def test_volumetric_ratio_matches_the_closed_form(single_stage_vehicle: Vehicle) -> None:
    stage = single_stage_vehicle.stages[0]
    props = propellants.properties(stage.propellant)

    result = dag.propagate_vehicle(single_stage_vehicle)

    expected = dag.volumetric_ratio(
        stage.engine.mixture_ratio, props.density_ox_kg_m3, props.density_fuel_kg_m3
    )
    assert result.values["stage1.volumetric_ratio"] == pytest.approx(expected, rel=1e-12)


def test_volume_ratio_is_cross_checked_by_mass_and_density(single_stage_vehicle: Vehicle) -> None:
    """两条**独立**路径算出的容积比必须一致。

    ``volumetric_ratio`` 直接由 O/F 与密度算（§5.9 的公式），``volume_ratio_check``
    则由"质量 → 容积"（V = m/ρ）重算一遍。若哪条路径写错（例如密度写反），
    单看一条不会察觉，两条一比就会分开。
    """
    result = dag.propagate_vehicle(single_stage_vehicle, propellant_mass_kg={1: 450_000.0})

    assert result.values["stage1.volume_ratio_check"] == pytest.approx(
        result.values["stage1.volumetric_ratio"], rel=1e-9
    )


def test_tank_lengths_follow_volume_over_area(single_stage_vehicle: Vehicle) -> None:
    """§5.9 派生规则 1：箱长 = 容积 / 截面积，截面积取该级直径。"""
    mass = 450_000.0
    result = dag.propagate_vehicle(single_stage_vehicle, propellant_mass_kg={1: mass})
    stage = single_stage_vehicle.stages[0]
    props = propellants.properties(stage.propellant)
    area = dag.tank_area(stage.diameter_m)

    oxidizer = mass * 2.36 / 3.36
    ox_volume = oxidizer / props.density_ox_kg_m3
    fuel_volume = (mass / 3.36) / props.density_fuel_kg_m3

    assert result.values["stage1.oxidizer_volume_m3"] == pytest.approx(ox_volume)
    assert result.values["stage1.oxidizer_tank_length_m"] == pytest.approx(ox_volume / area)
    assert result.values["stage1.tanks_length_sum_m"] == pytest.approx(
        (ox_volume + fuel_volume) / area
    )


@pytest.mark.parametrize(
    ("model", "combination", "mixture_ratio", "formula_column", "ox_gal", "fuel_gal"),
    [
        # §5.9 表首行：S-IC，LOX 3 178 000 lb / 334 500 gal；RP-1 1 400 000 lb / 209 000 gal
        ("S-IC", "LOX/RP-1", 2.27, 1.60, 334_500.0, 209_000.0),
        # §5.9 表次行：S-II，LOX 83 000 gal；LH₂ 260 000 gal；J-2 O/F = 5
        ("S-II", "LOX/LH2", 5.0, 0.311, 83_000.0, 260_000.0),
    ],
    ids=["S-IC", "S-II"],
)
def test_section_5_9_reverse_calculations(
    model: str,
    combination: propellants.PropellantCombination,
    mixture_ratio: float,
    formula_column: float,
    ox_gal: float,
    fuel_gal: float,
) -> None:
    """§5.9 的两次独立反算（M2 验收项）。

    规格同时给了「公式结果」与「实测容积比」两列，故本用例**两边都核**：
    与公式列比得上，说明实现与 §5.9 的算式一致；与实测列比得上，
    说明该算式确实能还原公开数据（而不是只在自家公式里自洽）。
    """
    props = propellants.properties(combination)
    computed = dag.volumetric_ratio(mixture_ratio, props.density_ox_kg_m3, props.density_fuel_kg_m3)
    measured = ox_gal / fuel_gal

    assert computed == pytest.approx(formula_column, rel=_FORMULA_TOL), (
        f"{model}：由内置密度算得 {computed:.4f}，规格「公式结果」列为 {formula_column}"
    )
    assert computed == pytest.approx(measured, rel=_MEASURED_TOL), (
        f"{model}：由内置密度算得 {computed:.4f}，公开数据实测容积比为 {measured:.4f}"
    )


def test_lh2_breaks_the_oxidizer_tank_is_bigger_rule() -> None:
    """§5.9 的「非铁律」：液氢密度极低，氢氧级的容积比会小于 1（容积比 < 1 = 燃料箱更大）。

    这条钉住的是"不得把『氧箱恒大于燃料箱』写死进实现"——一旦有人按经验写死，
    本用例会立刻失败。
    """
    lox_lh2 = propellants.properties("LOX/LH2")
    lox_rp1 = propellants.properties("LOX/RP-1")

    hydrogen = dag.volumetric_ratio(5.0, lox_lh2.density_ox_kg_m3, lox_lh2.density_fuel_kg_m3)
    kerosene = dag.volumetric_ratio(2.36, lox_rp1.density_ox_kg_m3, lox_rp1.density_fuel_kg_m3)

    assert hydrogen < 1.0 < kerosene


def test_kerosene_stage_from_fixture_is_oxidizer_heavy() -> None:
    """正例夹具（LOX/RP-1）的容积比应大于 1，与其物理量级一致。"""
    vehicle = _legal_vehicle("容积比正例", (make_stage(1, length_m=41.2),))

    result = dag.propagate_vehicle(vehicle, propellant_mass_kg={1: 450_000.0})

    assert result.values["stage1.volumetric_ratio"] == pytest.approx(
        2.36 * 810.0 / 1141.0, rel=1e-12
    )
