"""批量扫描（规格 §14：全因子 ≤ 10⁴ 组合，超限报错，M6）。

形态
----
- 轴 = 变量路径 + 范围 + 步数（≤ 3 轴，§14「推进剂 × 级数 × 轨道」的全因子
  形态落在本片变量空间内为「长度缩放 × 发动机数 × 助推器数量」）；
- **轴取值语义随变量类别**（OpenAPI 契约注明）：``continuous_scale`` 路径的
  min/max 是**无量纲缩放因子**（施加值 = 基线值 × 因子，与 NSGA-II 变量同口径）；
  ``integer_count`` 路径的 min/max 是**绝对数量**；
- 轴范围必须落在该变量的设计空间内（缩放 [0.5, 1.5] / 数量 [1, 上界]）——
  越界 422，不静默夹取（静默改用户的扫描范围会让"扫了什么"失真）；
- 组合数 = 各轴取值数的笛卡尔积，**> 10⁴ 即 422**（§14 目标尺度）；
- 同参数候选复用内容寻址缓存（§14 约束 1）：整型轴取值取整去重后，格点间
  相同候选直接命中；行级 provenance 记录缓存命中与候选键。

聚合表：逐格点一行（笛卡尔积序），列为参数摘要 + GLOW + LEO 运力；不可行
格点不进表（附 warning 汇总——扫描是"如实呈现可行域"的工具，不可行本身是结果）。
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from aeroforge.cache.store import ArtifactStore
from aeroforge.errors import OptimizeError
from aeroforge.optimize import OptimizeCancelled
from aeroforge.optimize.design_space import (
    DesignVariable,
    apply_values,
    design_variable_from_path,
)
from aeroforge.optimize.objectives import (
    OptimizeSolutionRow,
    audit_provenance,
    evaluate_candidate,
    row_provenance,
)
from aeroforge.params.schema import Vehicle
from aeroforge.perf.capacity import DvSupplyMode

#: 轴数上限（§14 全因子形态：≤ 3 轴）。
MAX_AXES = 3

#: 组合数上限（§14 目标尺度：全因子 ≤ 10⁴；超限 422）。
MAX_COMBINATIONS = 10_000

#: 每轴步数下限（单点不构成扫描）。
MIN_STEPS = 2

ProgressCallback = Callable[[int, int], None]
CancelPredicate = Callable[[], bool]


class SweepAxisSpec(BaseModel):
    """批量扫描的一个轴（变量 × 范围 × 步数；与前端 ``SweepAxisSpec`` 契约对齐）。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        description="变量路径（stages[i].length_m / stages[i].engine_count / boosters[j].count）"
    )
    min: float = Field(
        description="范围下界（continuous_scale 路径 = 缩放因子；integer_count 路径 = 绝对数量）"
    )
    max: float = Field(description="范围上界（口径同 min）")
    steps: int = Field(
        ge=MIN_STEPS,
        description=f"步数（≥ {MIN_STEPS}；下界到上界均匀采样）",
    )


