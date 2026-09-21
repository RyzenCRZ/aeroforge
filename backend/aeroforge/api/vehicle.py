"""整箭数据面板端点（规格 §11.5 FR-10 / §10.1，M5 第三片）。

``POST /api/vehicle/summary`` —— 右栏「整箭数据 + 轨道运力」面板的唯一数据源：
总质量（kg 与 t 双字段）、推进剂总质量、干重（含助推器干重）、总高（含整流罩）、
箭体最大直径（含助推器包络若有）、整流罩直径、轨道运力摘要。

纪律（§11.5 / ADR-011）：
- **数值一律来自后端 calculatedData**——前端只格式化不换算（kg→t 在本端点完成）；
- 质量账与 ``/api/perf/evaluate`` **同源**（:func:`vehicle_ledger`：几何解析
  推进剂 + σ 派生干重 + 0 级段助推器账）——面板 GLOW 与 evaluate 的
  ``point.glow_kg`` 逐位一致是验收判据；
- 高度 / 直径与 sections 端点、装配树同口径（:func:`vehicle_core_height_m`
  单一实现；助推器包络复用 :func:`booster_axis_radius` 的定位公式）；
- 轨道运力**复用 capacity 不另算**（四轨道表 = evaluate ``point.payload_by_orbit``
  的同一张表；当前 Mission 目标轨道点值 = 表内对应行，表外轨道如实给 null
  并附 warning——TLI / TMI 随 M6 轨道层交付）。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.errors import GeometryError, ParamsError
from aeroforge.geometry.assembly import AssemblyError, plan_stage, vehicle_core_height_m
from aeroforge.geometry.bundle import booster_axis_radius
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Mission, Vehicle
from aeroforge.perf.capacity import OrbitPayload, payload_by_orbit, vehicle_ledger
from aeroforge.perf.evaluate import resolve_site

router = APIRouter(tags=["vehicle"])


class VehicleSummaryRequest(BaseModel):
    """``POST /api/vehicle/summary`` 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="飞行器参数（面板质量 / 几何 / 运力的唯一输入）")
    mission: Mission | None = Field(
        default=None,
        description=(
            "运力摘要的轨道上下文覆写（试算不同目标轨道而不改车辆参数）；"
            "省略 = 使用 vehicle.mission（质量与几何量不受本字段影响）"
        ),
    )


class OrbitCapacitySummary(BaseModel):
    """轨道运力摘要（FR-11：当前目标轨道点值 + 七目标运力表）。"""

    target_orbit: str = Field(description="当前 Mission 的目标轨道类型（§6.1 OrbitType）")
    target_payload_kg: float | None = Field(
        description=(
            "当前目标轨道的运力点值（kg）；七目标表（LEO/SSO/GTO/GEO/TLI/TMI/"
            "GEO_GTO_CIRC）覆盖内取表行，表外轨道（escape/custom）为 null"
        ),
    )
    target_attainable: bool | None = Field(
        description="当前目标轨道是否可达（表外轨道为 null）；不可达时点值记 0"
    )
    payload_by_orbit: dict[str, OrbitPayload] = Field(
        description="七目标点值运力表（与 /api/perf/evaluate 的 point.payload_by_orbit 同源同值）"
    )


class VehicleSummaryResponse(BaseModel):
    """整箭数据面板响应体（数值字段一律带单位后缀 _kg / _m / _t，FR-10）。"""

    total_mass_kg: float = Field(description="总质量 / GLOW（kg = 载荷 + 全部级 + 助推器）")
    total_mass_t: float = Field(description="总质量（t；kg→t 换算由后端完成，前端零换算）")
    propellant_mass_kg: float = Field(description="推进剂总质量（kg，含助推器推进剂）")
    dry_mass_kg: float = Field(description="干重（kg，含助推器干重——0 级段账）")
    total_height_m: float = Field(description="总高（m，含整流罩 / 适配器；不含助推器）")
    body_max_diameter_m: float = Field(
        description="箭体最大直径（m，含助推器包络若有；整流罩直径单列不并入）"
    )
    booster_envelope_diameter_m: float | None = Field(
        default=None,
        description=(
            "助推器包络直径（m = 芯级全剖面最大半径〔含整流罩〕+ 助推器直径 + 间隙的包络）；"
            "无助推器为 null"
        ),
    )
    fairing_diameter_m: float | None = Field(description="整流罩直径（m；无整流罩为 null）")
    capacity: OrbitCapacitySummary = Field(description="轨道运力摘要（FR-11）")
    warnings: tuple[str, ...] = Field(description="面板相关警告（发射场缺省 / 目标轨道表外等）")
    provenance: dict[str, str] = Field(description="口径与来源声明（§1.4-4 溯源红线）")


