"""权衡研究（规格 §14：多方案并行评估与对比表，≤ 20 方案，M6）。

方案族（实现选择，随 provenance 下发）
----------------------------------------
请求只给 ``variant_count``（前端契约），方案由后端围绕基线构型确定性生成：
**全级等比长度缩放梯度**——N 个方案均匀铺在缩放带 ``[0.7, 1.3]``（工程惯例
对比带，非权威来源；N = 1 时取基线自身 1.0×）。选择理由：

- 梯度方案族**必然覆盖基线两侧**，对比表能同时呈现「缩比省质量 / 放大换运力」
  的权衡方向——这正是权衡研究要回答的问题；
- 方案生成**不依赖请求侧自由度**（变量清单 / 边界），同一基线的对比表可复现。

每个方案走与 NSGA-II 同一条候选评估链（同键缓存共享，§14 约束 1），行级
provenance 记录方案名 / 缩放因子 / 缓存命中 / 候选键。不可行方案**不进对比表**
（附 warning 说明原因——如实留痕，不静默丢行也不伪造数字）。
"""

from __future__ import annotations

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

#: 方案数上限（§14 目标尺度：≤ 20 方案；超限 422）。
MAX_VARIANTS = 20

#: 方案族的缩放对比带（工程惯例：基线 ±30%，非权威来源）。
TRADE_SCALE_MIN = 0.7
TRADE_SCALE_MAX = 1.3

ProgressCallback = Callable[[int, int], None]
CancelPredicate = Callable[[], bool]


class TradeStudyRequest(BaseModel):
    """``POST /api/optimize/trade-study`` 的请求体（权衡研究，规格 §14）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="基线飞行器参数（§6.1 全量；方案由此逐项派生）")
    variant_count: int = Field(
        ge=1,
        description=f"方案数（1–{MAX_VARIANTS}；§14 目标尺度 ≤ 20，超限 422）",
    )
    dv_supply: DvSupplyMode = Field(
        default="anchored",
        description="ΔV 需求供给模式（§8.6）：anchored（缺省，毫秒级）/ l2（每方案一次积分）",
    )


def validate_variant_count(variant_count: int) -> int:
    """方案数校验（> {MAX_VARIANTS} 即 422，§14 目标尺度）。"""
    if not 1 <= variant_count <= MAX_VARIANTS:
        raise OptimizeError(
            f"权衡研究方案数 {variant_count} 超出上限 {MAX_VARIANTS}（§14 目标尺度：≤ 20 方案）",
            suggestion=(
                f"把 variant_count 降到 1–{MAX_VARIANTS} 之间；更大规模的参数探索请用批量扫描"
            ),
        )
    return variant_count


def _scale_ladder(count: int) -> list[float]:
    """方案缩放梯度：N 点均匀铺在 [0.7, 1.3]；N = 1 时取基线自身。"""
    if count == 1:
        return [1.0]
    step = (TRADE_SCALE_MAX - TRADE_SCALE_MIN) / (count - 1)
    return [TRADE_SCALE_MIN + step * i for i in range(count)]


def run_trade_study(
    request: TradeStudyRequest,
    *,
    store: ArtifactStore,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelPredicate | None = None,
) -> dict[str, Any]:
    """权衡研究主入口（§14）：返回 ``{variants, warnings, provenance}``。"""
    started = time.perf_counter()
    count = validate_variant_count(request.variant_count)
    factors = _scale_ladder(count)

    # 变量族 = 全部级长度缩放（方案族的自由度；路径由基线推导，边界不做裁剪——
    # 方案梯度固定在对比带内，合法性交给候选评估复核）
    variables: tuple[DesignVariable, ...] = tuple(
        design_variable_from_path(request.vehicle, f"stages[{i}].length_m")
        for i in range(len(request.vehicle.stages))
    )

    rows: list[OptimizeSolutionRow] = []
    warnings: list[str] = []
    evals_total = 0
    cache_hits_file = 0
    infeasible_evals = 0

    for index, factor in enumerate(factors):
        if should_cancel is not None and should_cancel():
            raise OptimizeCancelled
        candidate = apply_values(request.vehicle, variables, [factor] * len(request.vehicle.stages))
        result = evaluate_candidate(candidate, store=store, dv_supply=request.dv_supply)
        evals_total += 1
        if result.cache_hit:
            cache_hits_file += 1
        label = f"全级缩放 {factor:.2f}×"
        if not result.feasible:
            infeasible_evals += 1
            assert result.infeasible_reason is not None
            warnings.append(f"方案「{label}」不可行，未进对比表（{result.infeasible_reason}）")
        else:
            assert result.glow_kg is not None and result.payload_leo_kg is not None
            row_prov = row_provenance(result)
            row_prov["scale_factor"] = f"{factor:.4f}"
            rows.append(
                OptimizeSolutionRow(
                    label=label,
                    params={v.path: v.applied_value(candidate) for v in variables},
                    glow_kg=result.glow_kg,
                    payload_kg=result.payload_leo_kg,
                    provenance=row_prov,
                )
            )
        if on_progress is not None:
            on_progress(index + 1, count)

    rows.sort(key=lambda row: row.glow_kg)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    provenance = audit_provenance(
        algorithm=(
            "权衡研究（§14）：全级等比长度缩放梯度方案族，均匀铺在 [0.7, 1.3] 对比带"
            "（N=1 取基线）；逐方案走候选评估链（与 NSGA-II 同键缓存共享）"
        ),
        parameters={
            "variant_count": count,
            "scale_band": f"[{TRADE_SCALE_MIN}, {TRADE_SCALE_MAX}]",
            "dv_supply": request.dv_supply,
        },
        evals_total=evals_total,
        evals_fresh=evals_total - cache_hits_file,
        evals_memoized=0,
        cache_hits_file=cache_hits_file,
        infeasible_evals=infeasible_evals,
        elapsed_ms=elapsed_ms,
    )
    if not rows:
        warnings.append("全部方案不可行——未得到对比表；请检查基线构型的合法性")
    return {
        "variants": [row.model_dump(mode="json") for row in rows],
        "warnings": warnings,
        "provenance": provenance,
    }


__all__ = [
    "MAX_VARIANTS",
    "TRADE_SCALE_MAX",
    "TRADE_SCALE_MIN",
    "TradeStudyRequest",
    "run_trade_study",
    "validate_variant_count",
]
