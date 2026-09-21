"""性能评估域端点（规格 §8.6 / §8.8 / §8.7 / §10.1，M4 第四片起含两阶段契约）。

- ``POST /api/perf/evaluate`` —— 各轨道点值运力表（OI-38 + §8.10 七目标）+
  ΔV 瀑布（OI-23）+ **两阶段契约的阶段①**（OI-25：点值同步返回，MC 区间后台作业）；
- ``POST /api/perf/latitude-curve`` —— 运力—纬度曲线（OI-23，M6 轨道层第一片）：
  固定其余参数、纬度 0–90° 采样逐点反推运力，同步毫秒级纯数值（不触作业体系）；
- ``POST /api/perf/trajectory`` —— L2 简化上升弹道积分（§8.6 L2，M6 轨道层第二片；
  **异步作业**：实测 RK4 积分耗时 F9 ≈76 ms / CZ-5 ≈153 ms / SV ≈272 ms，全部
  超出 50 ms 同步阈值——按 §9.1 惯例走作业体系，结果经 ``GET /api/jobs/{id}``
  下发，``metrics`` = 弹道结果载荷（损失四项分解 + 燃尽状态 + provenance）。

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

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.api.deps import get_compute_runner, get_store
from aeroforge.cache.store import evaluate_cache_key
from aeroforge.errors import ParamsError
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Vehicle
from aeroforge.perf.capacity import payload_latitude_curve
from aeroforge.perf.evaluate import (
    PerfEvaluateResponse,
    PointEvaluation,
    compute_point_evaluation,
    resolve_site,
)
from aeroforge.perf.mc import DEFAULT_SAMPLES
from aeroforge.perf.trajectory import (
    DEFAULT_DT_S,
    DT_MAX_S,
    DT_MIN_S,
    TrajectoryProgram,
)

router = APIRouter(tags=["perf"])


class LatitudeCurvePoint(BaseModel):
    """纬度采样点（OI-23）。"""

    lat_deg: float = Field(description="发射场纬度（°）")
    payload_kg: float = Field(description="该纬度下的反推运力（kg；不可达记 0）")
    attainable: bool = Field(default=True, description="该纬度下目标是否可达")


class LatitudeCurveRequest(BaseModel):
    """``POST /api/perf/latitude-curve`` 的请求体（OI-23 运力—纬度曲线）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(
        description=(
            "飞行器参数（§6.1 全量）：曲线只替换发射场纬度，方位角 / 倾角 / 构型"
            "全部固定——「固定其余参数」的 FR-19 / OI-23 口径"
        )
    )
    orbit: str | None = Field(
        default=None,
        description=(
            "目标轨道（LEO/SSO/GTO/GEO/TLI/TMI/GEO_GTO_CIRC 之一）；缺省取 "
            "Mission.orbit_type（不在表内时 422）"
        ),
    )
    n_points: int = Field(
        default=11,
        ge=2,
        le=91,
        description="纬度采样点数（0–90° 均匀；缺省 11 点 = 步距 9°，OI-23 工程惯例）",
    )


class LatitudeCurveResponse(BaseModel):
    """``POST /api/perf/latitude-curve`` 的响应体（§8.8 ``payload_latitude_curve``）。"""

    payload_key: str = Field(description="运力量键（§8.8 形态：payload_<orbit>_kg）")
    orbit: str = Field(description="目标轨道")
    points: tuple[LatitudeCurvePoint, ...] = Field(description="纬度采样点（0–90° 均匀）")
    assumption: str = Field(description="采样口径与单调性判据说明")
    compute_ms: float = Field(description="本次曲线计算耗时（ms，同步链实测）")