@router.post("/api/vehicle/summary", response_model=VehicleSummaryResponse)
def vehicle_summary(request: VehicleSummaryRequest) -> VehicleSummaryResponse:
    """整箭数据面板（FR-10 / FR-11）：质量 / 几何 / 运力一次下发，全部后端算出。

    校验失败走既有错误体系：参数硬约束 → 422 ``PARAMS_CONSTRAINT_VIOLATION``
    （与 evaluate 同判据）；布局不可行（plan_stage 纯数值复核）→ 422
    ``GEOMETRY_INVALID``（与 sections 同判据）。
    """
    vehicle = request.vehicle

    violations = check_vehicle(vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝生成整箭数据",
            suggestion=hard[0].suggestion,
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )

    # ── 质量账（与 evaluate 同源：GLOW 逐位一致） ──
    ledger = vehicle_ledger(vehicle)
    propellant_total = sum(stage.m_propellant_kg for stage in ledger.stages)
    dry_total = sum(stage.m_dry_kg for stage in ledger.stages)
    if ledger.zero_stage is not None:
        propellant_total += ledger.zero_stage.booster_propellant_kg
        dry_total += ledger.zero_stage.booster_dry_kg
    glow = ledger.glow_kg(vehicle.payload_mass_kg)

    # ── 几何量（纯数值，无 OCCT；高度 / 直径与 sections / 装配树同源） ──
    try:
        total_height = vehicle_core_height_m(vehicle)
        core_max_radius = 0.0
        for stage in sorted(vehicle.stages, key=lambda item: item.index):
            for band in plan_stage(stage).bands:
                core_max_radius = max(core_max_radius, band.radius_start, band.radius_end)
    except AssemblyError as exc:
        raise GeometryError(
            str(exc),
            suggestion="分区必须恰好铺满级长且封头不干涉（§5.9 / §5.5）；"
            "请调整级长、扁度或发动机高度",
        ) from exc

    # 助推器包络（与装配树放置同公式：定位半径含整流罩段的最大半径，OI-36 ④）
    booster_envelope: float | None = None
    if vehicle.boosters:
        placement_radius = core_max_radius
        if vehicle.fairing_diameter_m is not None:
            placement_radius = max(placement_radius, vehicle.fairing_diameter_m / 2.0)
        for group in vehicle.boosters:
            diameter = group.stage.diameter_m
            envelope = 2.0 * (booster_axis_radius(placement_radius, diameter) + diameter / 2.0)
            booster_envelope = (
                envelope if booster_envelope is None else max(booster_envelope, envelope)
            )

    body_max_diameter = core_max_radius * 2.0
    if booster_envelope is not None:
        body_max_diameter = max(body_max_diameter, booster_envelope)

    # ── 轨道运力（复用 capacity；mission 覆写只作用于运力上下文） ──
    warnings: list[str] = []
    capacity_vehicle = (
        vehicle
        if request.mission is None
        else vehicle.model_copy(update={"mission": request.mission})
    )
    site = resolve_site(capacity_vehicle, warnings)
    mission = capacity_vehicle.mission
    table = payload_by_orbit(capacity_vehicle, site)
    for orbit, row in table.items():
        if not row.attainable:
            warnings.append(
                f"目标 {orbit} 的 ΔV 需求 {row.dv_used_km_s:.2f} km/s 超出该构型"
                "零载荷可达上限——运力记 0（attainable=false）"
            )
    target_row: OrbitPayload | None = table.get(mission.orbit_type)
    if target_row is None:
        warnings.append(
            f"目标轨道 {mission.orbit_type} 不在七目标运力表（LEO/SSO/GTO/GEO/"
            "TLI/TMI/GEO_GTO_CIRC）覆盖内——当前目标点值为 null（单点解析走 "
            "POST /api/orbits/transfer）"
        )

    provenance = {
        "mass_ledger": (
            "固定火箭质量账（perf.capacity.vehicle_ledger，与 /api/perf/evaluate 同源）："
            "几何解析推进剂（显式箱长优先）+ σ 派生干重（σ 为存储权威，§6.1）+ 0 级段助推器账"
        ),
        "total_height_m": "Σ 级装配高 + 顶级适配器 + 整流罩（§5.9 分区，与 sections/装配树同源）",
        "body_max_diameter_m": (
            "芯级最大直径（分区带半径 ×2，整流罩单列）；含助推器构型取与包络的较大值——"
            "包络 = 芯级全剖面最大半径（含整流罩段）+ 助推器直径 + 0.1 m 间隙（OI-36 ④ 放置公式）"
        ),
        "capacity": (
            "载荷二分反推（perf.capacity.payload_by_orbit，复用 evaluate 的 point 同表不另算）；"
            "ΔV 需求 = §8.6 锚定（或 Mission 用户覆写）"
        ),
        "mission_override": (
            "运力上下文使用请求 mission 覆写"
            if request.mission is not None
            else "运力上下文 = vehicle.mission"
        ),
    }

    return VehicleSummaryResponse(
        total_mass_kg=glow,
        total_mass_t=glow / 1000.0,
        propellant_mass_kg=propellant_total,
        dry_mass_kg=dry_total,
        total_height_m=total_height,
        body_max_diameter_m=body_max_diameter,
        booster_envelope_diameter_m=booster_envelope,
        fairing_diameter_m=vehicle.fairing_diameter_m,
        capacity=OrbitCapacitySummary(
            target_orbit=mission.orbit_type,
            target_payload_kg=target_row.payload_kg if target_row is not None else None,
            target_attainable=target_row.attainable if target_row is not None else None,
            payload_by_orbit=table,
        ),
        warnings=tuple(warnings),
        provenance=provenance,
    )


__all__ = [
    "OrbitCapacitySummary",
    "VehicleSummaryRequest",
    "VehicleSummaryResponse",
    "router",
    "vehicle_summary",
]
