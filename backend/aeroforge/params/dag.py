"""依赖 DAG 与派生量传播（规格 §6.2 / §6.1 的「关键派生量」块）。

规则（§6.2 逐条）
----------------
- DAG 在**构建时**检测环路，有环即拒绝（:func:`build_vehicle_graph` 末尾立即拓扑排序）；
- 变更传播按**拓扑序**执行，单次传播内**每个节点最多计算一次**；
- 每个节点标记 ``source``：``user``（调用方给定）或 ``derived``（推导得出）；
- 调用方直接给定 ``derived`` 节点时，用 :meth:`DependencyGraph.override_notice`
  提示**将覆盖哪些上游约束**（不得静默覆盖，§5.9 派生规则 2）。

⚠ 缺输入不等于算得对
--------------------
本层的硬纪律：**推不出来的节点必须显式进 ``deferred`` 并写明缺哪个依赖**，
绝不填一个"看着合理"的默认值。M2 阶段 ``propellant_mass_kg`` 由 M4 的定尺求解提供，
故 §6.2 那条链（推进剂质量 → 贮箱容积 → 箱长）在 M2 只走一半——
这不是缺陷，而是**必须留痕**的进度事实（填 0 或填经验值才是缺陷）。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from aeroforge.params import propellants
from aeroforge.params.schema import ParamsModel, Vehicle

#: 标准重力加速度（m/s²）。比冲（s）↔ 有效排气速度的换算基准，§1.4-3 的 SI 口径。
G0 = 9.80665

#: §5.9 派生规则 1 要求的**近似方式声明**——须随 ``provenance`` 一起下发。
TANK_LENGTH_APPROXIMATION = (
    "分段柱体近似：箱长 = 容积 / (π·d²/4)，d 取该级直径；锥段未按平均半径折算（M5 补）"
)


class Source(StrEnum):
    """节点来源（§6.2）。"""

    USER = "user"
    DERIVED = "derived"


class DependencyError(ValueError):
    """依赖图自身的缺陷（节点重名、依赖未注册、成环）——属实现缺陷而非输入问题。"""


class DependencyCycleError(DependencyError):
    """成环。异常消息带上环路径，避免"有环"这种无从下手的报错。"""

    def __init__(self, cycle: tuple[str, ...]) -> None:
        self.cycle = cycle
        super().__init__(
            "依赖成环，拒绝加载："
            + " → ".join(cycle)
            + "。请检查派生量的定义顺序与依赖声明（规格 §6.2 要求启动时即检出）"
        )


class UnknownDependencyError(DependencyError):
    """依赖了未注册的节点。"""


@dataclass(frozen=True, slots=True)
class Node:
    """一个参数节点。``compute is None`` 即**输入节点**（由调用方或求解器提供）。"""

    name: str
    deps: tuple[str, ...] = ()
    compute: Callable[[Mapping[str, float]], float] | None = None
    note: str = ""

    @property
    def is_input(self) -> bool:
        return self.compute is None


def input_node(name: str, note: str = "") -> Node:
    """输入节点：值必须由调用方提供，**不提供即 deferred**（不得自行编造）。"""
    return Node(name=name, deps=(), compute=None, note=note)


def derived_node(
    name: str,
    deps: tuple[str, ...],
    compute: Callable[[Mapping[str, float]], float],
    note: str = "",
) -> Node:
    """派生节点：依赖全部就绪时按拓扑序计算一次。"""
    return Node(name=name, deps=deps, compute=compute, note=note)


def _bind(
    prefix: str,
    compute: Callable[[Mapping[str, float], str], float],
) -> Callable[[Mapping[str, float]], float]:
    """把节点前缀绑进计算函数（节点名形如 ``stage1.xxx``，取值须带前缀）。

    为什么不写成 ``lambda values, _p=prefix: ...``：**带默认参数的 lambda 是 mypy
    的类型推断盲区**（``Cannot infer type of lambda``），改用这个工厂后 lambda 的形参
    类型由 ``compute`` 的签名给出，推断即成立。
    """
    return lambda values: compute(values, prefix)


class PropagationResult(ParamsModel):
    """一次传播的完整账目——含**未算出来的**那些节点与原因。"""

    values: dict[str, float]
    sources: dict[str, Source]
    deferred: dict[str, str]
    unused_inputs: tuple[str, ...]
    order: tuple[str, ...]

    @property
    def derived_values(self) -> dict[str, float]:
        """只保留 ``source == derived`` 的节点（即真正由本层算出的量）。"""
        return {
            name: value
            for name, value in self.values.items()
            if self.sources.get(name) is Source.DERIVED
        }


class DependencyGraph:
    """显式依赖图（规格 §6.2）。"""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}

    def add(self, node: Node) -> None:
        if node.name in self._nodes:
            msg = f"节点重名：{node.name}"
            raise DependencyError(msg)
        self._nodes[node.name] = node

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._nodes)

    def node(self, name: str) -> Node:
        try:
            return self._nodes[name]
        except KeyError as exc:
            msg = f"未注册的节点：{name}"
            raise UnknownDependencyError(msg) from exc

    def topological_order(self) -> tuple[str, ...]:
        """拓扑序（依赖在前）。DFS 着色，命中环时抛出**带环路径**的异常。"""
        order: list[str] = []
        state: dict[str, int] = {}  # 0/缺省 = 未访问；1 = 在当前 DFS 栈上；2 = 已完成
        stack: list[str] = []

        def visit(name: str) -> None:
            current = state.get(name, 0)
            if current == 2:
                return
            if current == 1:
                raise DependencyCycleError(tuple([*stack[stack.index(name) :], name]))
            state[name] = 1
            stack.append(name)
            for dep in self.node(name).deps:
                if dep not in self._nodes:
                    msg = f"节点 {name} 依赖了未注册的 {dep}"
                    raise UnknownDependencyError(msg)
                visit(dep)
            stack.pop()
            state[name] = 2
            order.append(name)

        for name in self._nodes:
            visit(name)
        return tuple(order)

    def upstream(self, name: str) -> tuple[str, ...]:
        """传递闭包上游（含直接依赖）。用于"直接给定派生节点会覆盖哪些上游"。"""
        collected: list[str] = []
        pending = list(self.node(name).deps)
        while pending:
            current = pending.pop()
            if current in collected:
                continue
            collected.append(current)
            pending.extend(self.node(current).deps)
        return tuple(collected)

    def override_notice(self, name: str) -> str:
        """§6.2：调用方改 ``derived`` 节点时的提示文案。"""
        upstream = self.upstream(name)
        if not upstream:
            return f"{name} 无上游依赖，直接给定不会覆盖其他节点"
        return f"直接给定 {name} 将覆盖其上游派生链：{'、'.join(upstream)}"

    def propagate(self, provided: Mapping[str, float]) -> PropagationResult:
        """按拓扑序传播。每个节点**最多计算一次**；缺输入的节点进 ``deferred``。"""
        order = self.topological_order()
        values = {name: float(value) for name, value in provided.items()}
        sources = {name: Source.USER for name in values}
        deferred: dict[str, str] = {}

        for name in order:
            if name in provided:
                continue
            node = self._nodes[name]
            if node.is_input:
                deferred[name] = "未提供输入（由调用方或 M4 定尺求解提供）"
                continue
            missing = [dep for dep in node.deps if dep not in values]
            if missing:
                deferred[name] = "缺少依赖：" + "、".join(missing)
                continue
            assert node.compute is not None  # is_input 已排除 None
            values[name] = float(node.compute(values))
            sources[name] = Source.DERIVED

        return PropagationResult(
            values=values,
            sources=sources,
            deferred=deferred,
            unused_inputs=tuple(sorted(set(provided) - set(self._nodes))),
            order=order,
        )


# ---------------------------------------------------------------------------
# 派生量（§6.1 的「关键派生量」块 + §5.9 的箱体比例）
# ---------------------------------------------------------------------------


def volumetric_ratio(mixture_ratio: float, density_ox: float, density_fuel: float) -> float:
    """§5.9：``V_ox / V_fuel = (O/F) · (ρ_fuel / ρ_ox)``。

    ⚠ 这是本节**唯一的几何语义变更**（故 ``SPEC_VERSION`` 递增至 0.6.0）：
    箱体比例不再由用户直接给定，而由发动机混合比与两剂密度派生。
    """
    return mixture_ratio * density_fuel / density_ox


def tank_area(diameter_m: float) -> float:
    """贮箱截面积（§5.9 派生规则 1：取该级直径）。"""
    return math.pi * diameter_m**2 / 4.0


def tank_length(volume_m3: float, area_m2: float) -> float:
    """§5.9 派生规则 1：箱长 = 容积 / 截面积（分段柱体近似，近似方式见
    :data:`TANK_LENGTH_APPROXIMATION`）。"""
    return volume_m3 / area_m2


def mass_flow(thrust_n: float, isp_s: float) -> float:
    """§6.1：``ṁ = 推力 / (Isp · g₀)``。"""
    return thrust_n / (isp_s * G0)


def dry_to_prop_ratio(structure_coefficient: float) -> float:
    """§6.1：``m_dry / m_prop = σ / (1 − σ)``（σ 是存储权威，本式是等价表示）。"""
    return structure_coefficient / (1.0 - structure_coefficient)


def propellant_mass_fraction(structure_coefficient: float) -> float:
    """§6.1：``m_prop / (m_dry + m_prop) = 1 − σ``。"""
    return 1.0 - structure_coefficient


# ---------------------------------------------------------------------------
# 图装配：把 §6.2 的链条落成节点
# ---------------------------------------------------------------------------


def stage_prefix(index: int) -> str:
    """级的节点命名空间前缀（``stage1`` / ``stage2`` …）。"""
    return f"stage{index}"


def _stage_nodes(prefix: str) -> tuple[Node, ...]:
    return (
        input_node(f"{prefix}.mixture_ratio", "混合比 O/F（来自发动机定义）"),
        input_node(f"{prefix}.density_ox_kg_m3", "氧化剂密度（推进剂物性表）"),
        input_node(f"{prefix}.density_fuel_kg_m3", "燃料密度（推进剂物性表）"),
        input_node(f"{prefix}.structure_coefficient", "结构系数 σ（存储权威）"),
        input_node(f"{prefix}.stage_diameter_m", "级直径"),
        input_node(f"{prefix}.stage_length_m", "级高度"),
        input_node(f"{prefix}.engine_count", "发动机台数（整数值，图上按浮点流转）"),
        input_node(f"{prefix}.engine_thrust_sea_level_n", "单机海平面推力"),
        input_node(f"{prefix}.engine_thrust_vacuum_n", "单机真空推力"),
        input_node(f"{prefix}.isp_vacuum_s", "该级真空比冲"),
        input_node(
            f"{prefix}.propellant_mass_kg",
            "该级推进剂质量——由 M4 定尺求解提供；M2 缺省时下游节点整体 deferred",
        ),
        derived_node(
            f"{prefix}.tank_area_m2",
            (f"{prefix}.stage_diameter_m",),
            _bind(prefix, lambda values, p: tank_area(values[f"{p}.stage_diameter_m"])),
            "§5.9 派生规则 1：截面积取该级直径",
        ),
        derived_node(
            f"{prefix}.volumetric_ratio",
            (
                f"{prefix}.mixture_ratio",
                f"{prefix}.density_ox_kg_m3",
                f"{prefix}.density_fuel_kg_m3",
            ),
            _bind(
                prefix,
                lambda values, p: volumetric_ratio(
                    values[f"{p}.mixture_ratio"],
                    values[f"{p}.density_ox_kg_m3"],
                    values[f"{p}.density_fuel_kg_m3"],
                ),
            ),
            "§5.9 箱体比例派生（本轮唯一几何语义变更）",
        ),
        derived_node(
            f"{prefix}.oxidizer_mass_kg",
            (f"{prefix}.propellant_mass_kg", f"{prefix}.mixture_ratio"),
            _bind(
                prefix,
                lambda values, p: (
                    values[f"{p}.propellant_mass_kg"]
                    * values[f"{p}.mixture_ratio"]
                    / (1.0 + values[f"{p}.mixture_ratio"])
                ),
            ),
            "m_ox = m_prop · (O/F)/(1+O/F)",
        ),
        derived_node(
            f"{prefix}.fuel_mass_kg",
            (f"{prefix}.propellant_mass_kg", f"{prefix}.mixture_ratio"),
            _bind(
                prefix,
                lambda values, p: (
                    values[f"{p}.propellant_mass_kg"] / (1.0 + values[f"{p}.mixture_ratio"])
                ),
            ),
            "m_fuel = m_prop/(1+O/F)",
        ),
        derived_node(
            f"{prefix}.dry_mass_kg",
            (f"{prefix}.propellant_mass_kg", f"{prefix}.structure_coefficient"),
            _bind(
                prefix,
                lambda values, p: (
                    values[f"{p}.propellant_mass_kg"]
                    * dry_to_prop_ratio(values[f"{p}.structure_coefficient"])
                ),
            ),
            "m_dry = m_prop · σ/(1−σ)",
        ),
        derived_node(
            f"{prefix}.oxidizer_volume_m3",
            (f"{prefix}.oxidizer_mass_kg", f"{prefix}.density_ox_kg_m3"),
            _bind(
                prefix,
                lambda values, p: values[f"{p}.oxidizer_mass_kg"] / values[f"{p}.density_ox_kg_m3"],
            ),
            "§6.1：V = m/ρ（氧化剂箱）",
        ),
        derived_node(
            f"{prefix}.fuel_volume_m3",
            (f"{prefix}.fuel_mass_kg", f"{prefix}.density_fuel_kg_m3"),
            _bind(
                prefix,
                lambda values, p: values[f"{p}.fuel_mass_kg"] / values[f"{p}.density_fuel_kg_m3"],
            ),
            "§6.1：V = m/ρ（燃料箱）",
        ),
        derived_node(
            f"{prefix}.volume_ratio_check",
            (f"{prefix}.oxidizer_volume_m3", f"{prefix}.fuel_volume_m3"),
            _bind(
                prefix,
                lambda values, p: values[f"{p}.oxidizer_volume_m3"] / values[f"{p}.fuel_volume_m3"],
            ),
            "由质量与密度**独立重算**的容积比：与 volumetric_ratio 互为交叉核验",
        ),
        derived_node(
            f"{prefix}.oxidizer_tank_length_m",
            (f"{prefix}.oxidizer_volume_m3", f"{prefix}.tank_area_m2"),
            _bind(
                prefix,
                lambda values, p: tank_length(
                    values[f"{p}.oxidizer_volume_m3"], values[f"{p}.tank_area_m2"]
                ),
            ),
            "§5.9 派生规则 1",
        ),
        derived_node(
            f"{prefix}.fuel_tank_length_m",
            (f"{prefix}.fuel_volume_m3", f"{prefix}.tank_area_m2"),
            _bind(
                prefix,
                lambda values, p: tank_length(
                    values[f"{p}.fuel_volume_m3"], values[f"{p}.tank_area_m2"]
                ),
            ),
            "§5.9 派生规则 1",
        ),
        derived_node(
            f"{prefix}.tanks_length_sum_m",
            (f"{prefix}.oxidizer_tank_length_m", f"{prefix}.fuel_tank_length_m"),
            _bind(
                prefix,
                lambda values, p: (
                    values[f"{p}.oxidizer_tank_length_m"] + values[f"{p}.fuel_tank_length_m"]
                ),
            ),
            "两箱长度之和（供 M5 与级长比对）",
        ),
        derived_node(
            f"{prefix}.mass_flow_kg_s",
            (f"{prefix}.engine_thrust_vacuum_n", f"{prefix}.isp_vacuum_s"),
            _bind(
                prefix,
                lambda values, p: mass_flow(
                    values[f"{p}.engine_thrust_vacuum_n"], values[f"{p}.isp_vacuum_s"]
                ),
            ),
            "§6.1：ṁ = F/(Isp·g₀)（按单机真空推力计）",
        ),
        derived_node(
            f"{prefix}.burn_time_s",
            (f"{prefix}.propellant_mass_kg", f"{prefix}.mass_flow_kg_s"),
            _bind(
                prefix,
                lambda values, p: values[f"{p}.propellant_mass_kg"] / values[f"{p}.mass_flow_kg_s"],
            ),
            "§6.1：t = 推进剂质量 / ṁ",
        ),
        derived_node(
            f"{prefix}.dry_to_prop_ratio",
            (f"{prefix}.structure_coefficient",),
            _bind(
                prefix,
                lambda values, p: dry_to_prop_ratio(values[f"{p}.structure_coefficient"]),
            ),
            "§6.1：干质比 = σ/(1−σ)（等价表示，非独立输入）",
        ),
        derived_node(
            f"{prefix}.propellant_mass_fraction",
            (f"{prefix}.structure_coefficient",),
            _bind(
                prefix,
                lambda values, p: propellant_mass_fraction(values[f"{p}.structure_coefficient"]),
            ),
            "§6.1：推进剂质量分数 = 1−σ（等价表示，非独立输入）",
        ),
        derived_node(
            f"{prefix}.thrust_sea_level_total_n",
            (f"{prefix}.engine_count", f"{prefix}.engine_thrust_sea_level_n"),
            _bind(
                prefix,
                lambda values, p: (
                    values[f"{p}.engine_count"] * values[f"{p}.engine_thrust_sea_level_n"]
                ),
            ),
            "台数 × 单机海平面推力（起飞推力）",
        ),
        derived_node(
            f"{prefix}.length_to_diameter",
            (f"{prefix}.stage_length_m", f"{prefix}.stage_diameter_m"),
            _bind(
                prefix,
                lambda values, p: values[f"{p}.stage_length_m"] / values[f"{p}.stage_diameter_m"],
            ),
            "§6.1：长径比",
        ),
    )


def _vehicle_nodes(stages: tuple[int, ...], *, fairing_diameter: bool) -> tuple[Node, ...]:
    """整箭层节点。``stages`` 是参与聚合的级号序列（自下而上）。"""
    prefixes = tuple(stage_prefix(index) for index in stages)
    diameter_deps = [*(f"{prefix}.stage_diameter_m" for prefix in prefixes)]
    inputs: list[Node] = [input_node("vehicle.payload_mass_kg", "有效载荷质量")]
    if fairing_diameter:
        # 只在启用整流罩时注册：否则该输入会永远躺在 deferred 里，变成一条无意义的噪声
        diameter_deps.append("vehicle.fairing_diameter_m")
        inputs.append(input_node("vehicle.fairing_diameter_m", "整流罩直径"))

    return (
        *inputs,
        derived_node(
            "vehicle.total_length_m",
            tuple(f"{prefix}.stage_length_m" for prefix in prefixes),
            lambda values: sum(values[f"{prefix}.stage_length_m"] for prefix in prefixes),
            "§6.1：总长 = 各级长度之和（MVP 轴对称构型）",
        ),
        derived_node(
            "vehicle.max_diameter_m",
            tuple(diameter_deps),
            lambda values: max(values[name] for name in diameter_deps),
            "§6.1：最大直径 = 各级直径与整流罩直径的最大值",
        ),
        derived_node(
            "vehicle.length_to_diameter",
            ("vehicle.total_length_m", "vehicle.max_diameter_m"),
            lambda values: values["vehicle.total_length_m"] / values["vehicle.max_diameter_m"],
            "§6.1：长径比 = 总长 / 最大直径",
        ),
        derived_node(
            "vehicle.dry_mass_kg",
            tuple(f"{prefix}.dry_mass_kg" for prefix in prefixes),
            lambda values: sum(values[f"{prefix}.dry_mass_kg"] for prefix in prefixes),
            "各级干质量之和",
        ),
        derived_node(
            "vehicle.glow_kg",
            (
                "vehicle.dry_mass_kg",
                *(f"{prefix}.propellant_mass_kg" for prefix in prefixes),
                "vehicle.payload_mass_kg",
            ),
            lambda values: (
                values["vehicle.dry_mass_kg"]
                + sum(values[f"{prefix}.propellant_mass_kg"] for prefix in prefixes)
                + values["vehicle.payload_mass_kg"]
            ),
            "起飞质量 GLOW = 干质量 + 推进剂 + 载荷（供 §6.5 推重比判据）",
        ),
        derived_node(
            "vehicle.twr_liftoff",
            (f"{stage_prefix(stages[0])}.thrust_sea_level_total_n", "vehicle.glow_kg"),
            lambda values: (
                values[f"{stage_prefix(stages[0])}.thrust_sea_level_total_n"]
                / (values["vehicle.glow_kg"] * G0)
            ),
            "§6.5 起飞推重比 = 一级海平面总推力 / (GLOW·g₀)",
        ),
    )


def build_vehicle_graph(vehicle: Vehicle) -> DependencyGraph:
    """按一架飞行器装配依赖图，并**立即验环**（§6.2：有环即拒绝加载）。"""
    indices = tuple(stage.index for stage in vehicle.stages)
    graph = DependencyGraph()
    for index in indices:
        for node in _stage_nodes(stage_prefix(index)):
            graph.add(node)
    for node in _vehicle_nodes(indices, fairing_diameter=vehicle.fairing_diameter_m is not None):
        graph.add(node)
    graph.topological_order()
    return graph


def vehicle_inputs(
    vehicle: Vehicle,
    *,
    propellant_mass_kg: Mapping[int, float] | None = None,
) -> dict[str, float]:
    """把 Schema 里的用户值投影成图的输入命名空间。

    ``propellant_mass_kg``（级号 → 质量）**不是 Schema 字段**：它由 M4 的定尺求解给出
    （§6.2 的推论——"目标运力 → 所需推进剂质量"是求解器的输出，不是用户的输入）。
    未给出的级，其下游节点会整体进 ``deferred``，而不是被填一个假值。
    """
    provided: dict[str, float] = {"vehicle.payload_mass_kg": vehicle.payload_mass_kg}
    if vehicle.fairing_diameter_m is not None:
        provided["vehicle.fairing_diameter_m"] = vehicle.fairing_diameter_m

    masses = propellant_mass_kg or {}
    for stage in vehicle.stages:
        prefix = stage_prefix(stage.index)
        props = propellants.properties(stage.propellant)
        provided[f"{prefix}.mixture_ratio"] = stage.engine.mixture_ratio
        provided[f"{prefix}.density_ox_kg_m3"] = props.density_ox_kg_m3
        provided[f"{prefix}.density_fuel_kg_m3"] = props.density_fuel_kg_m3
        provided[f"{prefix}.structure_coefficient"] = stage.structure_coefficient
        provided[f"{prefix}.stage_diameter_m"] = stage.diameter_m
        provided[f"{prefix}.stage_length_m"] = stage.length_m
        provided[f"{prefix}.engine_count"] = float(stage.engine_count)
        provided[f"{prefix}.engine_thrust_sea_level_n"] = stage.engine.thrust_sea_level_n
        provided[f"{prefix}.engine_thrust_vacuum_n"] = stage.engine.thrust_vacuum_n
        # 唯一权威（QA-1，v0.6.2）：级层省略 isp_* 即取发动机标称值；
        # custom 却缺值属约束引擎的 hard 违反——这里不再兜底（兜底会掩盖违约），
        # 直接拒绝，防止绕过诊断直调本函数时把 None 静默带进图。
        if stage.isp_vacuum_s is not None:
            provided[f"{prefix}.isp_vacuum_s"] = stage.isp_vacuum_s
        elif stage.isp_source == "custom":
            raise ValueError(
                f"第 {stage.index} 级 isp_source=custom 但未提供 isp_vacuum_s（先跑诊断）"
            )
        else:
            provided[f"{prefix}.isp_vacuum_s"] = stage.engine.isp_vacuum_s
        mass = masses.get(stage.index)
        if mass is not None:
            provided[f"{prefix}.propellant_mass_kg"] = mass
    return provided


def propagate_vehicle(
    vehicle: Vehicle,
    *,
    propellant_mass_kg: Mapping[int, float] | None = None,
) -> PropagationResult:
    """便捷入口：建图 → 投影输入 → 传播。"""
    graph = build_vehicle_graph(vehicle)
    return graph.propagate(vehicle_inputs(vehicle, propellant_mass_kg=propellant_mass_kg))


__all__ = [
    "G0",
    "TANK_LENGTH_APPROXIMATION",
    "DependencyCycleError",
    "DependencyError",
    "DependencyGraph",
    "Node",
    "PropagationResult",
    "Source",
    "UnknownDependencyError",
    "build_vehicle_graph",
    "derived_node",
    "dry_to_prop_ratio",
    "input_node",
    "mass_flow",
    "propagate_vehicle",
    "propellant_mass_fraction",
    "stage_prefix",
    "tank_area",
    "tank_length",
    "vehicle_inputs",
    "volumetric_ratio",
]
