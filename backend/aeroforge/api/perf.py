"""性能评估域端点（规格 §8.6 / §8.8 / §8.7 / §10.1，M4 第四片起含两阶段契约）。

- ``POST /api/perf/evaluate`` —— 各轨道点值运力表（OI-38）+ ΔV 瀑布（OI-23）
  + **两阶段契约的阶段①**（OI-25：点值同步返回，MC 区间后台作业）。

两阶段契约（OI-25，第四片兑现）
------------------------------
MC 的 10 000 样本是秒级，而「调整发射场纬度 → 运力即时更新」要毫秒级——
故**显式分两段**，而不是把 MC 塞进交互路径后假装它很快：

- **阶段①（本端点，同步）**：``point`` 与 ``delta_v_budget`` 毫秒级返回；
  点值算完**自动投递** MC 作业（计算侧进程池，§9.1），响应标记
  ``interval_pending=true`` 并携带 ``mc_job_id``——前端据此把区间列显示为
  占位态（不是上一次的数字）；
- **阶段②（作业通道）**：MC 完成（默认 10 000 样本）后，结果经既有作业取回
  通道（``GET /api/jobs/{id}`` / ``/ws/jobs/{id}``）下发，``metrics`` 形态为
  ``{interval, moments, sensitivity, histogram, convergence, interval_pending:
  false, mc_job_id, provenance}``——整体覆盖同名键；
- **规则 3 机检**：阶段①的求解路径**不调用 MC**（MC 在协调线程与进程池）——
  测试钉住 evaluate 同步耗时 < 100 ms；
- 请求带 ``"mc": false`` 时不投递：``interval_pending=false``、无 ``mc_job_id``
  （批量 / 调参场景可关掉后台作业噪音）。

缓存（§9.2）：点值缓存命中与否**都同步返回**（evaluate 是毫秒级纯数值）；
自动投递的 MC 用飞行器哈希派生的**确定性种子**——同一构型重复评估直接命中
MC 结果缓存（``mc.json``），不重跑 10 000 样本。

点值链本体在 :mod:`aeroforge.perf.evaluate`（M5 第三片抽出）：导出报告与
整箭数据面板消费同一条链，本模块只保留端点装配（请求模型 + 缓存 + 投递）。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.api.deps import get_compute_runner, get_store
from aeroforge.cache.store import evaluate_cache_key
from aeroforge.errors import ParamsError
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Vehicle
from aeroforge.perf.evaluate import (
    PerfEvaluateResponse,
    PointEvaluation,
    compute_point_evaluation,
    resolve_site,
)
from aeroforge.perf.mc import DEFAULT_SAMPLES

router = APIRouter(tags=["perf"])


class PerfEvaluateRequest(BaseModel):
    """``POST /api/perf/evaluate`` 的请求体（形态随 ``/api/sizing/solve`` 惯例）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(
        description=(
            "飞行器参数（§6.1 全量；Mission 层的目标轨道 / 倾角 / 损失系数与 "
            "launch_site 内嵌其中——纬度是自转加成与转向损失的唯一输入，§8.6）"
        )
    )
    mc: bool = Field(
        default=True,
        description=(
            "是否自动投递 MC 区间作业（OI-25 阶段②）：true（默认）→ 响应带 "
            "interval_pending=true 与 mc_job_id；false → 不投递、无区间作业"
        ),
    )


@router.post("/api/perf/evaluate", response_model=PerfEvaluateResponse)
def evaluate_performance(request: PerfEvaluateRequest) -> PerfEvaluateResponse:
    """性能评估（§8.6 L1 损失 + OI-38 运力表 + OI-23 ΔV 瀑布；同步纯数值）。

    两阶段契约（OI-25）：点值同步返回后按 ``mc`` 开关自动投递 MC 作业（阶段②，
    计算侧进程池）——**同步段不调用 MC**（规则 3），投递只是微秒级入队。
    硬约束违反沿用参数域拒绝口径（``PARAMS_CONSTRAINT_VIOLATION`` → 422，与
    ``/api/sizing/solve`` 同判据）；评估域问题（轨道要素缺失 / 构型对目标 ΔV
    不可达）由 :class:`~aeroforge.errors.PerfError` 给出 422。缓存键 =
    sha256(canonical_json(vehicle) + spec_version)（§9.2），命中直接同步返回。
    """
    violations = check_vehicle(request.vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝进入性能评估",
            suggestion=hard[0].suggestion,
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )

    vehicle = request.vehicle
    key = evaluate_cache_key(vehicle)
    store = get_store()
    cached = store.load_evaluate(key)
    if cached is not None:
        response = PerfEvaluateResponse.model_validate({**cached, "cache_hit": True})
    else:
        response = compute_point_evaluation(vehicle)
        store.save_evaluate(
            key,
            response.model_dump(
                mode="json", exclude={"cache_hit", "interval_pending", "mc_job_id"}
            ),
        )

    if request.mc:
        record = get_compute_runner().submit_mc(vehicle, samples=DEFAULT_SAMPLES)
        response = response.model_copy(
            update={"interval_pending": True, "mc_job_id": record.job_id}
        )
    return response


__all__ = [
    "PerfEvaluateRequest",
    "PerfEvaluateResponse",
    "PointEvaluation",
    "evaluate_performance",
    "resolve_site",
    "router",
]
