"""各轨道点值运力（OI-38 / §8.6 / §8.8 / §8.10，M4 第三片 + M6 轨道层第一片）。

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
- 默认：:func:`anchored_dv_km_s` = :func:`aeroforge.perf.losses.orbit_dv_requirement`
  （§8.6 表区间中值 + 纬度插值，量级锚定、非权威）**+ 长燃时构型修正**——一级
  燃时超过 184 s 的构型（如 CZ-5 氢氧芯级 480 s）叠加
  :func:`aeroforge.perf.losses.long_burn_surcharge_km_s`（超长燃时的重力累积与
  大气内比冲折减，按 §13.2 三基准反标定）；典型燃时构型（≤184 s）附加恒为 0，
  锚定口径与既有断言不变；
- 用户覆写：``Mission.loss_factors`` 给出（非 None）时，四项损失按用户份额
  （× 理想 ΔV）计，需求 = ``理想 ΔV + Σ损失 − 自转加成``（与 ΔV 瀑布同口径，
  §8.8），``dv_source`` 标「Mission 用户输入」。

七目标运力表（M6 轨道层第一片，§8.10 / OI-22）
----------------------------------------------
锚定四目标（LEO / SSO / GTO / GEO 直送，OI-38）之外新增三行**复合需求**：
**TLI / TMI / GEO（GTO+圆化，键 ``GEO_GTO_CIRC``）**——需求 = 锚定上升段（LEO
行口径，含损失与纬度依赖）+ 停泊轨道上的解析机动（:mod:`aeroforge.perf.orbits`
闭式：TLI/TMI 单脉冲射入、GTO 近地点点火 + 远地点圆化/平面变更矢量合成）。
TLI / TMI 行带 ``c3_km2_s2``（§8.10 约束 1：禁止只给 ΔV），TMI 行另带必填的
``window_assumption``（约束 4：无窗口假设的 TMI 结果视为不可复现）。

运力—纬度曲线（OI-23，M6 验收判据）
------------------------------------
:func:`payload_latitude_curve`：给定构型与目标轨道，纬度 0–90° 均匀采样逐点
反推运力（复用同一质量账与二分链，不另写第二套）；§16 M6 判据要求曲线对纬度
**单调不增**（FR-19 同型，测试显式断言）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from aeroforge.errors import PerfError
from aeroforge.params import staging
from aeroforge.params.dag import G0
from aeroforge.params.schema import LaunchSite, Vehicle
from aeroforge.perf import mass as mass_module
from aeroforge.perf.losses import (
    CAPACITY_ORBITS,
    DEFAULT_LAUNCH_SITE,
    DEFAULT_LEO_ALT_M,
    DEFAULT_SSO_ALT_M,
    EARTH_EQUATOR_RADIUS_M,
    ideal_orbit_dv_km_s,
    long_burn_surcharge_km_s,
    orbit_dv_requirement,
    rotation_assist_km_s,
)
from aeroforge.perf.orbits import (
    GEO_RADIUS_M,
    PARKING_DEFAULT_ALT_M,
    circular_velocity_km_s,
    geo_via_gto_km_s,
    hohmann_transfer_km_s,
    parking_injection_km_s,
)
from aeroforge.perf.solver import _booster_ledger, _stage_inputs
from aeroforge.perf.trajectory import TrajectoryLosses, TrajectoryProgram, integrate_ascent

#: 二分迭代上限（200 次的收缩因子 2⁻²⁰⁰ 远超双精度，实际 ~60 次收敛）。
BISECT_MAX_ITERATIONS = 200

#: 载荷收敛容差 [kg]：区间宽 ≤ 该值 × max(1, hi) 即收敛。
PAYLOAD_TOLERANCE_KG = 1e-9

#: bracket 扩张的上限 [kg]（防病态输入下无限扩张；正常解远小于该值）。
_PAYLOAD_BRACKET_CAP_KG = 1e15

#: ΔV 需求供给模式（M6 收官片，§8.6 L2 接链）：
#: - ``anchored``（缺省）——锚定表 + 长燃时修正（M4 口径，缓存键与数字字节稳定）；
#: - ``l2``——orbits 精算理想 ΔV + L2 弹道积分四项损失 − 自转加成（物理升级路径）。
DvSupplyMode = Literal["anchored", "l2"]

#: L2 供给模式的标定程序参数（§13.2 三基准反标定，M6 收官片；标定记录见 §16.7）：
#: **单一自由度偏离工程惯例缺省**——指数标高 40 → 65 km；其余取工程惯例值
#: （垂直段 8 s、α 双饱和界 10°/30°）。标定过程（7 组候选参数的三基准扫描）：
#: H=40k 缺省下 CZ-5 / SV 积分触地发散（程序-构型失配）；H ∈ [55,80] 全部可积，
#: 其中 **H=65k 使 F9 −6.8% 与 SV −9.2% 同时带内 <10%**；CZ-5 在全部候选下
#: +19%~+83%（模板能力偏置，根因见 §16.7——非程序参数可消除）。物理依据：标高
#: 是 γ 剖面的转弯节奏——65 km 对应「上面积累速度前先爬升到稠密大气上界」的
#: 常规剖面，也匹配 SV 低 TWR 上面级（S-IVB TWR≈0.41）的跟踪能力边界。
L2_CALIBRATED_PROGRAM = TrajectoryProgram(scale_height_m=65_000.0)

#: 运力表全目标（M6 轨道层第一片扩至七行）：锚定四目标 + 复合三行。
#: ⚠ 时序链（perf.sequence）的单目标需求仍只走锚定四目标（CAPACITY_ORBITS）——
#: 复合行的「上升段 + 解析机动」拼合不进时序账，避免两处各拼一份。
PAYLOAD_ORBITS: tuple[str, ...] = (*CAPACITY_ORBITS, "TLI", "TMI", "GEO_GTO_CIRC")

#: GEO（GTO+圆化）行的表键（GEO 经 GTO + 远地点圆化/平面变更复合，§8.10）。
GEO_GTO_CIRC_ORBIT = "GEO_GTO_CIRC"


class OrbitPayload(BaseModel):
    """运力表中一个目标轨道的点值（OI-38 / §8.8 ``payload_by_orbit`` 行）。"""

    payload_kg: float = Field(description="该目标轨道的反推载荷点值（kg）")
    dv_used_km_s: float = Field(description="反推所用 ΔV 需求（km/s，含损失与纬度依赖）")
    dv_source: str = Field(
        description=(
            "需求来源：量级锚定（§8.6 表中值）/ Mission 用户输入（loss_factors）"
            "；复合行（TLI/TMI/GEO_GTO_CIRC）= 上升段锚定 + §8.10 轨道解析拼合"
        )
    )
    attainable: bool = Field(
        default=True,
        description="构型可达该目标与否；False 时需求超出零载荷可达上限、运力记 0",
    )
    c3_km2_s2: float | None = Field(
        default=None,
        description=(
            "特征能量 C3 = v∞²（km²/s²，OI-22：TLI 为负、TMI 典型 8–15）——"
            "TLI / TMI 行必填（§8.10 约束 1：禁止只给 ΔV），其余行为 null"
        ),
    )
    window_assumption: str | None = Field(
        default=None,
        description=(
            "窗口/相位假设（§8.10 约束 4）：TMI 行必填（无窗口假设的 TMI 结果视为"
            "不可复现，CON-04 同口径），其余行为 null"
        ),
    )


class PayloadLatitudePoint(BaseModel):
    """运力—纬度曲线上的一个采样点（OI-23 / §8.8 ``payload_latitude_curve``）。"""

    lat_deg: float = Field(description="发射场纬度（°，0–90）")
    payload_kg: float = Field(description="该纬度下的反推运力（kg；不可达记 0）")
    attainable: bool = Field(default=True, description="该纬度下目标是否可达")


class PayloadLatitudeCurve(BaseModel):
    """运力—纬度曲线（OI-23）：固定其余参数，纬度采样 × 逐点运力反推。

    M6 验收判据（§16）：曲线对纬度**单调不增**——由测试显式断言（LEO / SSO 各一）。
    """

    payload_key: str = Field(description="运力量键（§8.8 形态：payload_<orbit>_kg）")
    orbit: str = Field(description="目标轨道（PAYLOAD_ORBITS 之一）")
    points: tuple[PayloadLatitudePoint, ...] = Field(description="纬度采样点（0–90° 均匀）")
    assumption: str = Field(description="采样口径与单调性判据说明")


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


def payload_for_dv_on_ledger(
    ledger: FixedVehicleLedger,
    dv_requirement_km_s: float,
    *,
    bracket_hint_kg: float = 1.0,
) -> float:
    """账本级载荷二分：与 :func:`payload_for_dv` 同算法，但复用已构建的质量账。

    存在意义（§8.7 / §8.9）：MC 每样本与任务时序的回收代价核算都要对**同一份
    账本**做多次（四轨道 / 有无预留）反推——逐次重建 ``vehicle_ledger`` 会把
    几何解析账白白重算四遍。二分本身只消费 ``total_delta_v_m_s``，与账本来源
    （几何解析 / 求解器输出）解耦。需求超出零载荷上限时抛 :class:`PerfError`。
    """
    if not (dv_requirement_km_s > 0.0):
        raise PerfError(
            f"目标 ΔV 需求必须为正，收到 {dv_requirement_km_s} km/s",
            suggestion="ΔV 需求是运力反推的驱动量（如 LEO 约 9.4 km/s，§8.6 表）",
        )
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

    lo, hi = 0.0, max(bracket_hint_kg, 1.0)
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


def payload_for_dv(vehicle: Vehicle, dv_requirement_km_s: float) -> float:
    """载荷二分（OI-38）：求 ``ΣΔV(P) = dv_requirement_km_s`` 的载荷 P [kg]。

    单调性：P ↑ ⟹ 各级质量比 ↓ ⟹ ΣΔV ↓（严格单调减），二分唯一收敛。
    收敛容差：载荷区间宽 ≤ 1e-9·max(1, hi) kg（200 次上限）。零载荷的 ΣΔV
    上限低于需求时抛 :class:`PerfError`（构型对该目标不可达，不静默给 0）。
    """
    return payload_for_dv_on_ledger(
        vehicle_ledger(vehicle),
        dv_requirement_km_s,
        bracket_hint_kg=vehicle.payload_mass_kg,
    )


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


def first_stage_burn_time_s(vehicle: Vehicle, m_prop_first_kg: float) -> float:
    """一级燃时 [s]：显式 ``Stage.burn_time_s`` 优先，缺省按 m_prop/ṁ 派生（海平面口径）。

    原实现位于 :mod:`aeroforge.perf.budget`；因运力表的锚定 + 长燃时附加口径
    （:func:`anchored_dv_km_s`）与 ΔV 瀑布都要用同一燃时，且 budget 依赖本模块
    （不得反向 import），函数移驻于此——**单一来源，两处共用不漂移**。
    """
    first = sorted(vehicle.stages, key=lambda s: s.index)[0]
    if first.burn_time_s is not None:
        return first.burn_time_s
    engine = first.engine
    mass_flow = first.engine_count * engine.thrust_sea_level_n / (engine.isp_sea_level_s * G0)
    return m_prop_first_kg / mass_flow


def _liftoff_twr(vehicle: Vehicle, glow_kg: float) -> float:
    """整箭起飞推重比：芯一级 + 全部助推器的海平面推力 / (GLOW·g₀)。

    助推器推力必须计入（并联构型的起飞推力主体在助推器——DAG 的
    ``vehicle.twr_liftoff`` 不建助推器节点，对 CZ-5 这类构型会给出 0.5 以下的
    失真值，不得用于损失层）。
    """
    first = sorted(vehicle.stages, key=lambda s: s.index)[0]
    thrust_n = first.engine_count * first.engine.thrust_sea_level_n
    for booster in vehicle.boosters:
        thrust_n += (
            booster.count * booster.stage.engine_count * booster.stage.engine.thrust_sea_level_n
        )
    return thrust_n / (glow_kg * G0)


def anchored_dv_km_s(
    vehicle: Vehicle, ledger: FixedVehicleLedger, orbit: str, site: LaunchSite
) -> tuple[float, str, str]:
    """锚定 + 构型修正的 ΔV 需求（运力表默认口径）：``(dv_km_s, source, assumption)``。

    - 基值：:func:`aeroforge.perf.losses.orbit_dv_requirement`（§8.6 表中值 +
      发射场纬度插值，量级锚定）——典型构型（一级燃时 ≤184 s）到此为止，
      ``source`` 即锚定文案（既有口径与精确断言不变）；
    - 附加：一级燃时超过 184 s 的构型叠加
      :func:`aeroforge.perf.losses.long_burn_surcharge_km_s`（超长燃时的重力累积
      与大气内比冲折减，按 §13.2 三基准反标定）——``source`` 在锚定文案上追加
      附加项声明，``assumption`` 供调用方写入溯源账。
    """
    requirement = orbit_dv_requirement(orbit, site)
    burn_time_s = first_stage_burn_time_s(vehicle, ledger.stages[0].m_propellant_kg)
    twr = _liftoff_twr(vehicle, ledger.glow_kg(vehicle.payload_mass_kg))
    surcharge = long_burn_surcharge_km_s(twr, burn_time_s)
    if surcharge.value_km_s <= 0.0:
        return requirement.value_km_s, requirement.source, surcharge.assumption
    source = (
        f"{requirement.source} + L1 长燃时构型修正（+{surcharge.value_km_s:.2f} km/s，"
        "按 §13.2 三基准反标定）"
    )
    return requirement.value_km_s + surcharge.value_km_s, source, surcharge.assumption


def _ascent_requirement_km_s(
    vehicle: Vehicle, site: LaunchSite, ledger: FixedVehicleLedger
) -> tuple[float, str]:
    """上升段需求（LEO 行口径）：用户覆写 / 锚定 + 长燃时修正——复合行的公共底座。"""
    if vehicle.mission.loss_factors is not None:
        return _user_dv_requirement_km_s(vehicle, site, "LEO")
    dv_used, source, _ = anchored_dv_km_s(vehicle, ledger, "LEO", site)
    return dv_used, source


# ---------------------------------------------------------------------------
# L2 供给模式（M6 收官片，§8.6 L2 接链）：orbits 精算 ideal + L2 损失 − 自转加成
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class L2SupplyState:
    """L2 供给模式的**一次冻结态**：损失四项 + 标定程序 + 参考载荷。

    损失一阶冻结（§13.2 标定纪律 / 任务口径）：L2 单发积分 ~0.1 s，而载荷二分
    ~60 次求值——**每次迭代重跑积分不可接受**。故先按参考载荷跑**一次**积分取
    四项损失，全表七行共用；二分内损失为常量（需求仍是常数，标准二分不变）。

    参考载荷取**锚定模式 LEO 行运力**——它是全表七行的载荷量级中心（LEO 最大、
    GEO 最小，取 LEO 锚最接近"上升段主导"的解），比用户输入载荷更靠近各行的
    真解。一阶误差量级：载荷差 ΔP 通过 GLOW 改变重力损失 ~O(ΔP/GLOW)（F9 极端
    情形 LEO vs GEO 差 22 t / 549 t ≈ 4%，重力损失响应 ~2%≈0.03 km/s ⟹ 载荷
    误差 <1%——敏感性实测见 test_capacity_dual_supply 注释）。
    """

    losses_km_s: TrajectoryLosses
    program: TrajectoryProgram
    reference_payload_kg: float
    warnings: tuple[str, ...]

    @property
    def total_km_s(self) -> float:
        return self.losses_km_s.total_km_s


def l2_reference_losses_km_s(
    vehicle: Vehicle,
    ledger: FixedVehicleLedger,
    site: LaunchSite,
    *,
    program: TrajectoryProgram = L2_CALIBRATED_PROGRAM,
) -> L2SupplyState:
    """L2 供给的冻结态：按锚定 LEO 参考载荷跑一次弹道积分（§8.6 L2）。

    - 参考载荷 = 锚定模式 LEO 行运力（不可达时退回用户输入载荷并告警）；
    - 积分失败的诚实口径：PerfError 原样上抛（L2 是显式选用的模式——程序-构型
      失配是真实物理事实，静默回落锚定表会伪装成"算出来了"）；
    - 程序参数缺省用 :data:`L2_CALIBRATED_PROGRAM`（§13.2 三基准反标定一套参数
      服务全部构型——禁止逐构型调参，SV 的标高需求统一进参数空间处理）。
    """
    try:
        # 参考载荷 = 锚定模式 LEO 行运力（**含长燃时修正的完整 anchored 需求**——
        # 缺口修正：裸表值会让长燃时构型的参考载荷偏大 ~65%，冻结点偏离七行真解）
        anchored_req, _, _ = anchored_dv_km_s(vehicle, ledger, "LEO", site)
        reference_payload = payload_for_dv_on_ledger(
            ledger,
            anchored_req,
            bracket_hint_kg=vehicle.payload_mass_kg,
        )
    except PerfError:
        reference_payload = vehicle.payload_mass_kg
    probe = vehicle.model_copy(update={"payload_mass_kg": reference_payload})
    result = integrate_ascent(probe, program)
    return L2SupplyState(
        losses_km_s=result.losses_km_s,
        program=program,
        reference_payload_kg=reference_payload,
        warnings=tuple(result.warnings),
    )


def _l2_ideal_km_s(vehicle: Vehicle, site: LaunchSite, orbit: str) -> tuple[float, str]:
    """L2 模式的理想 ΔV [km/s]（§8.10 orbits 精算；返回 (ideal, 来源注)）。

    - LEO / SSO：停泊（圆）轨道速度 ``√(μ/r)``——高度取 Mission 显式值，缺省按
      :data:`DEFAULT_LEO_ALT_M` / :data:`DEFAULT_SSO_ALT_M`（与锚定链同一份工程
      惯例剖面）；倾角差（SSO 的 dogleg / 方位角惩罚）不在 2D 面内模型内——它经
      自转加成的方位角投影进入（南向发射加成为负），来源注如实声明；
    - GTO：停泊圆速度 + Hohmann 第一脉冲（= 转移椭圆近地点速度，与锚定链
      ``ideal_orbit_dv_km_s`` 同值，此处自 perf.orbits 精算取得——单一公式族）；
    - GEO（直送）：停泊圆速度 + :func:`geo_via_gto_km_s` 全路线（近地点点火 +
      远地点圆化/平面变更**矢量合成**，Δi = Mission.inclination_deg 或发射场
      纬度——比锚定链的理想多出平面变更精算，§8.10 约束 2）。
    """
    mission = vehicle.mission
    if orbit == "LEO":
        radius = EARTH_EQUATOR_RADIUS_M + (mission.altitude_m or DEFAULT_LEO_ALT_M)
        return circular_velocity_km_s(radius), (
            f"停泊圆轨道速度 √(μ/r)，r = R+{radius - EARTH_EQUATOR_RADIUS_M:.0f} m"
            "（perf.orbits 精算，§8.10；倾角差经自转加成方位角投影进入，2D 面内模型不含 dogleg）"
        )
    if orbit == "SSO":
        radius = EARTH_EQUATOR_RADIUS_M + (mission.altitude_m or DEFAULT_SSO_ALT_M)
        return circular_velocity_km_s(radius), (
            f"停泊圆轨道速度 √(μ/r)，r = R+{radius - EARTH_EQUATOR_RADIUS_M:.0f} m"
            "（perf.orbits 精算，§8.10；SSO 倾角差经自转加成方位角投影进入）"
        )
    r_p_m = _parking_radius_m(vehicle)
    if orbit == "GTO":
        kick = hohmann_transfer_km_s(r_p_m, GEO_RADIUS_M)
        ideal = circular_velocity_km_s(r_p_m) + kick.dv1_km_s
        return ideal, (
            "停泊圆速度 + Hohmann 近地点点火（= GTO 转移椭圆近地点速度；"
            f"perf.orbits 精算，§8.10，r_p={r_p_m:.0f} m）"
        )
    if orbit == "GEO":
        inclination = (
            mission.inclination_deg if mission.inclination_deg is not None else site.latitude_deg
        )
        geo = geo_via_gto_km_s(r_p_m, inclination)
        return circular_velocity_km_s(r_p_m) + geo.total_km_s, (
            "停泊圆速度 + GEO（GTO+远地点圆化/平面变更矢量合成）全路线"
            f"（perf.orbits 精算，§8.10，Δi={inclination:.2f}°）"
        )
    raise PerfError(  # pragma: no cover - 调用方已按目标分派
        f"轨道 {orbit!r} 无 L2 精算理想 ΔV 口径",
        suggestion="四锚定目标走 _l2_ideal_km_s；复合行走上升段底座 + 解析机动",
    )


def l2_dv_km_s(
    vehicle: Vehicle,
    site: LaunchSite,
    orbit: str,
    state: L2SupplyState,
) -> tuple[float, str]:
    """L2 供给的需求组装 [km/s]：``(ideal_orbits + ΣL2损失 − 自转加成, source)``。

    - 四锚定目标：ideal（:func:`_l2_ideal_km_s`）+ 冻结的四项损失 − 自转加成
      （**与 L1 同口径复用** :func:`rotation_assist_km_s`——含相容因子，任务口径）；
    - 复合行（TLI / TMI / GEO_GTO_CIRC）：上升段底座 = L2 的 LEO 行需求（同一
      冻结态），停泊轨道外机动沿用 §8.10 解析闭式——**射入段脉冲保持理想脉冲
      口径**（orbits 模块 assumption 已声明"不含有限推力损失"；L2 上升段损失只
      覆盖上升段，射入段的有限推力小量按惯例并入理想脉冲口径、不重复建模）；
    - **长燃时修正在 L2 模式自然消失**：L2 积分按质量账推进真实燃时（CZ-5 芯级
      319 s 的长燃累积直接体现在重力损失里），``long_burn_surcharge`` 的 k_g
      标定自由度不再需要——由测试钉住（l2 模式 dv_source 不含长燃时文案）。
    """
    loss_total = state.total_km_s
    assist = rotation_assist_km_s(
        site.latitude_deg, site.altitude_m, site.azimuth_deg, vehicle.mission.inclination_deg
    )
    loss_note = (
        f"L2 弹道积分四项损失 {loss_total:.2f} km/s（gravity/aero/steering/back_pressure，"
        f"§8.6 L2；程序=垂直段 {state.program.vertical_rise_s:.0f} s + 标高 "
        f"{state.program.scale_height_m / 1000:.0f} km，§13.2 三基准反标定）；"
    )
    if orbit in ("TLI", "TMI", GEO_GTO_CIRC_ORBIT):
        ascent_ideal, ascent_note = _l2_ideal_km_s(vehicle, site, "LEO")
        ascent_dv = ascent_ideal + loss_total - assist
        return ascent_dv, (
            f"上升段（L2 口径）：{loss_note}ideal={ascent_note}"
            f" − 自转加成 {assist:.2f}（L1 口径含相容因子）+ §8.10 轨道解析"
            "（射入/圆化脉冲为理想脉冲口径，L2 损失只覆盖上升段）"
        )
    ideal, ideal_note = _l2_ideal_km_s(vehicle, site, orbit)
    total = ideal + loss_total - assist
    return total, (
        f"orbits 精算理想 ΔV（{ideal_note}）+ {loss_note}"
        f"− 自转加成 {assist:.2f} km/s（L1 口径含相容因子，§8.6）"
    )


def _parking_radius_m(vehicle: Vehicle) -> float:
    """停泊轨道半径 [m]（§8.10 约束 3 的 r_p 标注口径）：近地点优先，缺省 200 km。"""
    perigee = vehicle.mission.perigee_altitude_m or PARKING_DEFAULT_ALT_M
    return EARTH_EQUATOR_RADIUS_M + perigee


def _in_space_addition_km_s(
    vehicle: Vehicle, site: LaunchSite, orbit: str
) -> tuple[float, float | None, str | None]:
    """复合行（TLI / TMI / GEO_GTO_CIRC）的停泊轨道外解析需求（§8.10 闭式）。

    返回 ``(dv_km_s, c3_km2_s2, window_assumption)``——TLI / TMI 行的 C3 必输出
    （约束 1），TMI 的窗口假设必填（约束 4）；GEO_GTO_CIRC 行的平面变更与圆化
    走矢量合成（约束 2），转角取 Mission.inclination_deg（缺省 = 发射场纬度，
    向东发射的自然倾角口径）。
    """
    r_p_m = _parking_radius_m(vehicle)
    if orbit == "TLI":
        injection = parking_injection_km_s("TLI", r_p_m)
        return injection.dv_km_s, injection.c3_km2_s2, None
    if orbit == "TMI":
        injection = parking_injection_km_s("TMI", r_p_m)
        return injection.dv_km_s, injection.c3_km2_s2, injection.window_assumption
    if orbit == GEO_GTO_CIRC_ORBIT:
        inclination = (
            vehicle.mission.inclination_deg
            if vehicle.mission.inclination_deg is not None
            else site.latitude_deg
        )
        geo = geo_via_gto_km_s(r_p_m, inclination)
        return geo.total_km_s, None, None
    raise PerfError(  # pragma: no cover - 调用方已按 PAYLOAD_ORBITS 分派
        f"轨道 {orbit!r} 不是复合目标（TLI/TMI/GEO_GTO_CIRC）",
        suggestion="锚定目标走 orbit_dv_requirement（§8.6 表）",
    )


_IN_SPACE_SOURCE_TAG: dict[str, str] = {
    "TLI": "§8.10 轨道解析（TLI 单脉冲射入，C3 成对输出——地心束缚 C3<0）",
    "TMI": ("§8.10 轨道解析（TMI 单脉冲射入，C3 成对输出且窗口/相位假设必填——§8.10 约束 4）"),
    "GEO_GTO_CIRC": (
        "§8.10 轨道解析（GTO 近地点点火 + 远地点圆化/平面变更矢量合成"
        "——标量相加被禁止，§8.10 约束 2）"
    ),
}


def payload_for_orbit(
    vehicle: Vehicle,
    site: LaunchSite,
    orbit: str,
    *,
    ledger: FixedVehicleLedger | None = None,
    dv_supply: DvSupplyMode = "anchored",
    l2_state: L2SupplyState | None = None,
) -> OrbitPayload:
    """单个目标轨道的运力反推（OI-38 / §8.10）：payload_by_orbit 的单行本体。

    - 锚定四目标（CAPACITY_ORBITS）：需求 = 锚定表 / 用户覆写（M4 口径）/
      **L2 供给**（M6 收官片：orbits 精算 ideal + L2 冻结损失 − 自转加成）；
    - 复合三行（TLI / TMI / GEO_GTO_CIRC）：需求 = **上升段底座（随 dv_supply
      切换：anchored = LEO 行锚定口径；l2 = LEO 行 L2 口径）** +
      :mod:`aeroforge.perf.orbits` 解析机动——``dv_source`` 记录两段拼合口径；
      TLI / TMI 行带 ``c3_km2_s2``，TMI 行带 ``window_assumption``（§8.10）；
    - 不可达：``payload_kg=0``、``attainable=False``（行齐备不丢行）。

    ``ledger`` 复用已构建质量账（纬度曲线 / 运力表 / MC 多次反推不重算几何账）；
    ``l2_state`` 复用已构建的 L2 冻结态（一次积分服务全表，缺省按需构建）。
    ``Mission.loss_factors`` 用户覆写在两种供给模式下都优先（最显式的用户输入）。
    """
    led = ledger if ledger is not None else vehicle_ledger(vehicle)
    c3: float | None = None
    window: str | None = None
    if orbit in CAPACITY_ORBITS:
        if vehicle.mission.loss_factors is not None:
            dv_used, source = _user_dv_requirement_km_s(vehicle, site, orbit)
        elif dv_supply == "l2":
            state = (
                l2_state if l2_state is not None else l2_reference_losses_km_s(vehicle, led, site)
            )
            dv_used, source = l2_dv_km_s(vehicle, site, orbit, state)
        else:
            dv_used, source, _ = anchored_dv_km_s(vehicle, led, orbit, site)
    elif orbit in ("TLI", "TMI", GEO_GTO_CIRC_ORBIT):
        if dv_supply == "l2" and vehicle.mission.loss_factors is None:
            state = (
                l2_state if l2_state is not None else l2_reference_losses_km_s(vehicle, led, site)
            )
            ascent_dv, ascent_source = l2_dv_km_s(vehicle, site, orbit, state)
        else:
            ascent_dv, ascent_source = _ascent_requirement_km_s(vehicle, site, led)
            ascent_source = f"上升段（LEO 行口径）：{ascent_source}"
        add_dv, c3, window = _in_space_addition_km_s(vehicle, site, orbit)
        dv_used = ascent_dv + add_dv
        source = f"{ascent_source} + {_IN_SPACE_SOURCE_TAG[orbit]}"
    else:
        raise PerfError(
            f"轨道 {orbit!r} 不在运力表目标内（PAYLOAD_ORBITS = {PAYLOAD_ORBITS}）",
            suggestion="表键取 LEO/SSO/GTO/GEO/TLI/TMI/GEO_GTO_CIRC；escape/custom 走 "
            "POST /api/orbits/transfer 做单点解析",
        )
    try:
        payload = payload_for_dv_on_ledger(led, dv_used, bracket_hint_kg=vehicle.payload_mass_kg)
    except PerfError:
        return OrbitPayload(
            payload_kg=0.0,
            dv_used_km_s=dv_used,
            dv_source=source,
            attainable=False,
            c3_km2_s2=c3,
            window_assumption=window,
        )
    return OrbitPayload(
        payload_kg=payload,
        dv_used_km_s=dv_used,
        dv_source=source,
        c3_km2_s2=c3,
        window_assumption=window,
    )


def payload_by_orbit(
    vehicle: Vehicle,
    site: LaunchSite,
    *,
    dv_supply: DvSupplyMode = "anchored",
) -> dict[str, OrbitPayload]:
    """各轨道点值运力表（OI-38 + §8.10）：七目标各反推一次（M6 轨道层扩行）。

    锚定四目标（LEO / SSO / GTO / GEO 直送）+ 复合三行（TLI / TMI /
    GEO（GTO+圆化））。每项带所用 ΔV 需求值与来源（量级锚定 / 量级锚定 +
    长燃时构型修正 / Mission 用户输入 / L2 弹道积分 / 复合拼合，§8.8）；构型对
    某目标不可达时该项 ``payload_kg=0``、``attainable=False``（表保持七行齐备，
    由调用方按 attainable 汇总 warning，不在表内丢行）。

    ``dv_supply="l2"`` 时先建**一次** L2 冻结态（单发积分 ~0.1 s，锚定 LEO 参考
    载荷），七行共用——每次迭代重跑积分不可接受（损失一阶冻结，§16.7）。
    """
    ledger = vehicle_ledger(vehicle)
    l2_state: L2SupplyState | None = None
    if dv_supply == "l2" and vehicle.mission.loss_factors is None:
        l2_state = l2_reference_losses_km_s(vehicle, ledger, site)
    return {
        orbit: payload_for_orbit(
            vehicle, site, orbit, ledger=ledger, dv_supply=dv_supply, l2_state=l2_state
        )
        for orbit in PAYLOAD_ORBITS
    }


#: 纬度曲线缺省采样点数（0–90° 均匀 11 点：步距 9°，OI-23 口径的工程惯例采样）。
LATITUDE_CURVE_DEFAULT_POINTS = 11


def latitude_samples(n_points: int = LATITUDE_CURVE_DEFAULT_POINTS) -> tuple[float, ...]:
    """纬度采样序列（0–90° 均匀 n 点；n < 2 显式拒绝——单点不成曲线）。"""
    if n_points < 2:
        raise PerfError(
            f"纬度曲线至少 2 个采样点，收到 {n_points}",
            suggestion="缺省 11 点（0–90° 步距 9°，OI-23 工程惯例采样）",
        )
    return tuple(90.0 * i / (n_points - 1) for i in range(n_points))


def payload_latitude_curve(
    vehicle: Vehicle, orbit: str, *, n_points: int = LATITUDE_CURVE_DEFAULT_POINTS
) -> PayloadLatitudeCurve:
    """运力—纬度曲线（OI-23，M6 验收判据）：纬度 0–90° 采样逐点反推运力。

    **固定其余参数**：只替换发射场纬度（方位角 / 倾角 / 构型全部保持），逐点
    复用同一份质量账与二分链（不另写第二套齐氏账）。发射场缺失按默认场纬度
    骨架取 0–90° 采样（§8.6 口径：纬度是自转加成与转向损失的唯一输入）。

    §16 M6 判据：曲线对纬度**单调不增**——由测试对 LEO / SSO 各显式断言一次；
    本函数如实给值，不内置单调性裁剪。
    """
    if orbit not in PAYLOAD_ORBITS:
        raise PerfError(
            f"轨道 {orbit!r} 不在运力表目标内（PAYLOAD_ORBITS = {PAYLOAD_ORBITS}）",
            suggestion="曲线目标取 LEO/SSO/GTO/GEO/TLI/TMI/GEO_GTO_CIRC 之一",
        )
    ledger = vehicle_ledger(vehicle)
    base_site = vehicle.mission.launch_site or DEFAULT_LAUNCH_SITE
    points: list[PayloadLatitudePoint] = []
    for lat in latitude_samples(n_points):
        row = payload_for_orbit(
            vehicle,
            base_site.model_copy(update={"latitude_deg": lat}),
            orbit,
            ledger=ledger,
        )
        points.append(
            PayloadLatitudePoint(lat_deg=lat, payload_kg=row.payload_kg, attainable=row.attainable)
        )
    return PayloadLatitudeCurve(
        payload_key=f"payload_{orbit.lower()}_kg",
        orbit=orbit,
        points=tuple(points),
        assumption=(
            f"纬度 0–90° 均匀 {n_points} 点采样（其余参数固定：方位角/倾角/构型不变）；"
            "逐点 = 同一质量账的载荷二分（复用 OI-38 链路不另算）；§16 M6 验收判据："
            "曲线对纬度单调不增（FR-19 同型）"
        ),
    )


__all__ = [
    "BISECT_MAX_ITERATIONS",
    "CAPACITY_ORBITS",
    "GEO_GTO_CIRC_ORBIT",
    "L2_CALIBRATED_PROGRAM",
    "LATITUDE_CURVE_DEFAULT_POINTS",
    "PAYLOAD_ORBITS",
    "PAYLOAD_TOLERANCE_KG",
    "DvSupplyMode",
    "FixedVehicleLedger",
    "L2SupplyState",
    "OrbitPayload",
    "PayloadLatitudeCurve",
    "PayloadLatitudePoint",
    "StageMasses",
    "ZeroStageMasses",
    "anchored_dv_km_s",
    "first_stage_burn_time_s",
    "l2_dv_km_s",
    "l2_reference_losses_km_s",
    "latitude_samples",
    "payload_by_orbit",
    "payload_for_dv",
    "payload_for_dv_on_ledger",
    "payload_for_orbit",
    "payload_latitude_curve",
    "vehicle_ledger",
]
