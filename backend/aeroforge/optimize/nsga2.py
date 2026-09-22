"""自研轻量 NSGA-II（规格 §14 / §3.1 选型表：无 pymoo 依赖，M6）。

算法形态（Deb, *Multi-Objective Optimization Using Evolutionary Algorithms*, 2001
——经典参数逐项标注，禁止无来源系数）
------------------------------------------------------------------
- **表示**：实数基因向量 ∈ [0,1]^n（逐变量归一化到搜索边界，解码见
  :meth:`DesignVariable.decode`）；
- **初始化**：均匀随机（``random.Random(seed)``，seed 复现——同 seed 两跑逐位一致）；
- **配对选择**：二进制锦标赛，判据 (非支配序 asc, 拥挤距离 desc)；
- **交叉**：SBX 模拟二进制交叉（Deb 2001 §5.3；η_c = 15，pc = 0.9——原书推荐值）；
- **变异**：多项式变异（Deb 2001 §5.4；η_m = 20，pm = 1/n——原书推荐值）；
- **环境选择**：μ+λ 精英保留——父代 ∪ 子代按非支配序逐层填充，末层按拥挤距离
  截断（NSGA-II 原始形态，Deb 2001 §6.2）；
- **约束处理**：约束支配（Deb 2001 §7.1）：可行 ⪯ 不可行；双可行比目标支配；
  双不可行比违反数——**不可行候选淘汰不计 fitness**（§14），且永不进入 Pareto 前沿。

确定性纪律
-----------
单一 ``random.Random(seed)`` 源；排序一律稳定键（目标向量 + 下标）；评估为
纯函数（同输入同输出）——同 seed 同输入两跑的 Pareto 前沿**逐位一致**（测试钉住）。
执行形态为顺序评估（单候选 ~0.2 ms，见 :mod:`aeroforge.optimize` 模块注），
取消在代间边界生效（无半成品）。
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aeroforge.cache.store import ArtifactStore
from aeroforge.errors import OptimizeError
from aeroforge.optimize import OptimizeCancelled
from aeroforge.optimize.design_space import (
    OptimizeVariableSpec,
    apply_variables,
    parse_variables,
)
from aeroforge.optimize.objectives import (
    OBJECTIVE_REGISTRY,
    CandidateResult,
    ObjectiveKey,
    OptimizeSolutionRow,
    audit_provenance,
    evaluate_candidate,
    objective_vector,
    row_provenance,
)
from aeroforge.params.schema import Vehicle
from aeroforge.perf.capacity import DvSupplyMode

#: 缺省种群规模 / 代数（§14 M6 判据口径：40×20；可配，上限防误配烧穿作业时长）。
DEFAULT_POPULATION = 40
DEFAULT_GENERATIONS = 20
MIN_POPULATION = 4
MAX_POPULATION = 200
MIN_GENERATIONS = 1
MAX_GENERATIONS = 100

#: SBX 交叉概率与分布指数（Deb 2001 §5.3 推荐值）。
SBX_PROBABILITY = 0.9
SBX_ETA = 15.0
#: 多项式变异分布指数（Deb 2001 §5.4 推荐值；变异概率 = 1/n）。
POLY_ETA = 20.0

ProgressCallback = Callable[[int, int], None]
CancelPredicate = Callable[[], bool]


class Nsga2Request(BaseModel):
    """``POST /api/optimize/nsga2`` 的请求体（多目标优化，规格 §14）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="基线飞行器参数（§6.1 全量；候选由此逐项派生）")
    variables: list[OptimizeVariableSpec] = Field(
        description="优化变量清单（path + kind + base；搜索边界由后端按变量类别推导）"
    )
    population_size: int = Field(
        default=DEFAULT_POPULATION,
        ge=MIN_POPULATION,
        le=MAX_POPULATION,
        description=(
            f"种群规模（缺省 {DEFAULT_POPULATION}；范围 {MIN_POPULATION}–{MAX_POPULATION}）"
        ),
    )
    generations: int = Field(
        default=DEFAULT_GENERATIONS,
        ge=MIN_GENERATIONS,
        le=MAX_GENERATIONS,
        description=(
            f"进化代数（缺省 {DEFAULT_GENERATIONS}；范围 {MIN_GENERATIONS}–{MAX_GENERATIONS}）"
        ),
    )
    seed: int = Field(
        default=42,
        ge=0,
        description="随机种子（同 seed 同输入两跑 Pareto 前沿逐位一致，§14 可复现）",
    )
    objectives: list[ObjectiveKey] = Field(
        description=(
            "目标键列表（可组合，非空）：min_glow / max_payload_leo / min_dry_mass"
            "——前端片暴露前两个"
        )
    )
    dv_supply: DvSupplyMode = Field(
        default="anchored",
        description=(
            "ΔV 需求供给模式（§8.6）：anchored（缺省，毫秒级）；l2 = 每候选一次"
            "弹道积分（~0.1 s）——大种群 × l2 会线性放大作业时长，按需选用"
        ),
    )


