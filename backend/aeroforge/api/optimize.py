"""优化与权衡研究域端点（规格 §14，M6 缺项补做）。

- ``POST /api/optimize/nsga2``       —— 多目标优化（自研轻量 NSGA-II，Deb 2001 经典参数）
- ``POST /api/optimize/trade-study`` —— 权衡研究（≤ 20 命名方案对比表）
- ``POST /api/optimize/sweep``       —— 批量扫描（≤ 3 轴全因子，组合 ≤ 10⁴）
- ``POST /api/optimize/inverse``     —— 逆向设计（给定运力目标求最小构型）

四端点**全部异步作业**（§9.1 惯例）：本模块只做**同步校验**（参数域硬约束 +
优化域变量 / 上限——超限当场 422，不占用作业名额）与**微秒级入队**；计算在
作业执行器的协调线程内顺序评估（单候选 ~0.2 ms，见 :mod:`aeroforge.optimize`
模块注），进度按代数 / 批次上报，结果经既有作业取回通道
（``GET /api/jobs/{id}`` / ``/ws/jobs/{id}``）下发——``metrics`` 形态：

- nsga2 → ``{pareto_front, warnings, provenance}``（多目标 Pareto 前沿）
- trade-study → ``{variants, warnings, provenance}``（对比表）
- sweep → ``{rows, warnings, provenance}``（聚合表）
- inverse → ``{config, warnings, provenance}``（最小构型）

行形态统一为 ``{label?, params, glow_kg, payload_kg, provenance?}``
（:class:`~aeroforge.optimize.objectives.OptimizeSolutionRow`）；§14 约束 1 的
「每候选带 provenance」由行级 ``provenance``（候选键 / 缓存命中 / 耗时）与
结果级 ``provenance``（算法参数 / seed / 命中数 / 总耗时）共同兑现。

缓存（§14 约束 1）：候选评估复用内容寻址缓存（``optimize.json``，
:func:`~aeroforge.cache.store.optimize_cache_key`），四类运行在同参数候选上
共享命中——重复评估 / 交叉运行的候选不重算。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aeroforge.api.deps import get_compute_runner
from aeroforge.errors import ParamsError
from aeroforge.optimize.design_space import parse_variables
from aeroforge.optimize.inverse import InverseRequest
from aeroforge.optimize.nsga2 import Nsga2Request, validate_objectives
from aeroforge.optimize.sweep import SweepRequest, parse_axes
from aeroforge.optimize.trade_study import TradeStudyRequest, validate_variant_count
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Vehicle

router = APIRouter(tags=["optimize"])

#: 作业 kind（端点路径尾段 = worker 分派键，四端点共用同一 submit 入口）。
_KIND_NSGA2 = "nsga2"
_KIND_TRADE = "trade-study"
_KIND_SWEEP = "sweep"
_KIND_INVERSE = "inverse"


class OptimizeJobResponse(BaseModel):
    """优化作业投递的受理回执（形态随 ``/api/perf/trajectory`` 的异步挂点）。"""

    job_id: str = Field(
        description=(
            "优化作业 id：GET /api/jobs/{id} 轮询或 /ws/jobs/{id} 订阅；成功后 "
            "metrics = 结果载荷（键随端点：pareto_front / variants / rows / config）"
        )
    )


def _reject_hard_violations(vehicle: Vehicle) -> None:
    """参数域硬约束前置拒绝（与 /api/perf/evaluate 同判据：422 + 字段级裁定）。"""
    violations = check_vehicle(vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝进入优化",
            suggestion=hard[0].suggestion,
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )


@router.post("/api/optimize/nsga2", response_model=OptimizeJobResponse)
def start_nsga2(request: Nsga2Request) -> OptimizeJobResponse:
    """投递 NSGA-II 多目标优化作业（§14；异步——进度按代数上报）。

    同步段只做校验与入队（规则 1）：参数域硬约束、变量三重校验（路径 / 类别 /
    基线一致性，不合法 422 ``OPTIMIZE_INVALID``）、目标键与规模边界。缺省
    40×20（F9 三变量实测 < 0.5 s，§16 M6 判据「一次优化 < 1 min」）。
    """
    _reject_hard_violations(request.vehicle)
    parse_variables(request.vehicle, request.variables)
    validate_objectives(request.objectives)
    record = get_compute_runner().submit_optimize(_KIND_NSGA2, request.model_dump_json())
    return OptimizeJobResponse(job_id=record.job_id)


@router.post("/api/optimize/trade-study", response_model=OptimizeJobResponse)
def start_trade_study(request: TradeStudyRequest) -> OptimizeJobResponse:
    """投递权衡研究作业（§14；方案数 > 20 当场 422，不进作业）。"""
    _reject_hard_violations(request.vehicle)
    validate_variant_count(request.variant_count)
    record = get_compute_runner().submit_optimize(_KIND_TRADE, request.model_dump_json())
    return OptimizeJobResponse(job_id=record.job_id)


@router.post("/api/optimize/sweep", response_model=OptimizeJobResponse)
def start_sweep(request: SweepRequest) -> OptimizeJobResponse:
    """投递批量扫描作业（§14；轴数 / 范围 / 组合数超限当场 422，不进作业）。"""
    _reject_hard_violations(request.vehicle)
    parse_axes(request.vehicle, request.axes)
    record = get_compute_runner().submit_optimize(_KIND_SWEEP, request.model_dump_json())
    return OptimizeJobResponse(job_id=record.job_id)


@router.post("/api/optimize/inverse", response_model=OptimizeJobResponse)
def start_inverse(request: InverseRequest) -> OptimizeJobResponse:
    """投递逆向设计作业（§14；目标不可达时作业终态 FAILED，``OPTIMIZE_INFEASIBLE``）。"""
    _reject_hard_violations(request.vehicle)
    record = get_compute_runner().submit_optimize(_KIND_INVERSE, request.model_dump_json())
    return OptimizeJobResponse(job_id=record.job_id)


__all__ = [
    "OptimizeJobResponse",
    "router",
    "start_inverse",
    "start_nsga2",
    "start_sweep",
    "start_trade_study",
]
