"""不确定度域端点（规格 §8.7 / §10.1，M4 第四片）。

- ``POST /api/uncertainty/mc`` —— Monte Carlo 区间（**异步**：返回 ``job_id``，
  结果经既有作业通道 ``GET /api/jobs/{id}`` / ``/ws/jobs/{id}`` 取回）。

独立触发（不依赖 ``/api/perf/evaluate`` 的自动投递）：显式给样本数 / 种子时用
本端点——例如复现某次区间（同 seed 同结果，逐字节一致）或加大样本量。

结果（阶段②，``JobRecord.metrics``）契约（OI-25 定死）::

    {"interval":  {"leo_kg": {"p5":…, "p50":…, "p95":…}, "gto_kg": {…}, "glow_kg": {…}, …},
     "moments":   {"leo_kg": {"n": 10000, "estimator": "adjusted_sample",
                              "skewness": -0.18, "mean_vs_p50_note": null}, …},
     "sensitivity": [{"param": "stages[0].structure_coefficient", "impact": 0.42}, … Top-8],
     "histogram": {"leo_kg": {"bin_edges": […51], "counts": […50]}, …},
     "convergence": {"leo_kg": {"checkpoints": [1000, …], "p50": […], "drift_vs_final": […]}, …},
     "interval_pending": false, "mc_job_id": "…", "provenance": {…}}
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.api.deps import get_compute_runner
from aeroforge.errors import ParamsError
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Mission, Vehicle
from aeroforge.perf.mc import (
    DEFAULT_SAMPLES,
    MAX_SAMPLES,
    MIN_SAMPLES,
)

router = APIRouter(tags=["uncertainty"])


class McRequest(BaseModel):
    """``POST /api/uncertainty/mc`` 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="飞行器参数（§6.1 全量；Mission 内嵌其中）")
    mission: Mission | None = Field(
        default=None,
        description="任务覆写（给出时替换 vehicle.mission——轨道 / 倾角 / 损失系数 / 发射场）",
    )
    samples: int = Field(
        default=DEFAULT_SAMPLES,
        ge=MIN_SAMPLES,
        le=MAX_SAMPLES,
        description=(
            f"样本数（§8.7：默认 {DEFAULT_SAMPLES}，可配 {MIN_SAMPLES}–{MAX_SAMPLES}，LHS）"
        ),
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        description="随机种子（复现性：同 seed 两跑逐字节一致；省略 = 飞行器哈希派生的确定性种子）",
    )


class McResponse(BaseModel):
    """``POST /api/uncertainty/mc`` 的响应体（异步受理回执）。"""

    job_id: str = Field(description="MC 作业 id：GET /api/jobs/{id} 轮询或 /ws/jobs/{id} 订阅")


@router.post("/api/uncertainty/mc", response_model=McResponse)
def submit_mc(request: McRequest) -> McResponse:
    """投递 Monte Carlo 区间作业（§8.7 / §10.1：异步——10 000 样本是秒级）。

    硬约束违反沿用参数域拒绝口径（``PARAMS_CONSTRAINT_VIOLATION`` → 422）；
    样本数越界由 Schema 值域拒绝（422）。同 ``(vehicle, samples, seed)`` 命中
    ``mc.json`` 缓存时作业立即成功（§9.2 内容寻址）。
    """
    vehicle = request.vehicle
    if request.mission is not None:
        vehicle = vehicle.model_copy(update={"mission": request.mission})
    violations = check_vehicle(vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝进入 Monte Carlo 评估",
            suggestion=hard[0].suggestion,
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )
    record = get_compute_runner().submit_mc(vehicle, samples=request.samples, seed=request.seed)
    return McResponse(job_id=record.job_id)


__all__ = [
    "McRequest",
    "McResponse",
    "router",
    "submit_mc",
]
