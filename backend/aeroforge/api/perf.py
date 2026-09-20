"""性能评估域端点（规格 §8.6 / §8.8 / §10.1）。

- ``POST /api/perf/evaluate`` —— 各轨道点值运力表（OI-38）+ ΔV 瀑布（OI-23）。

同步化裁定（相对规格 §10.1「混合」类型的演进，由主线裁决）
----------------------------------------------------------
§10.1 原文「缓存命中同步返回，否则异步」是按 MC 10 000 样本（秒级）预估的形态；
本端点**不含 MC**（两阶段契约的阶段 ①，OI-25：`interval` / `interval_pending`
归第四片），纯数值链（损失 L1 + 载荷二分）实测毫秒级——故**命中与否都同步返回**，
缓存只省重算、不改变返回形态。测试钉住单次请求 < 500 ms（与 sizing 同预算）；
若后续实测超百毫秒量级，再按 §10.1 原文演进为异步（MC 才是真异步需求）。

响应不输出 ``interval`` / ``interval_pending``：那是 OI-25 两阶段契约的字段，
属第四片 MC；本片是纯点值（§8.7 阶段 ① 的载荷 ``point`` 与 ``delta_v_budget``）。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.api.deps import get_store
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
    """``POST /api/perf/evaluate`` 的响应体（点值，无 interval——归第四片 MC）。"""

    point: PointEvaluation
    delta_v_budget: DeltaVBudget = Field(description="ΔV 瀑布（OI-23，逐项列全）")
    warnings: tuple[str, ...]
    provenance: dict[str, str]
    cache_hit: bool = Field(description="本次结果是否来自缓存命中（§9.2）")


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
        return PerfEvaluateResponse.model_validate({**cached, "cache_hit": True})

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
        "cache.key": (
            "sha256(canonical_json(vehicle) + spec_version)（§9.2；spec_version="
            "随版本演进，本片未递增——无几何语义变更）"
        ),
    }

    response = PerfEvaluateResponse(
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
    store.save_evaluate(key, response.model_dump(mode="json", exclude={"cache_hit"}))
    return response


__all__ = [
    "PerfEvaluateRequest",
    "PerfEvaluateResponse",
    "PointEvaluation",
    "evaluate_performance",
    "router",
]
