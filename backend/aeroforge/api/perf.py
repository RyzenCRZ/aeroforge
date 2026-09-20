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
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.api.deps import get_compute_runner, get_store
from aeroforge.cache.store import evaluate_cache_key
from aeroforge.errors import ParamsError, PerfError
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import LaunchSite, Vehicle
from aeroforge.perf.budget import DeltaVBudget, delta_v_budget
from aeroforge.perf.capacity import OrbitPayload, payload_by_orbit, vehicle_ledger
from aeroforge.perf.losses import (
    DEFAULT_LAUNCH_SITE,
    characteristic_energy_km2_s2,
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


class PointEvaluation(BaseModel):
    """点值结果（§8.7 阶段 ① 的 ``point`` 载荷）。"""

    payload_by_orbit: dict[str, OrbitPayload] = Field(
        description="各轨道点值运力表（OI-38：LEO / SSO / GTO / GEO 直送四目标）"
    )
    payload_mass_kg: float = Field(description="用户输入的载荷质量（kg，对照基准）")
    glow_kg: float = Field(description="固定火箭在用户载荷下的起飞质量（kg）")
    c3_km2_s2: float | None = Field(
        description=(
            "终态轨道特征能量 C3 = v∞²（km²/s²，束缚轨道为负）——"
            "TLI / TMI / 逃逸轨道必输出（OI-23），本片四目标均为束缚轨道"
        )
    )


class PerfEvaluateResponse(BaseModel):
    """``POST /api/perf/evaluate`` 的响应体（阶段①：点值 + MC 作业挂点）。"""

    point: PointEvaluation
    delta_v_budget: DeltaVBudget = Field(description="ΔV 瀑布（OI-23，逐项列全）")
    warnings: tuple[str, ...]
    provenance: dict[str, str]
    cache_hit: bool = Field(description="本次结果是否来自缓存命中（§9.2）")
    interval_pending: bool = Field(
        default=False,
        description=(
            "MC 区间是否在途（OI-25 阶段①标记）：true = 已投递 mc_job_id、区间待阶段②"
            "覆盖；mc=false 或区间已到时为 false——前端据此决定区间列占位态还是数字"
        ),
    )
    mc_job_id: str | None = Field(
        default=None,
        description=(
            "自动投递的 MC 作业 id（经 GET /api/jobs/{id} / WS 取回阶段②；mc=false 为 null）"
        ),
    )


def _resolve_site(vehicle: Vehicle, warnings: list[str]) -> LaunchSite:
    """发射场解析：Mission 内联优先；缺失按工程惯例默认场并 warning（§6.1 口径）。"""
    site = vehicle.mission.launch_site
    if site is not None:
        return site
    warnings.append(
        "Mission.launch_site 缺失（launch_site_id 引用解析随后续片）：自转加成与"
        "转向损失按默认发射场（卡纳维拉尔 28.5°N、向东）计算——工程惯例锚，"
        "补齐发射场可消除该项近似"
    )
    return DEFAULT_LAUNCH_SITE


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
        response = _compute_point_evaluation(vehicle)
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


def _compute_point_evaluation(vehicle: Vehicle) -> PerfEvaluateResponse:
    """阶段①点值链（§8.6 L1 损失 + OI-38 运力表 + OI-23 瀑布；毫秒级纯数值）。"""
    warnings: list[str] = []
    site = _resolve_site(vehicle, warnings)
    mission = vehicle.mission

    budget, budget_warnings = delta_v_budget(vehicle, site, mission)
    warnings.extend(budget_warnings)

    try:
        c3 = characteristic_energy_km2_s2(mission.orbit_type, mission)
    except PerfError:
        c3 = None

    table = payload_by_orbit(vehicle, site)
    for orbit, row in table.items():
        if not row.attainable:
            warnings.append(
                f"目标 {orbit} 的 ΔV 需求 {row.dv_used_km_s:.2f} km/s 超出该构型"
                "零载荷可达上限——运力记 0（attainable=false），考虑降低轨道需求"
                "或提高比冲 / 降低结构系数 σ"
            )

    ledger = vehicle_ledger(vehicle)
    glow = ledger.glow_kg(vehicle.payload_mass_kg)

    loss_state = (
        "Mission 用户覆写（份额 × 理想 ΔV；0 值项未计入）"
        if mission.loss_factors is not None
        else "L1 参数化经验模型（§8.6；系数工程惯例、非权威来源）"
    )
    provenance = {
        "payload_by_orbit.dv_source": (
            "§8.6 目标轨道 ΔV 需求表区间中值 + 发射场纬度线性插值"
            "（量级锚定、非权威——禁止当标准引用）"
            if mission.loss_factors is None
            else "Mission 用户输入（loss_factors 份额口径，§6.1）"
        ),
        "payload_by_orbit.solver": (
            "载荷二分：ΣΔV(载荷) 单调减，区间收敛 1e-9 kg（200 次上限）；"
            "级链与 0 级段口径复用 §8.5 求解器（不另写第二套齐氏账）"
        ),
        "payload_by_orbit.mass_ledger": (
            "固定火箭质量账：几何解析推进剂（perf.mass，显式箱长优先）"
            "+ σ 派生干重（σ 为存储权威，§6.1）"
        ),
        "delta_v_budget.ideal_dv": "轨道力学理论速度增量（WGS-84 常数，真空脉冲口径）",
        "delta_v_budget.losses": loss_state,
        "delta_v_budget.rotation_assist": (
            "ω·(R+海拔)·cos(纬度)·cos(方位角)·相容因子（ω、R 取 WGS-84；"
            "相容因子与转向损失同一失配量驱动，§8.6 约束 2）"
        ),
        "point.c3_km2_s2": "终态轨道 C3 = −μ/a（束缚为负；TMI 本片按 escape 口径近似）",
        "interval.two_stage": (
            "OI-25 两阶段契约：本响应为阶段①点值（interval_pending=true 时区间在途）；"
            "阶段②经作业通道下发 {interval, moments, sensitivity, …} 并整体覆盖同名键"
        ),
        "cache.key": (
            "sha256(canonical_json(vehicle) + spec_version)（§9.2；spec_version="
            "随版本演进，本片未递增——无几何语义变更）"
        ),
    }

    return PerfEvaluateResponse(
        point=PointEvaluation(
            payload_by_orbit=table,
            payload_mass_kg=vehicle.payload_mass_kg,
            glow_kg=glow,
            c3_km2_s2=c3,
        ),
        delta_v_budget=budget,
        warnings=tuple(warnings),
        provenance=provenance,
        cache_hit=False,
    )


__all__ = [
    "PerfEvaluateRequest",
    "PerfEvaluateResponse",
    "PointEvaluation",
    "evaluate_performance",
    "router",
]
