"""逆向设计（规格 §14：给定运力目标求最小构型 = 约束优化，M6）。

实现选择（写明理由，随 provenance 下发）
------------------------------------------
**自由度**：单一「全级等比长度缩放因子 s ∈ [0.5, 1.5]」——不混入发动机数 /
助推器数量等整数自由度。理由：

1. **单调性可证**：固定其余参数时，s ↑ ⟹ 各级推进剂（几何解析账）↑ ⟹
   ΣΔV(P) ↑ ⟹ 各轨道运力单调不减；同时 GLOW 单调不减。两序同向，使
   「min GLOW s.t. 运力 ≥ 目标」退化为**一维单调搜索**：最优解 = 满足约束的
   最小 s（切边界解），二分法保证收敛到设计空间精度地板；
2. **最小性可证**：GLOW 与 s 同向单调 ⟹ 最小可行 s 即最小 GLOW——混合整数
   搜索无法给出这一保证（多峰 + 离散跳变），且「最小构型」的工程含义（同一
   构型方案按比例缩到恰好够用）与单一缩放自由度一致；
3. 候选评估与 NSGA-II / 扫描**同键共享缓存**（§14 约束 1）——二分 ~20 次评估
   全部走内容寻址缓存，重复逆向设计毫秒级返回。

**不硬凑**（§14 纪律）：目标运力超出该自由度在缩放带上的可达上限时，显式报
:class:`OptimizeError`（``OPTIMIZE_INFEASIBLE``，作业终态 FAILED）——如实告知
上限值与「本自由度不可达」的结论，绝不放大到上限硬凑一个不满足约束的解。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aeroforge.cache.store import ArtifactStore
from aeroforge.errors import OptimizeError
from aeroforge.optimize import OptimizeCancelled
from aeroforge.optimize.design_space import (
    LENGTH_SCALE_MAX,
    LENGTH_SCALE_MIN,
    DesignVariable,
    apply_values,
    design_variable_from_path,
)
from aeroforge.optimize.objectives import (
    CandidateResult,
    OptimizeSolutionRow,
    audit_provenance,
    evaluate_candidate,
    row_provenance,
)
from aeroforge.params.schema import Vehicle
from aeroforge.perf.capacity import DvSupplyMode

#: 逆向设计的目标轨道（与前端 ``InverseTargetOrbit`` 契约一致）。
InverseTargetOrbit = Literal["LEO", "GTO", "GEO"]

#: 二分收敛的相对宽度（缩放带 1.0 宽度上的 1e-4 ≈ 42.6 m 级长的毫米级分辨率）。
BISECT_REL_TOLERANCE = 1e-4

#: 二分迭代上限（防御性：1e-4 相对宽度理论 ~14 次，上限留 2 倍余量）。
BISECT_MAX_ITERATIONS = 32

ProgressCallback = Callable[[int, int], None]
CancelPredicate = Callable[[], bool]


class InverseRequest(BaseModel):
    """``POST /api/optimize/inverse`` 的请求体（逆向设计，规格 §14）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="基线飞行器参数（§6.1 全量；构型方案由此缩放派生）")
    target_orbit: InverseTargetOrbit = Field(description="目标轨道（LEO / GTO / GEO）")
    target_payload_kg: float = Field(
        gt=0.0, description="目标运力（kg；约束 运力 ≥ 目标，最小 GLOW）"
    )
    dv_supply: DvSupplyMode = Field(
        default="anchored",
        description="ΔV 需求供给模式（§8.6）：anchored（缺省，毫秒级）/ l2（每次评估一次积分）",
    )