class _Individual:
    """种群个体：基因 + 评估结果 + 最小化目标向量 + 非支配序与拥挤距离（工作态）。

    目标向量在构造时**折算定值**（可行 = 目标向量；不可行 = 空元组——约束支配
    只比违反数），使支配比较不依赖任何运行期全局态（线程安全 + 白盒可测）。
    ``objectives`` 显式传入时跳过折算（白盒测试构造假想个体用）。
    """

    __slots__ = ("crowding", "genes", "objectives", "rank", "result")

    def __init__(
        self,
        genes: list[float],
        result: CandidateResult,
        *,
        objectives: tuple[float, ...] | None = None,
    ) -> None:
        self.genes = genes
        self.result = result
        if objectives is None:
            objectives = objective_vector(result, _RUN_OBJECTIVES) if result.feasible else ()
        self.objectives = objectives
        self.rank = 0
        self.crowding = 0.0

    @property
    def feasible(self) -> bool:
        return self.result.feasible


#: 个体的缺省目标折算键（仅 ``objectives`` 未显式传入时使用；run_nsga2 内总是显式传）。
_RUN_OBJECTIVES: tuple[ObjectiveKey, ...] = ("min_glow",)


def constraint_dominates(a: _Individual, b: _Individual) -> bool:
    """约束支配（Deb 2001 §7.1）：可行优先，双可行比目标，双不可行比违反数。"""
    if a.feasible and not b.feasible:
        return True
    if not a.feasible and b.feasible:
        return False
    if not a.feasible and not b.feasible:
        assert a.result is not None and b.result is not None
        return a.result.violation_count < b.result.violation_count
    obj_a, obj_b = a.objectives, b.objectives
    return all(x <= y for x, y in zip(obj_a, obj_b, strict=True)) and any(
        x < y for x, y in zip(obj_a, obj_b, strict=True)
    )


def fast_non_dominated_sort(population: list[_Individual]) -> list[list[int]]:
    """非支配排序（Deb 2001 §6.1）：返回逐层下标（约束支配口径）。

    O(M·N²) 的教科书实现——优化种群 ≤ 400（2×200 上限），纯 Python 足够快，
    不为常数因子引入向量化依赖（§3.1：无 pymoo / 无新增依赖）。
    """
    size = len(population)
    dominated: list[list[int]] = [[] for _ in range(size)]
    domination_count = [0] * size
    fronts: list[list[int]] = [[]]
    for i in range(size):
        for j in range(i + 1, size):
            if constraint_dominates(population[i], population[j]):
                dominated[i].append(j)
                domination_count[j] += 1
            elif constraint_dominates(population[j], population[i]):
                dominated[j].append(i)
                domination_count[i] += 1
    for i in range(size):
        if domination_count[i] == 0:
            population[i].rank = 0
            fronts[0].append(i)
    level = 0
    while fronts[level]:
        next_front: list[int] = []
        for i in fronts[level]:
            for j in dominated[i]:
                domination_count[j] -= 1
                if domination_count[j] == 0:
                    population[j].rank = level + 1
                    next_front.append(j)
        level += 1
        fronts.append(next_front)
    fronts.pop()  # 最后一层为空占位
    return fronts


def crowding_distance(front: list[int], population: list[_Individual]) -> None:
    """拥挤距离（Deb 2001 §6.2）：原位写入个体的 ``crowding``（边界 = ∞）。

    按各目标维分别排序累计归一化间距；同值个体保持下标序（稳定排序——确定性纪律）。
    """
    for i in front:
        population[i].crowding = 0.0
    if len(front) <= 2:
        for i in front:
            population[i].crowding = math.inf
        return
    dimensions = len(population[front[0]].objectives)
    for dim in range(dimensions):
        order = sorted(front, key=lambda i: (population[i].objectives[dim], i))
        values = [population[i].objectives[dim] for i in order]
        population[order[0]].crowding = math.inf
        population[order[-1]].crowding = math.inf
        span = values[-1] - values[0]
        if span <= 0.0:
            continue  # 该目标维在前沿内无差异：不贡献距离（保持另维贡献）
        for position in range(1, len(order) - 1):
            individual = population[order[position]]
            if math.isinf(individual.crowding):
                continue
            individual.crowding += (values[position + 1] - values[position - 1]) / span


