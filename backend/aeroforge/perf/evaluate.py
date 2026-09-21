"""点值性能评估链（§8.6 / §8.8 / OI-23 / OI-38）——``/api/perf/evaluate`` 的计算本体。

从 :mod:`aeroforge.api.perf` 抽出（M5 第三片）：多格式导出的**性能 JSON 报告**
（§5.8「evaluate 输出原样」）与整箭数据面板的运力摘要都要消费**同一条**点值链——
留在 API 层会让几何导出模块反向 import API 层（分层倒置），复制一份则两处必然
漂移。故计算本体落位 perf 子包（纯数值、无 OCCT、无 FastAPI 依赖），API 层与
导出层都是它的消费者。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from aeroforge.errors import PerfError
from aeroforge.params.schema import LaunchSite, Vehicle
from aeroforge.perf.budget import DeltaVBudget, delta_v_budget
from aeroforge.perf.capacity import OrbitPayload, payload_by_orbit, vehicle_ledger
from aeroforge.perf.losses import DEFAULT_LAUNCH_SITE, characteristic_energy_km2_s2


class PointEvaluation(BaseModel):
    """点值结果（§8.7 阶段 ① 的 ``point`` 载荷）。"""

    payload_by_orbit: dict[str, OrbitPayload] = Field(
        description=(
            "各轨道点值运力表（OI-38 + §8.10：LEO / SSO / GTO / GEO 直送 + "
            "TLI / TMI / GEO（GTO+圆化，键 GEO_GTO_CIRC）七目标；TLI/TMI 行带 "
            "c3_km2_s2，TMI 行带 window_assumption）"
        )
    )
    payload_mass_kg: float = Field(description="用户输入的载荷质量（kg，对照基准）")
    glow_kg: float = Field(description="固定火箭在用户载荷下的起飞质量（kg）")
    c3_km2_s2: float | None = Field(
        description=(
            "终态轨道特征能量 C3 = v∞²（km²/s²，束缚轨道为负）——"
            "TLI / TMI / 逃逸轨道必输出（OI-23）；TMI 取典型值 11.5（§8.10 约束 4，"
            "窗口假设见运力表 TMI 行）"
        )
    )


class PerfEvaluateResponse(BaseModel):
    """点值评估响应体（阶段①：点值 + MC 作业挂点）。"""

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


def resolve_site(vehicle: Vehicle, warnings: list[str]) -> LaunchSite:
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


def compute_point_evaluation(
    vehicle: Vehicle, *, dv_supply: Literal["anchored", "l2"] = "anchored"
) -> PerfEvaluateResponse:
    """阶段①点值链（§8.6 运力表 + OI-23 瀑布；毫秒级纯数值；anchored 模式）。

    ``dv_supply="l2"``（M6 收官片）：运力表的 ΔV 需求供给切换为 orbits 精算
    ideal + L2 弹道积分四项损失（损失一阶冻结）− 自转加成——表内逐行
    ``dv_source`` 标注实际模式；ΔV 瀑布仍为 L1 物理分解（瀑布的「理论 ideal +
    L1 损失」与运力供给是两条口径，§8.8 既有纪律，不随供给模式切换）。
    l2 模式含一次弹道积分（~0.1 s），比 anchored 慢两个量级——调用方按需选用。
    """
    warnings: list[str] = []
    site = resolve_site(vehicle, warnings)
    mission = vehicle.mission

    budget, budget_warnings = delta_v_budget(vehicle, site, mission)
    warnings.extend(budget_warnings)

    try:
        c3 = characteristic_energy_km2_s2(mission.orbit_type, mission)
    except PerfError:
        c3 = None

    table = payload_by_orbit(vehicle, site, dv_supply=dv_supply)
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
    if mission.loss_factors is not None:
        dv_prov = "Mission 用户输入（loss_factors 份额口径，§6.1）"
    elif dv_supply == "l2":
        dv_prov = (
            "L2 弹道积分四项损失（§8.6 L2，参考载荷一阶冻结）+ perf.orbits 精算"
            "理想 ΔV（§8.10）− 自转加成（L1 口径）——M6 收官片物理供给路径"
        )
    else:
        dv_prov = (
            "§8.6 目标轨道 ΔV 需求表区间中值 + 发射场纬度线性插值"
            "（量级锚定、非权威——禁止当标准引用）"
        )
    composite_ascent = (
        "上升段（L2 口径，损失一阶冻结）"
        if dv_supply == "l2"
        else "上升段锚定（LEO 行口径，含损失与纬度依赖）"
    )
    provenance = {
        "payload_by_orbit.dv_source": dv_prov,
        "payload_by_orbit.composite_rows": (
            f"TLI / TMI / GEO_GTO_CIRC 复合行 = {composite_ascent}+ perf.orbits "
            "§8.10 解析机动（TLI/TMI 单脉冲射入——C3 与 ΔV 成对且同一组 μ/r_p；"
            "GTO 近地点点火 + 远地点圆化/平面变更矢量合成；射入/圆化脉冲恒为"
            "理想脉冲口径——L2 上升段损失只覆盖上升段，不重复建模射入段）"
        ),
        "payload_by_orbit.tmi_window": (
            "TMI 行 window_assumption 必填（§8.10 约束 4 / CON-04：C3 取典型值"
            " 8–15 km²/s² 区间中值 11.5 的跨窗口量级代表，窗口/相位假设随行——"
            "无窗口假设的 TMI 结果视为不可复现）"
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
        "point.c3_km2_s2": (
            "终态轨道 C3（束缚为负；escape=0、TMI 取典型值 11.5 km²/s²——"
            "窗口/相位假设见运力表 TMI 行 window_assumption，§8.10 约束 4）"
        ),
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
    "PerfEvaluateResponse",
    "PointEvaluation",
    "compute_point_evaluation",
    "resolve_site",
]
