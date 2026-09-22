"""优化目标注册与候选评估本体（规格 §14 约束 1：复用缓存 + 每候选带 provenance）。

目标族（§14 目标尺度：多目标 = 最小起飞质量 / 最大运力 / 最少级数——级数固定为
基线构型级数，不进本片变量空间，故目标族取可计算的三类）
------------------------------------------------------------------
- ``min_glow`` —— 最小起飞质量：GLOW = 用户载荷 + 全部级（含助推器）质量之和；
- ``max_payload_leo`` —— 最大 LEO 运力：运力表反推（OI-38 口径；链路为
  :func:`aeroforge.perf.capacity.payload_for_orbit` 的 LEO 行）；
- ``min_dry_mass`` —— 最小干重：Σ 级干重（σ 派生）+ 助推器干重。

多目标组合：请求给目标键列表，内部统一折算为**最小化向量**（``max_*`` 取负），
供 NSGA-II 的非支配排序消费（Deb 2001 口径）。

可行性 = 「无硬约束违反 + 分区铺满复核通过 + 评估链可解」（§14；不可行候选
淘汰、不计 fitness——具体淘汰机制在 :mod:`aeroforge.optimize.nsga2` 的约束支配）。
「评估链可解」涵盖：质量账构建失败、目标 ΔV 超出可达上限（PerfError）、
几何解析账拒绝（ValueError）——一律显式淘汰并记录原因，**绝不静默给 0 冒充**。

缓存（§14 约束 1）
------------------
每个可行候选的目标量经 :func:`aeroforge.cache.store.optimize_cache_key`
（canonical JSON + 口径分量）内容寻址缓存（``optimize.json``）。同参数候选在
NSGA-II 代间 / 批量扫描格点间 / 两次作业之间**直接命中不重算**；缓存只存
可行评估（不可行候选的判据本身是毫秒级复核，不值得缓存）。每次评估返回
行级 provenance（候选键 / 是否命中 / 耗时），结果级 provenance 由各运行
模块汇总（seed / 算法参数 / 命中数 / 总耗时）。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aeroforge.cache.store import ArtifactStore, optimize_cache_key
from aeroforge.errors import AeroForgeError
from aeroforge.optimize.design_space import candidate_infeasibility
from aeroforge.params.schema import Vehicle
from aeroforge.perf.capacity import DvSupplyMode, payload_for_orbit, vehicle_ledger
from aeroforge.perf.evaluate import resolve_site

#: 优化目标键（前端 ``OptimizeObjectiveKey`` 的超集：前端片只暴露前两个）。
ObjectiveKey = Literal["min_glow", "max_payload_leo", "min_dry_mass"]

#: 全部注册目标（键 → 中文说明；下发供审计，禁止前端自造目标）。
OBJECTIVE_REGISTRY: dict[ObjectiveKey, str] = {
    "min_glow": "最小起飞质量（GLOW，kg）",
    "max_payload_leo": "最大 LEO 运力（kg，运力表反推 OI-38 口径）",
    "min_dry_mass": "最小干重（级干重 + 助推器干重，kg）",
}

#: 行级 provenance 的固定键（Record<string, string> 契约，前端展开渲染）。
ROW_PROVENANCE_KEYS = ("vehicle_key", "cache_hit", "eval_ms")


class CandidateResult(BaseModel):
    """单个候选的评估结果（目标量 + 可行性 + 行级 provenance 数据）。"""

    model_config = ConfigDict(frozen=True)

    vehicle_key: str = Field(description="候选的内容寻址缓存键（canonical 输入的 sha256，可溯源）")
    feasible: bool = Field(description="候选可行与否（无硬违 + 分区铺满 + 链路可解）")
    infeasible_reason: str | None = Field(
        default=None, description="不可行原因（可行时为 null；不静默淘汰）"
    )
    violation_count: int = Field(
        default=0,
        description=(
            "约束违反计数（约束支配 Deb 2001 §7.1 的比较量）：可行 = 0；"
            "硬约束违反 = 条数；评估链不可解 = 1"
        ),
    )
    glow_kg: float | None = Field(default=None, description="起飞质量（kg，可行时必填）")
    dry_mass_kg: float | None = Field(default=None, description="干重（kg，可行时必填）")
    payload_leo_kg: float | None = Field(
        default=None, description="LEO 运力（kg；构型对 LEO 不可达时为 0——可达性不淘汰候选）"
    )
    extra_payloads: dict[str, float] = Field(
        default_factory=dict, description="附加约束轨道运力（kg；逆向设计的约束轨道行）"
    )
    cache_hit: bool = Field(default=False, description="本次评估是否命中候选缓存（§9.2）")
    elapsed_ms: float = Field(default=0.0, description="本次评估耗时（ms，含缓存命中的 IO）")


class OptimizeSolutionRow(BaseModel):
    """一个候选方案行（Pareto 前沿 / 权衡对比 / 扫描聚合 / 逆向最小构型共用形态）。

    与前端 ``OptimizeSolutionRow`` 契约逐键对齐：``label``（可缺省）、
    ``params``（field_path → 施加值，载入构型时按既有参数通路逐项写回）、
    ``glow_kg`` / ``payload_kg``（数量列）、``provenance``（行级溯源，可缺省）。
    """

    model_config = ConfigDict(frozen=True)

    label: str | None = Field(
        default=None, description="方案名（权衡 / 逆向行有；Pareto 行可缺省）"
    )
    params: dict[str, float] = Field(description="方案参数摘要：field_path → 施加值")
    glow_kg: float = Field(description="起飞质量（kg）")
    payload_kg: float = Field(description="LEO 运力（kg；逆向设计行为约束轨道运力）")
    provenance: dict[str, str] | None = Field(
        default=None, description="行级溯源（候选键 / 缓存命中 / 耗时等；缺失时 UI 不得伪造）"
    )


def objective_vector(result: CandidateResult, keys: tuple[ObjectiveKey, ...]) -> tuple[float, ...]:
    """可行候选的目标量 → **最小化向量**（``max_*`` 目标取负；Deb 2001 口径）。"""
    assert result.glow_kg is not None and result.dry_mass_kg is not None
    assert result.payload_leo_kg is not None
    vector: list[float] = []
    for key in keys:
        if key == "min_glow":
            vector.append(result.glow_kg)
        elif key == "max_payload_leo":
            vector.append(-result.payload_leo_kg)
        else:
            vector.append(result.dry_mass_kg)
    return tuple(vector)


def evaluate_candidate(
    vehicle: Vehicle,
    *,
    store: ArtifactStore,
    dv_supply: DvSupplyMode = "anchored",
    extra_orbits: tuple[str, ...] = (),
) -> CandidateResult:
    """评估一个候选（§14 约束 1 的兑现：缓存复用 + 行级 provenance）。

    顺序：可行性复核（毫秒级，不缓存）→ 缓存查询 → 评估链 → 缓存写回。
    ``extra_orbits`` 是逆向设计的约束轨道（≠ LEO 时追加该行并进缓存键，
    与 NSGA-II 的 LEO 行共享同键候选）。
    """
    started = time.perf_counter()

    infeasible, reason, hard = candidate_infeasibility(vehicle)
    if infeasible:
        return CandidateResult(
            vehicle_key=_candidate_key(vehicle, dv_supply, extra_orbits),
            feasible=False,
            infeasible_reason=reason,
            violation_count=len(hard) if hard else 1,
            elapsed_ms=_elapsed_ms(started),
        )

    key = _candidate_key(vehicle, dv_supply, extra_orbits)
    cached = store.load_optimize(key)
    if cached is not None:
        try:
            return CandidateResult(
                vehicle_key=key,
                feasible=True,
                glow_kg=float(cached["glow_kg"]),
                dry_mass_kg=float(cached["dry_mass_kg"]),
                payload_leo_kg=float(cached["payload_leo_kg"]),
                extra_payloads={k: float(v) for k, v in cached.get("extra_payloads", {}).items()},
                cache_hit=True,
                elapsed_ms=_elapsed_ms(started),
            )
        except (KeyError, TypeError, ValueError):
            # 缓存内容与当前形态不符（历史遗留 / 半写）：按未命中处理，重算覆盖
            pass

    try:
        ledger = vehicle_ledger(vehicle)
        warnings: list[str] = []
        site = resolve_site(vehicle, warnings)
        leo = payload_for_orbit(vehicle, site, "LEO", ledger=ledger, dv_supply=dv_supply)
        extras = {
            orbit: payload_for_orbit(
                vehicle, site, orbit, ledger=ledger, dv_supply=dv_supply
            ).payload_kg
            for orbit in extra_orbits
        }
        dry = sum(stage.m_dry_kg for stage in ledger.stages)
        if ledger.zero_stage is not None:
            dry += ledger.zero_stage.booster_dry_kg
    except (AeroForgeError, ValueError, ArithmeticError) as exc:
        # 链路不可解（质量账失败 / 目标超可达上限 / 几何解析拒绝）：显式淘汰不硬凑
        return CandidateResult(
            vehicle_key=key,
            feasible=False,
            infeasible_reason=f"评估链不可解（{type(exc).__name__}）：{exc}",
            violation_count=1,
            elapsed_ms=_elapsed_ms(started),
        )

    glow = ledger.glow_kg(vehicle.payload_mass_kg)
    store.save_optimize(
        key,
        {
            "glow_kg": glow,
            "dry_mass_kg": dry,
            "payload_leo_kg": leo.payload_kg,
            "extra_payloads": extras,
        },
    )
    return CandidateResult(
        vehicle_key=key,
        feasible=True,
        glow_kg=glow,
        dry_mass_kg=dry,
        payload_leo_kg=leo.payload_kg,
        extra_payloads=extras,
        cache_hit=False,
        elapsed_ms=_elapsed_ms(started),
    )


def evaluate_candidates(
    vehicles: list[Vehicle],
    *,
    store: ArtifactStore,
    dv_supply: DvSupplyMode = "anchored",
    extra_orbits: tuple[str, ...] = (),
) -> list[CandidateResult]:
    """批量评估（顺序执行——候选评估实测 ~0.2 ms/次，见 :mod:`aeroforge.optimize` 模块注）。"""
    return [
        evaluate_candidate(vehicle, store=store, dv_supply=dv_supply, extra_orbits=extra_orbits)
        for vehicle in vehicles
    ]


def _candidate_key(vehicle: Vehicle, dv_supply: DvSupplyMode, extra_orbits: tuple[str, ...]) -> str:
    """候选缓存键（canonical 输入摘要；可行性复核未过时不查询缓存）。"""
    return optimize_cache_key(vehicle, dv_supply=dv_supply, extra_orbits=extra_orbits)


def _elapsed_ms(started: float) -> float:
    """本次评估耗时（ms，三位小数——行级 provenance 的耗时口径）。"""
    return round((time.perf_counter() - started) * 1000.0, 3)


def row_provenance(result: CandidateResult) -> dict[str, str]:
    """行级 provenance（Record<string, string> 契约：全部值为字符串）。"""
    return {
        "vehicle_key": result.vehicle_key,
        "cache_hit": "true" if result.cache_hit else "false",
        "eval_ms": f"{result.elapsed_ms:.3f}",
    }


def audit_provenance(
    *,
    algorithm: str,
    parameters: dict[str, Any],
    evals_total: int,
    evals_fresh: int,
    evals_memoized: int,
    cache_hits_file: int,
    infeasible_evals: int,
    elapsed_ms: float,
) -> dict[str, Any]:
    """结果级审计 provenance（§14：每个结果可审计——算法、参数、缓存命中数、耗时）。"""
    return {
        "algorithm": algorithm,
        **parameters,
        "evals_total": evals_total,
        "evals_fresh": evals_fresh,
        "evals_memoized": evals_memoized,
        "cache_hits_file": cache_hits_file,
        "infeasible_evals": infeasible_evals,
        "cache_discipline": (
            "§14 约束 1：候选评估复用内容寻址缓存（perf-optimize-<sha256>，optimize.json）；"
            "同参数候选在代间 / 格点间 / 两次作业间直接命中不重算"
        ),
        "elapsed_ms": round(elapsed_ms, 2),
    }


__all__ = [
    "OBJECTIVE_REGISTRY",
    "ROW_PROVENANCE_KEYS",
    "CandidateResult",
    "ObjectiveKey",
    "OptimizeSolutionRow",
    "audit_provenance",
    "evaluate_candidate",
    "evaluate_candidates",
    "objective_vector",
    "row_provenance",
]