class SweepRequest(BaseModel):
    """``POST /api/optimize/sweep`` 的请求体（批量扫描，规格 §14）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="基线飞行器参数（§6.1 全量；格点候选由此逐项派生）")
    axes: list[SweepAxisSpec] = Field(
        min_length=1,
        description=(
            f"扫描轴清单（1–{MAX_AXES} 轴；组合数 = 各轴取值数的笛卡尔积，≤ {MAX_COMBINATIONS}）"
        ),
    )
    dv_supply: DvSupplyMode = Field(
        default="anchored",
        description="ΔV 需求供给模式（§8.6）：anchored（缺省，毫秒级）/ l2（每格点一次积分）",
    )


def parse_axes(vehicle: Vehicle, axes: list[SweepAxisSpec]) -> tuple[DesignVariable, ...]:
    """扫描轴校验与解析（轴数 / 范围 / 设计空间相容 / 组合数上限，不合法即 422）。"""
    if len(axes) > MAX_AXES:
        raise OptimizeError(
            f"扫描轴数 {len(axes)} 超出上限 {MAX_AXES}（§14 全因子形态：≤ 3 轴）",
            suggestion="把相关变量合并为更少的轴，或拆成多次扫描",
        )
    variables: list[DesignVariable] = []
    seen: set[str] = set()
    for axis in axes:
        if axis.path in seen:
            raise OptimizeError(
                f"扫描轴 {axis.path!r} 重复声明——同一变量只能作为一个轴",
                suggestion="去掉重复轴后再提交",
            )
        seen.add(axis.path)
        variable = design_variable_from_path(vehicle, axis.path)
        if axis.min > axis.max:
            raise OptimizeError(
                f"扫描轴 {axis.path!r} 的范围颠倒（min {axis.min} > max {axis.max}）",
                suggestion="交换 min 与 max",
            )
        if axis.min < variable.lower or axis.max > variable.upper:
            raise OptimizeError(
                f"扫描轴 {axis.path!r} 的范围 [{axis.min}, {axis.max}] 越出该变量的设计空间 "
                f"[{variable.lower}, {variable.upper}]——不静默夹取扫描范围",
                suggestion=(
                    "把轴范围收进设计空间内（缩放因子轴 [0.5, 1.5]；发动机数轴 "
                    "[1, 2×基线]；助推器数轴 [1, 12]）"
                ),
            )
        variables.append(variable)

    total = 1
    for axis, variable in zip(axes, variables, strict=True):
        total *= len(_axis_values(axis, variable))
    if total > MAX_COMBINATIONS:
        raise OptimizeError(
            f"扫描组合数 {total} 超出上限 {MAX_COMBINATIONS}（§14 目标尺度：全因子 ≤ 10⁴）",
            suggestion="减少轴数 / 步数，或拆成多次扫描（全因子规模随轴数指数增长）",
        )
    return tuple(variables)


def _axis_values(axis: SweepAxisSpec, variable: DesignVariable) -> list[float]:
    """单轴取值序列：连续 = 均匀因子；整数 = 均匀取整后去重（保持升序）。"""
    if variable.kind == "continuous_scale":
        if axis.steps == 1:  # Schema 已挡；防御性兜底（不硬凑单点）
            raise OptimizeError(
                f"扫描轴 {axis.path!r} 步数必须 ≥ {MIN_STEPS}",
                suggestion=f"步数至少 {MIN_STEPS}（单点不构成扫描）",
            )
        step = (axis.max - axis.min) / (axis.steps - 1)
        return [axis.min + step * i for i in range(axis.steps)]
    raw = [axis.min + (axis.max - axis.min) * i / (axis.steps - 1) for i in range(axis.steps)]
    clamped = sorted(
        {int(min(max(round(v), int(variable.lower)), int(variable.upper))) for v in raw}
    )
    if not clamped:
        raise OptimizeError(
            f"扫描轴 {axis.path!r} 在范围内没有合法整数值",
            suggestion=f"数量轴的有效域为 [{variable.lower}, {variable.upper}]",
        )
    return [float(v) for v in clamped]


def run_sweep(
    request: SweepRequest,
    *,
    store: ArtifactStore,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelPredicate | None = None,
) -> dict[str, Any]:
    """批量扫描主入口（§14）：返回 ``{rows, warnings, provenance}``。"""
    started = time.perf_counter()
    variables = parse_axes(request.vehicle, request.axes)
    value_grid = [
        _axis_values(axis, variable) for axis, variable in zip(request.axes, variables, strict=True)
    ]
    combinations = list(itertools.product(*value_grid))

    rows: list[OptimizeSolutionRow] = []
    warnings: list[str] = []
    evals_total = 0
    cache_hits_file = 0
    infeasible_evals = 0
    total = len(combinations)

    for index, values in enumerate(combinations):
        if should_cancel is not None and should_cancel():
            raise OptimizeCancelled
        candidate = apply_values(request.vehicle, variables, list(values))
        result = evaluate_candidate(candidate, store=store, dv_supply=request.dv_supply)
        evals_total += 1
        if result.cache_hit:
            cache_hits_file += 1
        if not result.feasible:
            infeasible_evals += 1
        else:
            assert result.glow_kg is not None and result.payload_leo_kg is not None
            rows.append(
                OptimizeSolutionRow(
                    params={v.path: v.applied_value(candidate) for v in variables},
                    glow_kg=result.glow_kg,
                    payload_kg=result.payload_leo_kg,
                    provenance=row_provenance(result),
                )
            )
        if on_progress is not None:
            on_progress(index + 1, total)

    if infeasible_evals:
        warnings.append(
            f"{infeasible_evals} / {total} 个格点不可行（硬约束违反或分区铺满失败），"
            "未进聚合表——不可行域本身是扫描结果的一部分"
        )
    if not rows:
        warnings.append("全部格点不可行——未得到聚合表；请收缩轴范围或检查基线构型")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    provenance = audit_provenance(
        algorithm=(
            "批量扫描（§14）：全因子笛卡尔积 ≤ 10⁴ 组合；同参数候选复用内容寻址缓存"
            "（整型轴取整去重后格点间命中不重算）"
        ),
        parameters={
            "axes": [
                {
                    "path": v.path,
                    "kind": v.kind,
                    "range": f"[{axis.min}, {axis.max}]",
                    "steps": axis.steps,
                    "values_count": len(values),
                }
                for axis, v, values in zip(request.axes, variables, value_grid, strict=True)
            ],
            "combinations": total,
            "dv_supply": request.dv_supply,
        },
        evals_total=evals_total,
        evals_fresh=evals_total - cache_hits_file,
        evals_memoized=0,
        cache_hits_file=cache_hits_file,
        infeasible_evals=infeasible_evals,
        elapsed_ms=elapsed_ms,
    )
    return {
        "rows": [row.model_dump(mode="json") for row in rows],
        "warnings": warnings,
        "provenance": provenance,
    }


__all__ = [
    "MAX_AXES",
    "MAX_COMBINATIONS",
    "MIN_STEPS",
    "SweepAxisSpec",
    "SweepRequest",
    "parse_axes",
    "run_sweep",
]