def run_inverse(
    request: InverseRequest,
    *,
    store: ArtifactStore,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelPredicate | None = None,
) -> dict[str, Any]:
    """逆向设计主入口（§14）：返回 ``{config, warnings, provenance}``。

    一维单调二分（见模块 docstring 的实现选择）：不可行 / 不达标的缩放记为
    「短」，达标记为「满足」，收敛到最小达标 s——返回构型**保证**运力 ≥ 目标。
    """
    started = time.perf_counter()
    orbit = request.target_orbit
    # 约束轨道 ≠ LEO 时作为 extra_orbits 参与评估与缓存键（与 NSGA-II 同键共享）
    extra_orbits: tuple[str, ...] = () if orbit == "LEO" else (orbit,)

    variables: tuple[DesignVariable, ...] = tuple(
        design_variable_from_path(request.vehicle, f"stages[{i}].length_m")
        for i in range(len(request.vehicle.stages))
    )

    evals_total = 0
    cache_hits_file = 0
    iterations = 0

    def capacity_at(scale: float) -> tuple[CandidateResult, float]:
        """缩放 s 处的候选评估与目标轨道运力（不可行候选运力记 -1 = 不达标）。"""
        nonlocal evals_total, cache_hits_file
        candidate = apply_values(request.vehicle, variables, [scale] * len(request.vehicle.stages))
        result = evaluate_candidate(
            candidate, store=store, dv_supply=request.dv_supply, extra_orbits=extra_orbits
        )
        evals_total += 1
        if result.cache_hit:
            cache_hits_file += 1
        if not result.feasible:
            return result, -1.0
        if orbit == "LEO":
            assert result.payload_leo_kg is not None
            return result, result.payload_leo_kg
        payload = result.extra_payloads.get(orbit)
        if payload is None:  # pragma: no cover - extra_orbits 已保证该行存在
            raise OptimizeError(
                f"候选评估未返回约束轨道 {orbit} 的运力行",
                suggestion="这是实现缺陷，请附参数提交 issue",
            )
        return result, payload

    def meets(result: CandidateResult, payload: float) -> bool:
        return result.feasible and payload >= request.target_payload_kg - 1e-9

    # ── 边界探测：上限不可达即显式报错（不硬凑）；下限已达标直接取下界 ──
    result_hi, payload_hi = capacity_at(LENGTH_SCALE_MAX)
    if not meets(result_hi, payload_hi):
        upper_note = "" if result_hi.feasible else "构型已不可行，可达上限更低"
        raise OptimizeError(
            (
                f"目标运力 {request.target_payload_kg:.0f} kg（{orbit}）超出该构型在长度缩放"
                f"自由度上的可达上限 {payload_hi:.0f} kg（缩放带 [{LENGTH_SCALE_MIN}, "
                f"{LENGTH_SCALE_MAX}]，上限处{upper_note}）——逆向设计不硬凑"
            ),
            suggestion=(
                "提高可达上限需变更构型方案（级数 / 直径 / 发动机 / 推进剂 / 助推器）——"
                "不在本片缩放自由度内；或降低目标运力 / 改用 DV 供给更优的轨道需求口径"
            ),
            code="OPTIMIZE_INFEASIBLE",
        )
    result_lo, payload_lo = capacity_at(LENGTH_SCALE_MIN)

    best_scale = LENGTH_SCALE_MAX
    best_result, best_payload = result_hi, payload_hi
    if meets(result_lo, payload_lo):
        best_scale, best_result, best_payload = LENGTH_SCALE_MIN, result_lo, payload_lo
    else:
        # ── 二分不变量：lo 端不达标（不可行或运力不足），hi 端达标 ──
        lo, hi = LENGTH_SCALE_MIN, LENGTH_SCALE_MAX
        for iteration in range(BISECT_MAX_ITERATIONS):
            if should_cancel is not None and should_cancel():
                raise OptimizeCancelled
            mid = 0.5 * (lo + hi)
            result_mid, payload_mid = capacity_at(mid)
            iterations += 1
            if meets(result_mid, payload_mid):
                hi, best_result, best_payload = mid, result_mid, payload_mid
            else:
                lo = mid
            if hi - lo <= BISECT_REL_TOLERANCE * hi:
                best_scale = hi
                break
            if on_progress is not None:
                on_progress(iteration + 1, BISECT_MAX_ITERATIONS)
        else:
            best_scale = hi
        if on_progress is not None:
            on_progress(BISECT_MAX_ITERATIONS, BISECT_MAX_ITERATIONS)

    assert best_result.glow_kg is not None
    # 基线（1.0×）评估：仅作审计对照（config 是唯一契约载荷；缩放带覆盖基线）
    baseline_result, _payload_baseline = capacity_at(1.0)
    candidate = apply_values(request.vehicle, variables, [best_scale] * len(request.vehicle.stages))
    provenance_row = row_provenance(best_result)
    provenance_row.update(
        {
            "scale_factor": f"{best_scale:.6f}",
            "target_orbit": orbit,
            "target_payload_kg": f"{request.target_payload_kg:.1f}",
            "bisection_iterations": str(iterations),
        }
    )
    config = OptimizeSolutionRow(
        label="逆向最小构型",
        params={v.path: v.applied_value(candidate) for v in variables},
        glow_kg=best_result.glow_kg,
        payload_kg=best_payload,
        provenance=provenance_row,
    )

    warnings: list[str] = []
    if best_scale > 1.0 + 1e-9:
        warnings.append(
            f"目标运力高于基线构型运力——最优构型为基线放大 {best_scale:.3f}×"
            "（GLOW 相应增加，见 config.glow_kg）"
        )

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    provenance = audit_provenance(
        algorithm=(
            "逆向设计（§14 约束优化）：单一「全级等比长度缩放」自由度 + 一维单调二分"
            "（运力与 GLOW 对 s 同向单调，最小可行 s 即最小 GLOW；实现选择详见模块注）"
        ),
        parameters={
            "target_orbit": orbit,
            "target_payload_kg": request.target_payload_kg,
            "scale_band": f"[{LENGTH_SCALE_MIN}, {LENGTH_SCALE_MAX}]",
            "bisection_rel_tolerance": BISECT_REL_TOLERANCE,
            "bisection_iterations": iterations,
            "baseline_glow_kg": (
                baseline_result.glow_kg if baseline_result.glow_kg is not None else 0.0
            ),
            "dv_supply": request.dv_supply,
        },
        evals_total=evals_total,
        evals_fresh=evals_total - cache_hits_file,
        evals_memoized=0,
        cache_hits_file=cache_hits_file,
        infeasible_evals=0,
        elapsed_ms=elapsed_ms,
    )
    return {
        "config": config.model_dump(mode="json"),
        "warnings": warnings,
        "provenance": provenance,
    }


__all__ = [
    "BISECT_MAX_ITERATIONS",
    "BISECT_REL_TOLERANCE",
    "InverseRequest",
    "InverseTargetOrbit",
    "run_inverse",
]