@router.post("/api/perf/latitude-curve", response_model=LatitudeCurveResponse)
def latitude_curve(request: LatitudeCurveRequest) -> LatitudeCurveResponse:
    """运力—纬度曲线（OI-23；同步毫秒级——同一质量账 + 逐点二分，不触作业体系）。

    §16 M6 验收判据：曲线对纬度**单调不增**（FR-19 同型）——由测试显式断言
    （LEO / SSO 各一），本端点如实给值。耗时实测：质量账构建一次 + N 次载荷
    二分（纯数值），实测毫秒级（``compute_ms`` 随响应返回；若未来超 50 ms
    交互预算再议作业化——§9.1 的 >10 ms 红线只针对 async handler 内的
    OCCT/重计算，本端点为同步 def，FastAPI 自动入线程池不阻塞事件循环）。
    """
    from time import perf_counter

    orbit = request.orbit or str(request.vehicle.mission.orbit_type)
    started = perf_counter()
    curve = payload_latitude_curve(request.vehicle, orbit, n_points=request.n_points)
    compute_ms = (perf_counter() - started) * 1000.0
    return LatitudeCurveResponse(
        payload_key=curve.payload_key,
        orbit=curve.orbit,
        points=tuple(LatitudeCurvePoint.model_validate(p.model_dump()) for p in curve.points),
        assumption=curve.assumption,
        compute_ms=compute_ms,
    )


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
    dv_supply: Literal["anchored", "l2"] = Field(
        default="anchored",
        description=(
            "ΔV 需求供给模式（§8.6，M6 收官片）：anchored（默认）= 锚定表 + "
            "长燃时修正（毫秒级，缓存键与历史一致）；l2 = orbits 精算 ideal + "
            "L2 弹道积分四项损失（损失一阶冻结）− 自转加成（含一次 ~0.1 s 积分，"
            "物理升级路径）；Mission.loss_factors 用户覆写在两模式下都优先"
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
    key = evaluate_cache_key(vehicle, dv_supply=request.dv_supply)
    store = get_store()
    cached = store.load_evaluate(key)
    if cached is not None:
        response = PerfEvaluateResponse.model_validate({**cached, "cache_hit": True})
    else:
        response = compute_point_evaluation(vehicle, dv_supply=request.dv_supply)
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


class PerfTrajectoryRequest(BaseModel):
    """``POST /api/perf/trajectory`` 的请求体（§8.6 L2 + 程序参数覆盖）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(
        description=(
            "飞行器参数（§6.1 全量；Mission.launch_site 提供发射场，Vehicle.aero "
            "提供 Cd / 参考面积——缺失按工程惯例缺省并附 warning）"
        )
    )
    program: TrajectoryProgram = Field(
        default_factory=TrajectoryProgram,
        description=(
            "重力转弯程序参数（§8.6 L2 可调自由度；缺省 = 工程惯例剖面：垂直段 "
            "8 s → 指数标高 40 km 收敛到 0°）"
        ),
    )
    dt_s: float = Field(
        default=DEFAULT_DT_S,
        ge=DT_MIN_S,
        le=DT_MAX_S,
        description=f"RK4 固定步长 [s]（§8.6「0.1 s 级」；可配 {DT_MIN_S}–{DT_MAX_S}）",
    )


class PerfTrajectoryResponse(BaseModel):
    """``POST /api/perf/trajectory`` 的响应体（异步受理回执，形态随 ``/api/uncertainty/mc``）。"""

    job_id: str = Field(
        description="弹道积分作业 id：GET /api/jobs/{id} 轮询或 /ws/jobs/{id} 订阅；"
        "成功后 metrics = 弹道结果（losses_km_s 四项分解 / burnout 燃尽状态 / "
        "stage_timeline / provenance）"
    )


@router.post("/api/perf/trajectory", response_model=PerfTrajectoryResponse)
def run_trajectory(request: PerfTrajectoryRequest) -> PerfTrajectoryResponse:
    """投递 L2 简化上升弹道积分作业（§8.6 L2；异步——实测积分耗时超 50 ms 同步阈值）。

    点质量 2D 积分：ISA 1976 分层指数大气 + 球面地球（变重力 + 离心卸载）+
    重力转弯程序（γ 剖面 + 逆动力学攻角）→ **直接算出**四项损失（与 L1 同名
    对齐：gravity / aero / steering / back_pressure）。纯数值单发实测 F9 ≈76 ms
    / CZ-5 ≈153 ms / SV ≈272 ms（RK4 0.1 s 步长），全部 > 50 ms——按 §9.1 惯例
    走既有作业体系（计算侧执行器），本端点只做参数域硬约束检查 + 微秒级入队。
    程序参数荒谬 / TWR ≤ 0 / 积分发散（触地、超第二宇宙速度）在作业内报错，
    作业终态 FAILED（``TRAJECTORY_FAILED`` → 422，错误携带最后状态）。
    """
    violations = check_vehicle(request.vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝进入 L2 弹道积分",
            suggestion=hard[0].suggestion,
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )
    record = get_compute_runner().submit_trajectory(
        request.vehicle,
        request.program.model_dump_json(),
        dt_s=request.dt_s,
    )
    return PerfTrajectoryResponse(job_id=record.job_id)


__all__ = [
    "LatitudeCurvePoint",
    "LatitudeCurveRequest",
    "LatitudeCurveResponse",
    "PerfEvaluateRequest",
    "PerfEvaluateResponse",
    "PerfTrajectoryRequest",
    "PerfTrajectoryResponse",
    "PointEvaluation",
    "evaluate_performance",
    "latitude_curve",
    "resolve_site",
    "router",
    "run_trajectory",
]