def _tournament(a: int, b: int, population: list[_Individual], rng: random.Random) -> int:
    """二进制锦标赛：非支配序小者胜；同序比拥挤距离大者；再同则取下标小者（确定）。"""
    first, second = population[a], population[b]
    if first.rank != second.rank:
        return a if first.rank < second.rank else b
    if first.crowding != second.crowding:
        return a if first.crowding > second.crowding else b
    return min(a, b)


def _sbx_cross(parent_a: float, parent_b: float, rng: random.Random) -> tuple[float, float]:
    """SBX 模拟二进制交叉（Deb 2001 §5.3，归一化 [0,1] 基因；越界夹取）。"""
    if rng.random() >= SBX_PROBABILITY:
        return parent_a, parent_b
    u = rng.random()
    if u <= 0.5:
        beta = (2.0 * u) ** (1.0 / (SBX_ETA + 1.0))
    else:
        beta = (1.0 / (2.0 * (1.0 - u))) ** (1.0 / (SBX_ETA + 1.0))
    child_a = 0.5 * ((1.0 + beta) * parent_a + (1.0 - beta) * parent_b)
    child_b = 0.5 * ((1.0 - beta) * parent_a + (1.0 + beta) * parent_b)
    return _clamp01(child_a), _clamp01(child_b)


def _poly_mutate(gene: float, rng: random.Random, mutation_probability: float) -> float:
    """多项式变异（Deb 2001 §5.4，归一化 [0,1] 基因；扰动朝边界方向按分布收缩）。"""
    if rng.random() >= mutation_probability:
        return gene
    u = rng.random()
    if u <= 0.5:
        delta = (2.0 * u) ** (1.0 / (POLY_ETA + 1.0)) - 1.0
        return _clamp01(gene + delta * gene)
    delta = 1.0 - (2.0 * (1.0 - u)) ** (1.0 / (POLY_ETA + 1.0))
    return _clamp01(gene + delta * (1.0 - gene))


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def validate_objectives(keys: list[ObjectiveKey]) -> tuple[ObjectiveKey, ...]:
    """目标键校验：非空、可注册、去重保序（API 层同步调用 + run 内复跑，均廉价）。"""
    if not keys:
        raise OptimizeError(
            "目标清单为空——至少选择一个优化目标（min_glow / max_payload_leo / min_dry_mass）",
            suggestion="在面板勾选至少一个目标后重试",
        )
    deduped: list[ObjectiveKey] = []
    for key in keys:
        if key not in OBJECTIVE_REGISTRY:
            raise OptimizeError(
                f"未知优化目标 {key!r}（可用：{sorted(OBJECTIVE_REGISTRY)}）",
                suggestion="目标键取 OpenAPI schema 的枚举值",
            )
        if key not in deduped:
            deduped.append(key)
    return tuple(deduped)


