"""各轨道点值运力（OI-38 / §8.6 / §8.8，M4 计算内核第三片）。

问题与算法（运力反推 = 定尺的逆问题）
------------------------------------
§8.5 的 ``solve`` 是「给定载荷与目标 ΔV 反求 GLOW 与级质量」（设计）；
本模块是「**火箭已定**（各级 m_prop / m_dry 固定），给定目标 ΔV 需求反求载荷」
（分析，OI-38）。固定火箭的质量账口径：

- 推进剂质量 = :func:`aeroforge.perf.mass.propellant_mass_kg`（几何解析，显式
  箱长优先——与 solver 助推器兜底账同源）；
- 干重 = ``m_prop × σ/(1−σ)``（σ 是存储权威，§6.1）；
- 0 级段（带助推器构型）经 :func:`aeroforge.params.staging.resolve_zero_stage`
  取 ``Isp_eff`` 与流量，段内账与 solver ``_make_inner`` 同式——**不另写第二套
  齐氏账**（芯级推进剂跨段连续核算、助推器独立核算，§8.5）。

载荷 ``P`` 是 ``ΣΔV(P)`` 的**严格单调减函数**（载荷越重、各级质量比越差），故用
二分法求 ``ΣΔV(P) = dv_req``：

```text
bracket：lo = 0（ΣΔV 最大端），hi 自用户载荷起指数×2 扩张至 ΣΔV(hi) < dv_req
bisect ：中点求值 → 按 ΣΔV(mid) 与 dv_req 的大小关系收缩区间
收敛 ：载荷区间宽 ≤ 1e-9·max(1, hi) kg（或 200 次上限；二分 200 次的区间
        收缩因子 2⁻²⁰⁰ 远超双精度，实际 ~60 次到机器精度地板）
```

ΔV 需求来源（§8.6 约束 3：禁止与发射场无关的常数）
----------------------------------------------------
- 默认：:func:`aeroforge.perf.losses.orbit_dv_requirement`（§8.6 表区间中值，
  量级锚定、非权威）；
- 用户覆写：``Mission.loss_factors`` 给出（非 None）时，四项损失按用户份额
  （× 理想 ΔV）计，需求 = ``理想 ΔV + Σ损失 − 自转加成``（与 ΔV 瀑布同口径，
  §8.8），``dv_source`` 标「Mission 用户输入」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, Field

from aeroforge.errors import PerfError
from aeroforge.params import staging
from aeroforge.params.dag import G0
from aeroforge.params.schema import LaunchSite, Vehicle
from aeroforge.perf import mass as mass_module
from aeroforge.perf.losses import (
    CAPACITY_ORBITS,
    DV_SOURCE_ANCHORED,
    ideal_orbit_dv_km_s,
    orbit_dv_requirement,
    rotation_assist_km_s,
)
from aeroforge.perf.solver import _booster_ledger, _stage_inputs

#: 二分迭代上限（200 次的收缩因子 2⁻²⁰⁰ 远超双精度，实际 ~60 次收敛）。
BISECT_MAX_ITERATIONS = 200

#: 载荷收敛容差 [kg]：区间宽 ≤ 该值 × max(1, hi) 即收敛。
PAYLOAD_TOLERANCE_KG = 1e-9

#: bracket 扩张的上限 [kg]（防病态输入下无限扩张；正常解远小于该值）。
_PAYLOAD_BRACKET_CAP_KG = 1e15


class OrbitPayload(BaseModel):
    """运力表中一个目标轨道的点值（OI-38 / §8.8 ``payload_by_orbit`` 行）。"""

    payload_kg: float = Field(description="该目标轨道的反推载荷点值（kg）")
    dv_used_km_s: float = Field(description="反推所用 ΔV 需求（km/s，含损失与纬度依赖）")
    dv_source: str = Field(
        description="需求来源：量级锚定（§8.6 表中值）/ Mission 用户输入（loss_factors）"
    )
    attainable: bool = Field(
        default=True,
        description="构型可达该目标与否；False 时需求超出零载荷可达上限、运力记 0",
    )


@dataclass(frozen=True, slots=True)
class StageMasses:
    """固定火箭一级的质量行（几何解析推进剂 + σ 派生干重）。"""

    index: int
    isp_vacuum_s: float
    isp_source: str
    m_propellant_kg: float
    m_dry_kg: float


@dataclass(frozen=True, slots=True)
class ZeroStageMasses:
    """0 级段（§8.5 合并规则）：段比冲与流量取 staging，助推器质量独立核算。"""

    isp_eff_s: float
    booster_propellant_kg: float
    booster_dry_kg: float
    booster_mass_flow_kg_s: float
    core_mass_flow_kg_s: float


@dataclass(frozen=True, slots=True)
class FixedVehicleLedger:
    """固定火箭的质量账与正向 ΣΔV 求值（载荷为唯一变量）。

    :meth:`total_delta_v_m_s` 与 solver 的级链同式：无助推器时逐级
    ``ΔV_i = Isp_i·g₀·ln(m₀/m_f)`` 后抛干重；有助推器时先算 0 级段（段时长 =
    助推器推进剂 / 仅助推器流量，芯级段内烧 ``ṁ_core·t₀`` 封顶于芯一级贮量），
    再算芯级段余量与上面级——**不另写第二套齐氏账**。
    """

    stages: tuple[StageMasses, ...]
    zero_stage: ZeroStageMasses | None

    def glow_kg(self, payload_kg: float) -> float:
        """起飞质量 [kg] = 载荷 + 全部级质量（含助推器推进剂与干重）。"""
        total = payload_kg + sum(s.m_propellant_kg + s.m_dry_kg for s in self.stages)
        if self.zero_stage is not None:
            total += self.zero_stage.booster_propellant_kg + self.zero_stage.booster_dry_kg
        return total

    def propellant_masses_by_index(self) -> dict[int, float]:
        """各级推进剂质量（级号 → kg；供 DAG 传播 TWR / GLOW 等整箭派生量）。"""
        return {s.index: s.m_propellant_kg for s in self.stages}

    def max_delta_v_m_s(self) -> float:
        """零载荷（纯结构）的 ΣΔV 上限——超出该值的目标不可达。"""
        return self.total_delta_v_m_s(0.0)

    def total_delta_v_m_s(self, payload_kg: float) -> float:
        """给定载荷的正向 ΣΔV [m/s]（真空口径；载荷越大值越小，严格单调减）。"""
        mass = self.glow_kg(payload_kg)
        total = 0.0
        stages = self.stages
        first = stages[0]
        if self.zero_stage is not None:
            zero = self.zero_stage
            burn_duration = zero.booster_propellant_kg / zero.booster_mass_flow_kg_s
            core_burn = min(zero.core_mass_flow_kg_s * burn_duration, first.m_propellant_kg)
            burnout = mass - zero.booster_propellant_kg - core_burn
            total += zero.isp_eff_s * G0 * math.log(mass / burnout)
            mass = burnout - zero.booster_dry_kg
            remaining = first.m_propellant_kg - core_burn
            total += first.isp_vacuum_s * G0 * math.log(mass / (mass - remaining))
            mass -= remaining + first.m_dry_kg
            rest = stages[1:]
        else:
            rest = stages
        for stage in rest:
            total += stage.isp_vacuum_s * G0 * math.log(mass / (mass - stage.m_propellant_kg))
            mass -= stage.m_propellant_kg + stage.m_dry_kg
        return total


def vehicle_ledger(vehicle: Vehicle) -> FixedVehicleLedger:
    """固定火箭质量账：Isp 经 DAG 解析（QA-1），推进剂几何解析、干重按 σ 派生。"""
    inputs = _stage_inputs(vehicle)  # 包内复用 solver 的 DAG Isp 解析（QA-1 唯一权威）
    schema_stages = sorted(vehicle.stages, key=lambda s: s.index)
    stages = tuple(
        StageMasses(
            index=item.index,
            isp_vacuum_s=item.isp_vacuum_s,
            isp_source=item.isp_source,
            m_propellant_kg=mass_module.propellant_mass_kg(schema_stage),
            m_dry_kg=mass_module.propellant_mass_kg(schema_stage) * item.sigma / (1.0 - item.sigma),
        )
        for item, schema_stage in zip(inputs, schema_stages, strict=True)
    )
    zero: ZeroStageMasses | None = None
    if vehicle.boosters:
        summary = staging.resolve_zero_stage(vehicle)
        if summary is None:  # pragma: no cover - 有助推器时 resolve 必不返回 None
            raise PerfError(
                "存在助推器但 0 级段派生返回空",
                suggestion="检查 boosters 配置（§8.5 / OI-36）",
            )
        ledger = _booster_ledger(vehicle, summary)  # 包内复用（显式箱长优先）
        if ledger.propellant_kg <= 0.0 or ledger.dry_kg <= 0.0:
            raise PerfError(
                "助推器推进剂 / 干重质量非正——无法构造 0 级段质量账",
                suggestion="检查助推器侧级的贮箱几何与加注比例（§8.5）",
            )
        zero = ZeroStageMasses(
            isp_eff_s=summary.isp_eff_s,
            booster_propellant_kg=ledger.propellant_kg,
            booster_dry_kg=ledger.dry_kg,
            booster_mass_flow_kg_s=summary.booster_mass_flow_kg_s,
            core_mass_flow_kg_s=summary.total_mass_flow_kg_s - summary.booster_mass_flow_kg_s,
        )
    return FixedVehicleLedger(stages=stages, zero_stage=zero)


def payload_for_dv(vehicle: Vehicle, dv_requirement_km_s: float) -> float:
    """载荷二分（OI-38）：求 ``ΣΔV(P) = dv_requirement_km_s`` 的载荷 P [kg]。

    单调性：P ↑ ⟹ 各级质量比 ↓ ⟹ ΣΔV ↓（严格单调减），二分唯一收敛。
    收敛容差：载荷区间宽 ≤ 1e-9·max(1, hi) kg（200 次上限）。零载荷的 ΣΔV
    上限低于需求时抛 :class:`PerfError`（构型对该目标不可达，不静默给 0）。
    """
    if not (dv_requirement_km_s > 0.0):
        raise PerfError(
            f"目标 ΔV 需求必须为正，收到 {dv_requirement_km_s} km/s",
            suggestion="ΔV 需求是运力反推的驱动量（如 LEO 约 9.4 km/s，§8.6 表）",
        )
    ledger = vehicle_ledger(vehicle)
    target_m_s = dv_requirement_km_s * 1000.0

    ceiling = ledger.max_delta_v_m_s()
    if ceiling < target_m_s:
        raise PerfError(
            f"目标 ΔV 需求 {dv_requirement_km_s:.3f} km/s 超出该构型零载荷可达上限 "
            f"{ceiling / 1000.0:.3f} km/s（Σ Isp·g₀·ln 质量比的结构极限）",
            suggestion="该构型无法直达此轨道：降低轨道需求、提高比冲或降低结构系数 σ",
        )

    def dv_of(payload_kg: float) -> float:
        return ledger.total_delta_v_m_s(payload_kg)

    lo, hi = 0.0, max(vehicle.payload_mass_kg, 1.0)
    while dv_of(hi) > target_m_s:
        lo = hi
        hi *= 2.0
        if hi > _PAYLOAD_BRACKET_CAP_KG:  # pragma: no cover - 病态防御（上限已验）
            raise PerfError(
                "运力二分的载荷上界扩张越界（需求接近结构极限，解不稳定）",
                suggestion="检查目标 ΔV 是否贴近构型可达上限",
            )
    for _ in range(BISECT_MAX_ITERATIONS):
        mid = 0.5 * (lo + hi)
        if mid <= lo or mid >= hi:  # 已到浮点分辨率地板
            break
        if dv_of(mid) > target_m_s:
            lo = mid
        else:
            hi = mid
        if hi - lo <= PAYLOAD_TOLERANCE_KG * max(1.0, hi):
            break
    return 0.5 * (lo + hi)


def _user_dv_requirement_km_s(vehicle: Vehicle, site: LaunchSite, orbit: str) -> tuple[float, str]:
    """用户覆写口径的需求：理想 ΔV + Σ(loss_factors 份额 × 理想 ΔV) − 自转加成。

    ``Mission.loss_factors`` 非 None 即用户接管全部四项（0 项按 Schema 语义
    「未计入」，不回落 L1 模型——M2 存储层的既定口径）。
    """
    mission = vehicle.mission
    factors = mission.loss_factors
    if factors is None:  # pragma: no cover - 调用方已保证非 None
        raise PerfError(
            "内部错误：用户覆写口径要求 loss_factors 非 None",
            suggestion="这是实现缺陷，请提交 issue 附复现参数",
        )
    ideal = ideal_orbit_dv_km_s(orbit, mission)
    losses = (factors.gravity + factors.drag + factors.steering + factors.back_pressure) * ideal
    assist = rotation_assist_km_s(
        site.latitude_deg, site.altitude_m, site.azimuth_deg, mission.inclination_deg
    )
    return ideal + losses - assist, "Mission 用户输入（loss_factors 份额 × 理想 ΔV）"


def payload_by_orbit(vehicle: Vehicle, site: LaunchSite) -> dict[str, OrbitPayload]:
    """各轨道点值运力表（OI-38）：LEO / SSO / GTO / GEO（直送）四目标各反推一次。

    每项带所用 ΔV 需求值与来源（量级锚定 / Mission 用户输入，§8.8）；构型对
    某目标不可达时该项 ``payload_kg=0``、``attainable=False``（表保持四行齐备，
    由调用方按 attainable 汇总 warning，不在表内丢行）。
    """
    table: dict[str, OrbitPayload] = {}
    user_override = vehicle.mission.loss_factors is not None
    for orbit in CAPACITY_ORBITS:
        if user_override:
            dv_used, source = _user_dv_requirement_km_s(vehicle, site, orbit)
        else:
            requirement = orbit_dv_requirement(orbit, site)
            dv_used, source = requirement.value_km_s, requirement.source
        try:
            payload = payload_for_dv(vehicle, dv_used)
        except PerfError:
            table[orbit] = OrbitPayload(
                payload_kg=0.0,
                dv_used_km_s=dv_used,
                dv_source=source if user_override else DV_SOURCE_ANCHORED,
                attainable=False,
            )
        else:
            table[orbit] = OrbitPayload(payload_kg=payload, dv_used_km_s=dv_used, dv_source=source)
    return table


__all__ = [
    "BISECT_MAX_ITERATIONS",
    "CAPACITY_ORBITS",
    "PAYLOAD_TOLERANCE_KG",
    "FixedVehicleLedger",
    "OrbitPayload",
    "StageMasses",
    "ZeroStageMasses",
    "payload_by_orbit",
    "payload_for_dv",
    "vehicle_ledger",
]