def run_nsga2(
    request: Nsga2Request,
    *,
    store: ArtifactStore,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelPredicate | None = None,
) -> dict[str, Any]:
    """NSGA-II 主入口（§14）：返回 ``{pareto_front, warnings, provenance}``。

    - ``on_progress(done_generations, total)``：逐代回调（作业进度条）；
    - ``should_cancel()``：代间边界生效，取消抛 :class:`OptimizeCancelled`；
    - 同 seed 同输入两跑逐位一致（无时钟、无未定序统计，测试钉住）。
    """
    started = time.perf_counter()
    objectives = validate_objectives(request.objectives)
    variables = parse_variables(request.vehicle, request.variables)
    if request.dv_supply == "l2":
        warnings = [
            "dv_supply=l2：每候选含一次弹道积分（~0.1 s），大种群 × 多代会线性放大"
            "作业时长（§8.6 L2 供给口径）；缺省 anchored 为毫秒级评估"
        ]
    else:
        warnings = []

    rng = random.Random(request.seed)
    n_variables = len(variables)
    mutation_probability = 1.0 / n_variables

    evals_total = 0
    evals_fresh = 0
    cache_hits_file = 0
    infeasible_evals = 0

    def evaluate(genes: list[float], memo: dict[tuple[float, ...], CandidateResult]) -> _Individual:
        """单个体评估：基因级去重（代内 / 代间同基因直接复用评估结果）。"""
        nonlocal evals_total, evals_fresh, cache_hits_file, infeasible_evals
        key = tuple(genes)
        cached = memo.get(key)
        if cached is None:
            candidate = apply_variables(request.vehicle, variables, genes)
            result = evaluate_candidate(candidate, store=store, dv_supply=request.dv_supply)
            evals_total += 1
            if result.cache_hit:
                cache_hits_file += 1
            else:
                evals_fresh += 1
            if not result.feasible:
                infeasible_evals += 1
            memo[key] = result
            cached = result
        else:
            evals_total += 1
        vector = objective_vector(cached, objectives) if cached.feasible else ()
        return _Individual(list(genes), cached, objectives=vector)

    # ── 初始种群：均匀随机 ──
    memo: dict[tuple[float, ...], CandidateResult] = {}
    population = [
        evaluate([rng.random() for _ in range(n_variables)], memo)
        for _ in range(request.population_size)
    ]
    if on_progress is not None:
        on_progress(0, request.generations)

    # ── 逐代进化：锦标赛 → SBX + 多项式变异 → μ+λ 环境选择 ──
    for generation in range(1, request.generations + 1):
        if should_cancel is not None and should_cancel():
            raise OptimizeCancelled
        fronts = fast_non_dominated_sort(population)
        for front in fronts:
            crowding_distance(front, population)
        offspring_genes: list[list[float]] = []
        while len(offspring_genes) < request.population_size:
            first = _tournament(
                rng.randrange(len(population)), rng.randrange(len(population)), population, rng
            )
            second = _tournament(
                rng.randrange(len(population)), rng.randrange(len(population)), population, rng
            )
            genes_a, genes_b = population[first].genes, population[second].genes
            crossed = [_sbx_cross(a, b, rng) for a, b in zip(genes_a, genes_b, strict=True)]
            # 两个子代 = 逐变量取交叉对的 a / b 分量并独立变异（SBX × n 变量）
            child_a = [_poly_mutate(pair[0], rng, mutation_probability) for pair in crossed]
            offspring_genes.append(child_a)
            if len(offspring_genes) < request.population_size:
                child_b = [_poly_mutate(pair[1], rng, mutation_probability) for pair in crossed]
                offspring_genes.append(child_b)
        children = [evaluate(genes, memo) for genes in offspring_genes]
        combined = population + children
        combined_fronts = fast_non_dominated_sort(combined)
        survivors: list[_Individual] = []
        for front in combined_fronts:
            crowding_distance(front, combined)
            if len(survivors) + len(front) <= request.population_size:
                survivors.extend(combined[i] for i in front)
            else:
                # 末层按拥挤距离截断（Deb 2001 §6.2：优先保留稀疏区域）
                order = sorted(front, key=lambda i: (-combined[i].crowding, i))
                survivors.extend(
                    combined[i] for i in order[: request.population_size - len(survivors)]
                )
                break
        population = survivors
        if on_progress is not None:
            on_progress(generation, request.generations)

    # ── Pareto 前沿（第一层的可行成员；不可行候选永不入前沿——§14 淘汰纪律） ──
    fronts = fast_non_dominated_sort(population)
    first_front = fronts[0] if fronts else []
    feasible_front = [population[i] for i in first_front if population[i].feasible]
    rows: list[OptimizeSolutionRow] = []
    if feasible_front:
        for individual in feasible_front:
            assert individual.result.glow_kg is not None
            assert individual.result.payload_leo_kg is not None
            candidate = apply_variables(request.vehicle, variables, individual.genes)
            rows.append(
                OptimizeSolutionRow(
                    params={v.path: v.applied_value(candidate) for v in variables},
                    glow_kg=individual.result.glow_kg,
                    payload_kg=individual.result.payload_leo_kg,
                    provenance=row_provenance(individual.result),
                )
            )
        rows.sort(key=lambda row: (row.glow_kg, -row.payload_kg))
    else:
        warnings.append(
            "全部候选不可行（硬约束违反或分区铺满失败）——未得到 Pareto 前沿；"
            "请放宽变量边界或检查基线构型的合法性"
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    provenance = audit_provenance(
        algorithm=(
            "NSGA-II（自研轻量实现，Deb 2001：SBX ηc=15/pc=0.9 + 多项式变异 "
            "ηm=20/pm=1/n + 二进制锦标赛 + 拥挤度环境选择 + 约束支配）"
        ),
        parameters={
            "seed": request.seed,
            "population_size": request.population_size,
            "generations": request.generations,
            "objectives": [str(key) for key in objectives],
            "dv_supply": request.dv_supply,
            "variables": [
                {
                    "path": v.path,
                    "kind": v.kind,
                    "base": v.base,
                    "bounds": f"[{v.lower}, {v.upper}]",
                }
                for v in variables
            ],
        },
        evals_total=evals_total,
        evals_fresh=evals_fresh,
        evals_memoized=evals_total - evals_fresh - cache_hits_file,
        cache_hits_file=cache_hits_file,
        infeasible_evals=infeasible_evals,
        elapsed_ms=elapsed_ms,
    )
    return {
        "pareto_front": [row.model_dump(mode="json") for row in rows],
        "warnings": warnings,
        "provenance": provenance,
    }
