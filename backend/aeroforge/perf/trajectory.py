"""L2 简化上升弹道积分（规格 §8.6「L2（M6 可选，简化上升弹道积分）」，M6 轨道层第二片）。

模型口径（§8.6 L2 原文：「点质量 2D 积分：指数大气 + 重力转弯程序 + 变重力 → 直接算出损失」）
=================================================================================================

**地球模型——球面不旋转地球，发射点相对系积分**：

- 常数复用 :mod:`aeroforge.perf.losses` 的 WGS-84 权威值（R = 6 378 137 m、GM =
  3.986004418×10¹⁴ m³/s²），不另设第二份地球常数；
- 变重力 ``g(h) = μ/(R+h)²``；离心卸载 ``v²/(R+h)`` 项进弹道倾角方程（球面模型
  的标志项——平面地球模型没有它）；
- 积分在**发射点相对系**（随地球自转的地面系）进行：初态 v₀=0、γ₀=90°（箭垂直
  立在发射台上）。若取惯性系积分，自转初速是水平 0.4 km/s 量级、点质量模型没有
  地面支撑约束，起飞瞬间弹道会立即向下弯（γ 走负）——物理错误。相对系积分下
  科氏力与牵连离心未建模（~0.03–0.3 m/s²，M6 简化口径，对损失分解的影响在 L1
  对照带之内）；**自转加成不进积分**，作为独立初速信用项输出（面内分量
  ``ω(R+h_场)·cos(纬度)·sin(罗盘方位角)``——与 :func:`losses.rotation_assist_km_s`
  同式但**不施加相容因子**：相容因子是 L1 经验构造，2D 面内模型没有它的对应物）。

**大气——ISA 1976 分层指数近似（§3.1「大气模型强制 ISA 1976」）**：

- 分层结构与层底温度/递减率取 US Standard Atmosphere 1976（NOAA/NASA/USAF，
  1976）表 4，位势高度分层 [0, 11, 20, 32, 47, 51, 71, 84.852] km；
- 层底气压/密度由 ISA 1976 水静力学方程逐层精确递推（层边界连续）；
- 层内用**等温指数近似** ``ρ = ρ_b·exp(−(h−h_b)/H)``：等温层（递减率 0）该式
  精确；变温层的标高按**层底/层顶密度两端精确衔接**拟合（``H = Δh/ln(ρ_b/ρ_顶)``，
  工程惯例的分段指数大气），层内中段对 ISA 精确式的偏差 <5%——这是 §8.6 L2
  「指数大气」的实现形态。气压同理独立拟合标高（只进背压推力修正，误差不敏感）。

**控制——重力转弯程序（γ 程序剖面 + 逆动力学攻角）**：

垂直段 ``t ≤ vertical_rise_s`` 内弹道倾角恒 90°（重力转弯的工程惯例执行方式）；
其后**弹道倾角剖面随高度收敛到目标倾角** ``final_pitch_deg``：指数模式
``γ_cmd = γ_f + (90°−γ_f)·exp(−h/H)``，线性模式 ``γ_cmd = max(γ_f, 90°−k·h)``——
§8.6 任务口径「γ 随高度线性/指数收敛到目标倾角」的字面实现。跟踪剖面所需的
推力攻角由弹道倾角方程**逆动力学反解**：
``sin α = (m·v/F)·[dγ_cmd/dt − (v/r − g/v)·cosγ]``；|sin α| > 1 时饱和于 ±90°
（推力不足以跟踪剖面，γ 滞后——不虚构不存在的控制力）。程序参数（垂直段长 /
模式 / 标高或斜率 / 目标倾角）= 本层的**可调自由度**（全部工程惯例缺省、非权威
来源）。为何不指令推力程序角 θ：θ 直控下程序衰减快于 γ 动力学时 α 出现大负值，
推力把弹道往地面压——实测触地发散；γ 剖面直控对任意合理参数都稳定，且正是
任务书描述的形态。

**力——与既有账本同源**：

- 推力：``F(h) = F_vac − p(h)·A_e``，喷管出口面积由 Schema 的海平面/真空推力对
  反推 ``A_e = (F_vac − F_sl)/p₀``（p₀ = ISA 1976 海平面 101 325 Pa）——h ≥ 0 时
  ``F(h) ≥ F_sl > 0`` 恒成立，无需防御分支。**与 L1 背压口径的同源性**：L1 的
  账本是真空 Isp 口径（§8.6 长燃时注记），L2 的损失同样以真空推力为参照记账；
- 阻力：``D = ½ρv²·Cd·A_ref``，**Cd 常数**（工程惯例：取 ``Vehicle.aero``，缺失
  按 0.3——与 L1 ``DEFAULT_DRAG_COEFFICIENT`` 同源同一份常量；Ma 分段表需逐点
  来源标注，M6 简化口径不引入无来源系数）；
- 质量账：复用 :func:`perf.capacity.vehicle_ledger`（几何解析推进剂 + σ 干重，
  单一来源）与 :func:`params.staging.resolve_zero_stage`（0 级段 OI-36 合并口径：
  助推器并联推力并入芯一级段、芯级推进剂跨段连续核算、段末抛离助推器干重）——
  **不另写第二套合并规则**。级间分离为瞬时质量突变（抛干重），级间无滑行段。

**损失分解——四项与 L1 同名对齐（§8.6 L1 表）**：

增广状态把各损失累计量随弹道一起 RK4 积分，与理想 ΔV 账本精确闭合（残差作为
自检输出）。四项定义（全部真 ΔV 量纲）：

- ``gravity_km_s   = ∫ g(h)·sinγ·dt``（变重力，任务书原式）；
- ``aero_km_s      = ∫ D/m·dt``（任务书原式）；
- ``back_pressure_km_s = ∫ (F_vac − F(h))/m·dt``——⚠ 任务书速记式
  ``∫(F_sl−F(h))/ṁ·dt`` 按字面积分为**负值**（h ≥ 0 时 F(h) ≥ F_sl），且 /ṁ 缺
  质量比权重（ṁ/m）不构成真 ΔV；本实现取**真空账本口径** ``∫p(h)·A_e/m·dt``——
  与 L1「压力/控制余量」（``back_pressure_loss_km_s``，真空账本下的低空推力
  折损）同名同义，量级 0.05–0.2 km/s；
- ``steering_km_s  = ∫ F(h)·(1−cos α)/m·dt``（面内攻角操纵损失：推力未对准速度
  方向的分量赤字）——⚠ 与 L1 转向项**口径不同**：L1 的转向损失是方位角偏离正东
  与倾角失配的**面外经验罚项**（§8.6 L1 表），2D 面内模型没有它的对应物；一个
  良好的重力转弯程序 α 极小、面内操纵损失天然只有几 m/s——这不是缺陷而是物理，
  对照测试按此口径差异如实报告（详见 tests）。

**积分**：RK4 固定步长（默认 0.1 s，可配），步内不跨级间边界（边界处抛离质量）。
发散防护：触地（h 低于发射场/海平面 1 m 以上）、速度超第二宇宙速度、燃时超上限
——报错并携带最后状态（§8.6 任务口径「报错带最后状态」）。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aeroforge.errors import PerfError
from aeroforge.params import staging
from aeroforge.params.dag import G0, propagate_vehicle
from aeroforge.params.schema import LaunchSite, Vehicle
from aeroforge.perf.losses import (
    DEFAULT_DRAG_COEFFICIENT,
    DEFAULT_LAUNCH_SITE,
    EARTH_EQUATOR_RADIUS_M,
    EARTH_GM_M3_S2,
    OMEGA_EARTH_RAD_S,
)

if TYPE_CHECKING:
    from aeroforge.perf.capacity import FixedVehicleLedger

# ---------------------------------------------------------------------------
# 大气：ISA 1976 分层指数近似（§3.1 强制口径；出处：US Standard Atmosphere 1976）
# ---------------------------------------------------------------------------

#: ISA 1976 分层表 (层底位势高度 [m], 层底温度 [K], 垂直温度递减率 [K/m])。
#: 逐条为 US Standard Atmosphere 1976（NOAA/NASA/USAF，1976）表 4 原文值。
_ISA_LAYERS: tuple[tuple[float, float, float], ...] = (
    (0.0, 288.15, -0.0065),
    (11_000.0, 216.65, 0.0),
    (20_000.0, 216.65, 0.001),
    (32_000.0, 228.65, 0.0028),
    (47_000.0, 270.65, 0.0),
    (51_000.0, 270.65, -0.0028),
    (71_000.0, 214.65, -0.002),
    (84_852.0, 186.87, 0.0),
)

#: 干空气气体常数 [J/(kg·K)]（ISA 1976 定义值）。
AIR_GAS_CONSTANT_J_KG_K = 287.05287

#: ISA 1976 海平面气压 [Pa]（标准原文值；同时是喷管出口面积反推的参照压）。
ISA_PRESSURE_SEA_LEVEL_PA = 101_325.0

#: ISA 1976 海平面密度 [kg/m³]（标准原文值；层底密度由气压递推，此值仅作文档锚）。
ISA_DENSITY_SEA_LEVEL_KG_M3 = 1.225


@dataclass(frozen=True, slots=True)
class _AtmoLayer:
    """单个 ISA 分层的指数近似参数（层底值精确衔接，层内等温指数）。"""

    base_m: float
    density_base_kg_m3: float
    pressure_base_pa: float
    density_scale_height_m: float
    pressure_scale_height_m: float


def _build_isa_layers() -> tuple[_AtmoLayer, ...]:
    """逐层递推 ISA 1976 层底气压/密度并拟合层内指数标高（模块级一次性计算）。

    变温层层底→层顶用 ISA 1976 多方式（``p ∝ T^(−g₀/(R·L))``）；标高按层底/层顶
    **两端精确衔接**拟合（等温层指数式本身精确，H = R·T/g₀）。
    """
    layers: list[_AtmoLayer] = []
    p_base = ISA_PRESSURE_SEA_LEVEL_PA
    for i, (h_b, t_b, lapse) in enumerate(_ISA_LAYERS):
        rho_b = p_base / (AIR_GAS_CONSTANT_J_KG_K * t_b)
        if i + 1 >= len(_ISA_LAYERS):
            h_scale = AIR_GAS_CONSTANT_J_KG_K * t_b / G0
            layers.append(_AtmoLayer(h_b, rho_b, p_base, h_scale, h_scale))
            break
        h_top = _ISA_LAYERS[i + 1][0]
        dh = h_top - h_b
        if lapse == 0.0:
            p_top = p_base * math.exp(-G0 * dh / (AIR_GAS_CONSTANT_J_KG_K * t_b))
            h_scale = AIR_GAS_CONSTANT_J_KG_K * t_b / G0
            layers.append(_AtmoLayer(h_b, rho_b, p_base, h_scale, h_scale))
        else:
            t_top = t_b + lapse * dh
            p_top = p_base * (t_top / t_b) ** (-G0 / (AIR_GAS_CONSTANT_J_KG_K * lapse))
            rho_top = p_top / (AIR_GAS_CONSTANT_J_KG_K * t_top)
            layers.append(
                _AtmoLayer(
                    h_b,
                    rho_b,
                    p_base,
                    dh / math.log(rho_b / rho_top),
                    dh / math.log(p_base / p_top),
                )
            )
        p_base = p_top
    return tuple(layers)


_ISA_LAYERS_BUILT: tuple[_AtmoLayer, ...] = _build_isa_layers()


def isa_density_pressure(h_m: float) -> tuple[float, float]:
    """ISA 1976 分层指数近似的密度与气压 ``(ρ [kg/m³], p [Pa])``。

    层边界连续（层底值由 ISA 精确式递推）；负高度按第 0 层外推（地下发射场无
    物理意义，但公式不炸）。"""
    layer = _ISA_LAYERS_BUILT[0]
    for candidate in reversed(_ISA_LAYERS_BUILT):
        if h_m >= candidate.base_m:
            layer = candidate
            break
    dh = h_m - layer.base_m
    return (
        layer.density_base_kg_m3 * math.exp(-dh / layer.density_scale_height_m),
        layer.pressure_base_pa * math.exp(-dh / layer.pressure_scale_height_m),
    )


# ---------------------------------------------------------------------------
# 程序参数（可调自由度）与校验域
# ---------------------------------------------------------------------------

#: 默认积分步长 [s]（§8.6 任务口径「0.1 s 级」）。
DEFAULT_DT_S = 0.1

#: 燃时上限 [s]：超过即视为积分发散（程序/构型失配，不静默给半截弹道）。
MAX_BURN_TIME_S = 3600.0

#: 程序参数「荒谬域」边界（域外报错——校验域不是物理标定域，只是显然不合理界）。
_VERTICAL_RISE_MAX_S = 60.0
_SCALE_HEIGHT_MAX_M = 1_000_000.0
_PITCH_RATE_MAX_DEG_PER_KM = 100.0
_FINAL_PITCH_MIN_DEG, _FINAL_PITCH_MAX_DEG = -30.0, 60.0
_DT_MIN_S, _DT_MAX_S = 0.01, 1.0

#: 积分布长公开口径（API 请求值域与 :func:`_validate_program` 同源）。
DT_MIN_S = _DT_MIN_S
DT_MAX_S = _DT_MAX_S

#: 显式燃时（Stage.burn_time_s）与质量账派生燃时的相对偏差告警阈值（不静默）。
_BURN_TIME_DEVIATION_WARN = 0.20

#: 逆动力学攻角饱和界（工程惯例、非权威来源）——**按高度分档**：max-q 前后的
#: 稠密段气动载荷限制攻角（~10° 量级，工程惯例），其上轨迹整形不受气动载荷
#: 约束、可达数十度。界的作用是把程序-推力失配（低 TWR 上面级、急剖面）时的
#: 操纵损失有界化（``∫F(1−cosα)/m ≤ (1−cos α_max)·ideal_dv``），γ 滞后剖面后由
#: 重力转弯动力学自然接管——不虚构不存在的控制力，也不让失配演变成俯冲发散。
#: ⚠ 缺省值同时是 :class:`TrajectoryProgram` 两个标定字段的缺省——M6 收官片把
#: 双界提为程序字段（§13.2 标定自由度），常量仅作缺省锚保留（单一来源：字段
#: default 引用本值，改字段不改编译期常量）。
_ALPHA_ATMO_MAX_DEG = 10.0
_ALPHA_VAC_MAX_DEG = 30.0
#: 分档过渡带（高度）：下缘全取大气档，上缘以上全取真空档（工程惯例：转折点
#: 与 max-q / 稠密段上界同量级）。
_ALPHA_TRANSITION_LOW_M = 12_000.0
_ALPHA_TRANSITION_HIGH_M = 30_000.0


class TrajectoryProgram(BaseModel):
    """重力转弯程序参数（§8.6 L2 的可调自由度；缺省为工程惯例值、非权威来源）。

    程序量是**弹道倾角剖面 γ_cmd(h)**（§8.6 任务口径「γ 随高度线性/指数收敛到
    目标倾角」的字面实现，见模块 docstring「控制」节）：垂直段内恒 90°，其后随
    高度按模式收敛到 ``final_pitch_deg``；推力攻角 α 由逆动力学自满足以跟踪剖面
    （推力不足以跟踪时 α 饱和于 ±90°，γ 滞后于剖面——不虚构不存在的控制力）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["linear", "exponential"] = Field(
        default="exponential",
        description=(
            "程序转弯模式：exponential = γ 随高度指数收敛（工程惯例缺省）；linear = 线性收敛"
        ),
    )
    vertical_rise_s: float = Field(
        default=8.0,
        ge=0.0,
        description="垂直上升段时长 [s]（工程惯例 5–10 s；此段内弹道倾角恒 90°）",
    )
    scale_height_m: float = Field(
        default=40_000.0,
        gt=0.0,
        description=(
            "exponential 模式的程序标高 [m]：γ_cmd = γ_f + (90°−γ_f)·exp(−h/H)。"
            "缺省 40 km 服务常规两级构型；低 TWR 上面级的重型构型（如 S-IVB 级"
            "TWR<0.5）需加大标高放缓末段拉平，否则燃前触地（报错提示）"
        ),
    )
    pitch_rate_deg_per_km: float = Field(
        default=0.5,
        gt=0.0,
        description="linear 模式的收敛斜率 [°/km]：γ_cmd = max(γ_f, 90° − k·h)",
    )
    final_pitch_deg: float = Field(
        default=0.0,
        description="目标弹道倾角 [°]（相对当地水平面；入轨剖面取 ~0°）",
    )
    alpha_atmo_max_deg: float = Field(
        default=_ALPHA_ATMO_MAX_DEG,
        gt=0.0,
        description=(
            "逆动力学攻角饱和界——稠密段档 [°]（max-q 前后的气动载荷限制；"
            "工程惯例缺省 10°，§13.2 标定自由度）"
        ),
    )
    alpha_vac_max_deg: float = Field(
        default=_ALPHA_VAC_MAX_DEG,
        gt=0.0,
        description=(
            "逆动力学攻角饱和界——真空段档 [°]（轨迹整形上限；工程惯例缺省 30°，§13.2 标定自由度）"
        ),
    )

    @model_validator(mode="after")
    def _check_alpha_bounds(self) -> TrajectoryProgram:
        """双饱和界必须大气档 ≤ 真空档 ≤ 90°（稠密段比真空段更受限是物理事实）。"""
        if not (self.alpha_atmo_max_deg <= self.alpha_vac_max_deg <= 90.0):
            raise ValueError(
                f"攻角饱和界须满足大气档 {self.alpha_atmo_max_deg}° ≤ 真空档 "
                f"{self.alpha_vac_max_deg}° ≤ 90°（稠密段受气动载荷限制、真空段不受）"
            )
        return self


def _validate_program(program: TrajectoryProgram, dt_s: float) -> None:
    """程序参数荒谬域校验（域外报错；边界见模块常量——校验域非物理标定域）。"""
    if not (0.0 <= program.vertical_rise_s <= _VERTICAL_RISE_MAX_S):
        raise PerfError(
            f"垂直上升段时长 {program.vertical_rise_s} s 越出合理域 [0, {_VERTICAL_RISE_MAX_S}] s",
            suggestion="垂直段是重力转弯的工程惯例执行段（典型 5–10 s），过长即程序荒谬",
        )
    if not (0.0 < program.scale_height_m <= _SCALE_HEIGHT_MAX_M):
        raise PerfError(
            f"程序标高 {program.scale_height_m} m 越出合理域 (0, {_SCALE_HEIGHT_MAX_M}] m",
            suggestion="exponential 模式标高取 1 万–3 万 m 量级（工程惯例）",
        )
    if not (0.0 < program.pitch_rate_deg_per_km <= _PITCH_RATE_MAX_DEG_PER_KM):
        raise PerfError(
            f"线性转弯斜率 {program.pitch_rate_deg_per_km} °/km 越出合理域 "
            f"(0, {_PITCH_RATE_MAX_DEG_PER_KM}]",
            suggestion="linear 模式斜率取 0.5–2 °/km 量级（工程惯例）",
        )
    if not (_FINAL_PITCH_MIN_DEG <= program.final_pitch_deg <= _FINAL_PITCH_MAX_DEG):
        raise PerfError(
            f"目标程序角 {program.final_pitch_deg}° 越出合理域 "
            f"[{_FINAL_PITCH_MIN_DEG}, {_FINAL_PITCH_MAX_DEG}]°",
            suggestion="入轨剖面的目标程序角在 0° 附近（水平入轨），大角度即程序荒谬",
        )
    if not (_DT_MIN_S <= dt_s <= _DT_MAX_S):
        raise PerfError(
            f"积分步长 {dt_s} s 越出合理域 [{_DT_MIN_S}, {_DT_MAX_S}] s",
            suggestion="RK4 固定步长按「0.1 s 级」配置（§8.6），过粗会损失转弯段精度",
        )


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


class TrajectoryLosses(BaseModel):
    """逐项损失分解 [km/s]（四项口径与 L1 同名对齐，§8.6；差异说明见模块 docstring）。"""

    gravity_km_s: float = Field(description="重力损失 = ∫g(h)·sinγ·dt（变重力）")
    aero_km_s: float = Field(description="气动损失 = ∫D/m·dt")
    steering_km_s: float = Field(
        description="面内攻角操纵损失 = ∫F(h)·(1−cosα)/m·dt（口径差异见 docstring）"
    )
    back_pressure_km_s: float = Field(
        description="背压损失 = ∫(F_vac − F(h))/m·dt（即 §8.6「压力/控制余量」的 L2 口径）"
    )
    total_km_s: float = Field(description="四项之和（与 L1「四损失之和」同角色）")


class TrajectoryBurnoutState(BaseModel):
    """燃尽状态（全部推进剂烧尽、级已抛离、载荷随行；相对发射点旋转系）。"""

    time_s: float = Field(description="燃尽时刻 [s]（自起飞）")
    altitude_m: float = Field(description="燃尽高度 [m]")
    velocity_m_s: float = Field(description="燃尽速度 [m/s]（相对系）")
    flight_path_angle_deg: float = Field(description="燃尽弹道倾角 [°]（相对当地水平面）")
    downrange_km: float = Field(description="燃尽射程 [km]（发射面内地面距离）")
    mass_kg: float = Field(description="燃尽质量 [kg]（载荷 + 末级干重）")
    horizontal_velocity_m_s: float = Field(
        description="燃尽水平速度分量 v·cosγ [m/s]（入轨有效分量）"
    )


class StageTimelineEntry(BaseModel):
    """级段时间线（0 级段含助推器并联段与芯级跨段续烧段，§8.5 口径）。"""

    segment: str = Field(description="段名：zero_stage_booster / zero_stage_core / stage_<index>")
    t_start_s: float = Field(description="段起始时刻 [s]")
    t_end_s: float = Field(description="段结束时刻 [s]")
    isp_vacuum_s: float = Field(
        description="段真空比冲 [s]（0 级段为并联有效比冲 Isp_eff，派生量）"
    )
    thrust_vacuum_n: float = Field(description="段真空推力 [N]")
    mass_flow_kg_s: float = Field(description="段真空流量 [kg/s]（OI-35 配对口径）")
    exit_area_m2: float = Field(description="段喷管出口总面积 [m²]（由海平面/真空推力对反推）")
    propellant_burned_kg: float = Field(description="段内烧掉推进剂 [kg]")
    jettison_kg: float = Field(description="段末抛离质量 [kg]（助推器干重 / 下级干重；末段为 0）")


class TrajectoryResult(BaseModel):
    """L2 上升弹道积分结果（§8.6 L2：损失分解 + 燃尽状态 + 求解元数据）。"""

    losses_km_s: TrajectoryLosses = Field(description="逐项损失分解（四项与 L1 同名对齐）")
    burnout: TrajectoryBurnoutState = Field(description="燃尽状态")
    ideal_dv_km_s: float = Field(
        description=(
            "火箭实际提供的理想 ΔV [km/s] = ∫F_vac/m·dt（真空账本口径，与质量账逐级齐氏账一致）"
        )
    )
    dv_gained_km_s: float = Field(
        description="弹道实际获得的速度增量 [km/s]（相对系 v_燃尽 − v_0）"
    )
    closure_residual_m_s: float = Field(
        description=(
            "闭合残差 [m/s] = ideal_dv − dv_gained − Σ损失（RK4 增广积分的自检量，应接近 0）"
        )
    )
    rotation_assist_m_s: float = Field(
        description=(
            "自转加成信用（面内分量）[m/s] = ω(R+h_场)·cos(纬度)·sin(罗盘方位角)——"
            "以独立信用项输出（不进积分、不施加 L1 相容因子，见 docstring）；西向为负"
        )
    )
    liftoff_twr: float = Field(description="起飞推重比（含助推器海平面推力，整箭口径）")
    glow_kg: float = Field(description="起飞质量 [kg]（含载荷与助推器，质量账口径）")
    stage_timeline: tuple[StageTimelineEntry, ...] = Field(
        description="级段时间线（含 0 级段拆分）"
    )
    warnings: tuple[str, ...] = Field(description="输入缺省 / 口径近似 / 燃时偏差等显式警告")
    provenance: dict[str, str] = Field(
        description="求解元数据：地球模型 / 大气口径 / 程序参数 / 积分器 / 损失口径"
    )
    compute_ms: float = Field(description="本次积分实测耗时 [ms]（端点同步/异步选择的依据）")


# ---------------------------------------------------------------------------
# 级段构造（复用 staging.resolve_zero_stage 与 vehicle_ledger，不另写合并规则）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Segment:
    """一个动力飞行段（段内推力/流量/出口面积恒定，段末瞬时抛离）。"""

    label: str
    t_start_s: float
    t_end_s: float
    thrust_vacuum_n: float
    mass_flow_kg_s: float
    exit_area_m2: float
    jettison_kg: float

    @property
    def isp_vacuum_s(self) -> float:
        """段真空比冲 [s]（OI-35 口径的派生量：F_vac/(ṁ·g₀)）。"""
        return self.thrust_vacuum_n / (self.mass_flow_kg_s * G0)

    @property
    def duration_s(self) -> float:
        return self.t_end_s - self.t_start_s

    @property
    def propellant_burned_kg(self) -> float:
        return self.mass_flow_kg_s * self.duration_s


def _exit_area_m2(thrust_vacuum_n: float, thrust_sea_level_n: float) -> float:
    """喷管出口总面积 [m²]：``A_e = (F_vac − F_sl)/p₀``（ISA 海平面参照压）。

    Schema 的海平面/真空推力对是同一台发动机的两个环境口径（§1.7.5 OI-35），
    其差恰为环境压强 × 出口面积——由数据自身反推，不引入新的经验系数。
    """
    return (thrust_vacuum_n - thrust_sea_level_n) / ISA_PRESSURE_SEA_LEVEL_PA


def _build_segments(
    vehicle: Vehicle, ledger: FixedVehicleLedger
) -> tuple[tuple[_Segment, ...], tuple[str, ...]]:
    """级段时间线：0 级段（有助推器时）+ 芯级串联链，段末抛离质量随段携带。

    燃时一律由**质量账派生**（``m_prop/ṁ``，OI-35 真空配对）——L2 积分烧的是
    几何解析推进剂，``Stage.burn_time_s`` 显式值仅作交叉核对（偏差 >20% 告警，
    不静默采信也不静默丢弃）。
    """
    warnings: list[str] = []
    schema_stages = sorted(vehicle.stages, key=lambda s: s.index)
    segments: list[_Segment] = []
    t = 0.0

    # 0 级段（§8.5 / OI-36：staging.resolve_zero_stage 的合并口径直接复用）
    if vehicle.boosters:
        summary = staging.resolve_zero_stage(vehicle)
        if summary is None:  # pragma: no cover - 有助推器时 resolve 必不返回 None
            raise PerfError(
                "存在助推器但 0 级段派生返回空",
                suggestion="检查 boosters 配置（§8.5 / OI-36）",
            )
        zero = ledger.zero_stage
        if zero is None:  # pragma: no cover - 同上，账本与 summary 必然同时存在
            raise PerfError(
                "存在助推器但质量账缺少 0 级段",
                suggestion="检查 boosters 配置（§8.5 / OI-36）",
            )
        core = schema_stages[0]
        f_sl_total = core.engine_count * core.engine.thrust_sea_level_n
        for booster in vehicle.boosters:
            f_sl_total += (
                booster.count * booster.stage.engine_count * booster.stage.engine.thrust_sea_level_n
            )
        t_booster = zero.booster_propellant_kg / zero.booster_mass_flow_kg_s
        core_burn = min(zero.core_mass_flow_kg_s * t_booster, ledger.stages[0].m_propellant_kg)
        core_remaining = ledger.stages[0].m_propellant_kg - core_burn
        segments.append(
            _Segment(
                label="zero_stage_booster",
                t_start_s=t,
                t_end_s=t + t_booster,
                thrust_vacuum_n=summary.total_vacuum_thrust_n,
                mass_flow_kg_s=summary.total_mass_flow_kg_s,
                exit_area_m2=_exit_area_m2(summary.total_vacuum_thrust_n, f_sl_total),
                jettison_kg=zero.booster_dry_kg,
            )
        )
        t += t_booster
        # 芯一级跨段续烧（§8.5：芯级推进剂跨段连续核算）；被 0 级段烧尽时干重
        # 随 0 级段末一并抛离（段时长为 0 的级不单列）
        core_dry = ledger.stages[0].m_dry_kg
        if core_remaining > 1e-9:
            f_vac_core = core.engine_count * core.engine.thrust_vacuum_n
            f_sl_core = core.engine_count * core.engine.thrust_sea_level_n
            segments.append(
                _Segment(
                    label="zero_stage_core",
                    t_start_s=t,
                    t_end_s=t + core_remaining / zero.core_mass_flow_kg_s,
                    thrust_vacuum_n=f_vac_core,
                    mass_flow_kg_s=zero.core_mass_flow_kg_s,
                    exit_area_m2=_exit_area_m2(f_vac_core, f_sl_core),
                    jettison_kg=core_dry,
                )
            )
            t += segments[-1].duration_s
        else:
            segments[-1] = _Segment(
                label=segments[-1].label,
                t_start_s=segments[-1].t_start_s,
                t_end_s=segments[-1].t_end_s,
                thrust_vacuum_n=segments[-1].thrust_vacuum_n,
                mass_flow_kg_s=segments[-1].mass_flow_kg_s,
                exit_area_m2=segments[-1].exit_area_m2,
                jettison_kg=segments[-1].jettison_kg + core_dry,
            )
        derived_burn_s = {
            1: t,  # 芯一级总燃时 = 0 级段 + 跨段续烧
        }
        rest = ledger.stages[1:]
        rest_schema = schema_stages[1:]
    else:
        derived_burn_s = {}
        rest = ledger.stages
        rest_schema = schema_stages

    # 串联链其余各级（含无助推器时的芯一级）：ṁ = F_vac/(Isp·g₀)（OI-35 配对）
    last_position = len(rest) - 1
    for position, (row, schema_stage) in enumerate(zip(rest, rest_schema, strict=True)):
        engine = schema_stage.engine
        f_vac = schema_stage.engine_count * engine.thrust_vacuum_n
        f_sl = schema_stage.engine_count * engine.thrust_sea_level_n
        flow = f_vac / (row.isp_vacuum_s * G0)
        duration = row.m_propellant_kg / flow
        jettison = row.m_dry_kg if position < last_position else 0.0
        segments.append(
            _Segment(
                label=f"stage_{schema_stage.index}",
                t_start_s=t,
                t_end_s=t + duration,
                thrust_vacuum_n=f_vac,
                mass_flow_kg_s=flow,
                exit_area_m2=_exit_area_m2(f_vac, f_sl),
                jettison_kg=jettison,
            )
        )
        t += duration
        if schema_stage.index not in derived_burn_s:
            derived_burn_s[schema_stage.index] = duration

    # 显式燃时交叉核对（§8.6 燃时口径：显式优先属 L1 经验链；L2 以质量账为准）
    for schema_stage in schema_stages:
        explicit = schema_stage.burn_time_s
        if explicit is None:
            continue
        derived = derived_burn_s.get(schema_stage.index)
        if (
            derived is not None
            and derived > 0.0
            and abs(explicit - derived) / derived > _BURN_TIME_DEVIATION_WARN
        ):
            warnings.append(
                f"级 {schema_stage.index} 的显式燃时 {explicit:.0f} s 与质量账派生燃时 "
                f"{derived:.0f} s 偏差超过 {_BURN_TIME_DEVIATION_WARN:.0%}——L2 积分按质量账"
                "（m_prop/ṁ，OI-35 真空配对）推进，显式值仅供 L1 经验链使用"
            )

    if not segments:  # pragma: no cover - Schema min_length=1 保证非空
        raise PerfError("无可用动力飞行段", suggestion="检查 stages 配置（§6.1）")
    if segments[-1].t_end_s > MAX_BURN_TIME_S:
        raise PerfError(
            f"质量账派生的总燃时 {segments[-1].t_end_s:.0f} s 超出积分上限 {MAX_BURN_TIME_S:.0f} s",
            suggestion="检查发动机流量与推进剂质量（流量异常小通常源于比冲字段量纲错误，ADR-014）",
        )
    return tuple(segments), tuple(warnings)


# ---------------------------------------------------------------------------
# 积分入口
# ---------------------------------------------------------------------------


def _resolve_site(vehicle: Vehicle, warnings: list[str]) -> LaunchSite:
    """发射场解析（与 perf.evaluate.resolve_site 同口径：内联优先、缺省默认场）。

    不直接 import evaluate 的实现，避免与并行轨道片对 evaluate 的扩展耦合；
    口径由本函数与测试共同钉住（缺省场 + warning，不静默）。"""
    site = vehicle.mission.launch_site
    if site is not None:
        return site
    warnings.append(
        "Mission.launch_site 缺失：自转加成信用按默认发射场（卡纳维拉尔 28.5°N、向东）"
        "计算——工程惯例锚，补齐发射场可消除该项近似（与 perf.evaluate 同口径）"
    )
    return DEFAULT_LAUNCH_SITE


def integrate_ascent(
    vehicle: Vehicle,
    program: TrajectoryProgram | None = None,
    *,
    dt_s: float = DEFAULT_DT_S,
) -> TrajectoryResult:
    """点质量 2D 上升弹道积分（§8.6 L2）：RK4 固定步长 + 增广损失状态。

    参数
    ----
    vehicle: 完整飞行器参数（§6.1；Mission.launch_site 提供发射场，Vehicle.aero
        提供 Cd / 参考面积——缺失按工程惯例缺省并 warning）。
    program: 重力转弯程序参数（缺省 :class:`TrajectoryProgram` 的工程惯例值）。
    dt_s: RK4 固定步长 [s]（默认 0.1，可配；域外报错）。

    返回
    ----
    :class:`TrajectoryResult`：四项损失分解 + 燃尽状态 + 级段时间线 + 溯源元数据。

    报错
    ----
    :class:`~aeroforge.errors.PerfError`：程序参数荒谬 / 起飞推重比 ≤ 0 / 积分发散
    （触地、超第二宇宙速度、燃时超上限）——发散报错携带最后状态。
    """
    prog = program if program is not None else TrajectoryProgram()
    _validate_program(prog, dt_s)

    # 质量账惰性导入（M6 收官片）：capacity 的 L2 供给消费本模块，本模块消费
    # capacity 的质量账——包内成环，运行时延迟到调用点拆环（模块 docstring 已声明
    # 质量账单一来源复用纪律不变）
    from aeroforge.perf.capacity import _liftoff_twr, vehicle_ledger

    warnings: list[str] = []
    site = _resolve_site(vehicle, warnings)

    ledger = vehicle_ledger(vehicle)
    glow = ledger.glow_kg(vehicle.payload_mass_kg)
    twr = _liftoff_twr(vehicle, glow)
    if twr <= 0.0:
        raise PerfError(
            f"起飞推重比 {twr:.3f} ≤ 0，无法进入上升弹道积分",
            suggestion="检查海平面推力与起飞质量（TWR 是动力上升的最低前提）",
        )

    segments, segment_warnings = _build_segments(vehicle, ledger)
    warnings.extend(segment_warnings)

    # 气动输入：Cd 缺省与 L1 同源（DEFAULT_DRAG_COEFFICIENT）；参考面积缺省取
    # 整箭最大截面积（与 budget.py 同一派生口径：DAG 的 vehicle.max_diameter_m）
    aero = vehicle.aero
    if aero is None:
        cd = DEFAULT_DRAG_COEFFICIENT
        warnings.append(
            "Aero 层缺失：气动阻力按默认 Cd=0.3 计算（§6.1 口径，与 L1 同源常量）"
            "——补齐 Vehicle.aero 可收窄该项"
        )
    else:
        cd = aero.drag_coefficient
    dag = propagate_vehicle(vehicle, propellant_mass_kg=ledger.propellant_masses_by_index())
    max_diameter = dag.values.get("vehicle.max_diameter_m")
    if max_diameter is None:
        raise PerfError(
            "DAG 整箭派生量（最大直径）不完整，无法确定气动参考面积",
            suggestion="检查各级参数齐备性（先跑 POST /api/params/diagnose）",
        )
    a_ref = (aero.reference_area_m2 if aero is not None else 0.0) or math.pi * max_diameter**2 / 4.0

    # 自转加成信用（面内分量；不进积分——见模块 docstring「地球模型」）
    rotation_assist = (
        OMEGA_EARTH_RAD_S
        * (EARTH_EQUATOR_RADIUS_M + site.altitude_m)
        * math.cos(math.radians(site.latitude_deg))
        * math.sin(math.radians(site.azimuth_deg))
    )

    # 程序参数绑定为局部量（导数闭包内热路径，避免属性查找）
    vertical_rise_s = prog.vertical_rise_s
    final_gamma_rad = math.radians(prog.final_pitch_deg)
    mode_exponential = prog.mode == "exponential"
    scale_height_m = prog.scale_height_m
    pitch_rate_per_m = math.radians(prog.pitch_rate_deg_per_km) / 1000.0
    sin_alpha_atmo = math.sin(math.radians(prog.alpha_atmo_max_deg))
    sin_alpha_vac = math.sin(math.radians(prog.alpha_vac_max_deg))

    # 地球 / 大气常量绑定（热路径）
    mu = EARTH_GM_M3_S2
    r_e = EARTH_EQUATOR_RADIUS_M
    layers = _ISA_LAYERS_BUILT
    cd_local = cd
    a_ref_local = a_ref

    _N_STATE = 10

    def _deriv(t: float, y: list[float], seg: _Segment) -> list[float]:
        """状态导数（§8.6 L2 力模型）；y = [h, v, γ, x, m, s_g, s_a, s_bp, s_st, s_ideal]。

        控制：γ_cmd(h) 程序剖面 + 逆动力学攻角——跟踪剖面所需的攻角由弹道倾角
        方程反解 ``sin α = (m·v/F)·[dγ_cmd/dt − (v/r − g/v)·cosγ]``，|sin α| > 1
        时饱和（推力不足以跟踪，γ 滞后剖面——不虚构控制力）。
        """
        h = y[0]
        v = y[1]
        gamma = y[2]
        m = y[4]

        # 大气（分层指数；层底精确衔接）
        layer = layers[-1]
        for candidate in reversed(layers):
            if h >= candidate.base_m:
                layer = candidate
                break
        dh = h - layer.base_m
        rho = layer.density_base_kg_m3 * math.exp(-dh / layer.density_scale_height_m)
        pressure = layer.pressure_base_pa * math.exp(-dh / layer.pressure_scale_height_m)

        # 重力转弯程序：γ_cmd(h) 剖面与其高度导数（垂直段内恒 90°、导数 0）
        if t <= vertical_rise_s:
            dgamma_cmd_dh = 0.0
        elif mode_exponential:
            decay = math.exp(-h / scale_height_m)
            dgamma_cmd_dh = -(math.pi / 2.0 - final_gamma_rad) * decay / scale_height_m
        else:
            if (math.pi / 2.0 - final_gamma_rad) - pitch_rate_per_m * h > 0.0:
                dgamma_cmd_dh = -pitch_rate_per_m
            else:
                dgamma_cmd_dh = 0.0

        # 力：推力（背压修正）与气动阻力；变重力
        f_vac = seg.thrust_vacuum_n
        f = f_vac - pressure * seg.exit_area_m2
        drag = 0.5 * rho * v * v * cd_local * a_ref_local
        r = r_e + h
        g = mu / (r * r)
        sin_gamma = math.sin(gamma)
        cos_gamma = math.cos(gamma)

        # 逆动力学攻角：跟踪 γ_cmd 所需的推力偏置（任务书「γ 随高度收敛」的实现）。
        # 饱和界按高度分档（大气内紧、大气外松，见 _ALPHA_* 常量注）。
        if v > 1.0:
            dgamma_cmd_dt = dgamma_cmd_dh * v * sin_gamma
            gravity_curvature = (v / r - g / v) * cos_gamma
            sin_alpha = (m * v / f) * (dgamma_cmd_dt - gravity_curvature)
            if h <= _ALPHA_TRANSITION_LOW_M:
                sin_cap = sin_alpha_atmo
            elif h >= _ALPHA_TRANSITION_HIGH_M:
                sin_cap = sin_alpha_vac
            else:
                ramp = (h - _ALPHA_TRANSITION_LOW_M) / (
                    _ALPHA_TRANSITION_HIGH_M - _ALPHA_TRANSITION_LOW_M
                )
                sin_cap = sin_alpha_atmo + ramp * (sin_alpha_vac - sin_alpha_atmo)
            if sin_alpha > sin_cap:
                sin_alpha = sin_cap
            elif sin_alpha < -sin_cap:
                sin_alpha = -sin_cap
            alpha = math.asin(sin_alpha)
            cos_alpha = math.cos(alpha)
            dv_dt = (f * cos_alpha - drag) / m - g * sin_gamma
            # 球面模型标志项：v²/(R+h) 离心卸载进弹道倾角方程（相对系近似，见 docstring）
            dgamma_dt = (f * sin_alpha) / (m * v) + (v / r - g / v) * cos_gamma
        else:
            # 起飞瞬间 v≈0：攻角 0、γ 恒 90°（剖面尚未接管）
            alpha = 0.0
            cos_alpha = 1.0
            dv_dt = (f - drag) / m - g * sin_gamma
            dgamma_dt = 0.0

        return [
            v * sin_gamma,
            dv_dt,
            dgamma_dt,
            r_e * v * cos_gamma / r,
            -seg.mass_flow_kg_s,
            g * sin_gamma,  # 重力损失累计
            drag / m,  # 气动损失累计
            (f_vac - f) / m,  # 背压损失累计（真空账本口径）
            f * (1.0 - cos_alpha) / m,  # 面内操纵损失累计
            f_vac / m,  # 理想 ΔV 累计（真空账本）
        ]

    # 初态（相对发射点旋转系）：v₀=0、γ₀=90°；x₀=0；m₀=GLOW
    y = [site.altitude_m, 0.0, math.pi / 2.0, 0.0, glow, 0.0, 0.0, 0.0, 0.0, 0.0]
    h_floor = min(site.altitude_m, 0.0) - 1.0
    total_burn_s = segments[-1].t_end_s
    seg_idx = 0
    t = 0.0
    started = time.perf_counter()

    while t < total_burn_s - 1e-9:
        seg = segments[seg_idx]
        step = dt_s if t + dt_s <= seg.t_end_s else seg.t_end_s - t

        # RK4（增广状态同步推进；步内不跨级间边界）
        k1 = _deriv(t, y, seg)
        y2 = [y[i] + 0.5 * step * k1[i] for i in range(_N_STATE)]
        k2 = _deriv(t + 0.5 * step, y2, seg)
        y3 = [y[i] + 0.5 * step * k2[i] for i in range(_N_STATE)]
        k3 = _deriv(t + 0.5 * step, y3, seg)
        y4 = [y[i] + step * k3[i] for i in range(_N_STATE)]
        k4 = _deriv(t + step, y4, seg)
        sixth = step / 6.0
        for i in range(_N_STATE):
            y[i] += sixth * (k1[i] + 2.0 * (k2[i] + k3[i]) + k4[i])
        t += step

        for component in y:
            if not math.isfinite(component):
                raise PerfError(
                    f"上升弹道积分发散（状态量非有限）：t={t:.1f} s，最后状态 h={y[0]:.0f} m, "
                    f"v={y[1]:.1f} m/s, γ={math.degrees(y[2]):.1f}°, m={y[4]:.0f} kg",
                    suggestion="检查程序参数与构型（推力/质量/比冲），必要时减小积分步长",
                )

        # 级间边界：瞬时抛离（助推器干重 / 下级干重）
        if t >= seg.t_end_s - 1e-9:
            t = seg.t_end_s
            y[4] -= seg.jettison_kg
            if seg_idx + 1 < len(segments):
                seg_idx += 1

        # 发散防护（§8.6：报错带最后状态）
        if y[0] < h_floor:
            raise PerfError(
                f"上升弹道触地（积分发散）：t={t:.1f} s，最后状态 h={y[0]:.0f} m, "
                f"v={y[1]:.1f} m/s, γ={math.degrees(y[2]):.1f}°, m={y[4]:.0f} kg——"
                "推力不足以支撑爬升（检查 TWR）或程序转弯过急",
                suggestion=(
                    "提高起飞推重比、加长垂直段，或加大程序标高 scale_height_m 放缓"
                    "末段拉平（程序参数=可调自由度；低 TWR 上面级的重型构型需要更缓的剖面）"
                ),
            )
        escape_speed = math.sqrt(2.0 * mu / (r_e + y[0]))
        if y[1] >= escape_speed:
            raise PerfError(
                f"速度超过第二宇宙速度（积分发散）：t={t:.1f} s，最后状态 h={y[0]:.0f} m, "
                f"v={y[1]:.1f} m/s ≥ v_esc={escape_speed / 1000.0:.2f} km/s",
                suggestion="检查比冲/质量比是否被异常放大（速度超逃逸即不再是上升弹道）",
            )

    compute_ms = (time.perf_counter() - started) * 1000.0

    # 闭合自检：ideal_dv = dv_gained + Σ损失（RK4 增广积分的恒等式，残差应近 0）
    ideal_dv_m_s = y[9]
    gravity_km_s = y[5] / 1000.0
    aero_km_s = y[6] / 1000.0
    back_pressure_km_s = y[7] / 1000.0
    steering_km_s = y[8] / 1000.0
    dv_gained_m_s = y[1] - 0.0
    closure = ideal_dv_m_s - dv_gained_m_s - (y[5] + y[6] + y[7] + y[8])

    losses_total = gravity_km_s + aero_km_s + steering_km_s + back_pressure_km_s
    timeline = tuple(
        StageTimelineEntry(
            segment=seg.label,
            t_start_s=seg.t_start_s,
            t_end_s=seg.t_end_s,
            isp_vacuum_s=seg.isp_vacuum_s,
            thrust_vacuum_n=seg.thrust_vacuum_n,
            mass_flow_kg_s=seg.mass_flow_kg_s,
            exit_area_m2=seg.exit_area_m2,
            propellant_burned_kg=seg.propellant_burned_kg,
            jettison_kg=seg.jettison_kg,
        )
        for seg in segments
    )

    atmosphere_note = (
        "ISA 1976 分层指数近似（US Standard Atmosphere 1976，NOAA/NASA/USAF 1976，表 4："
        "分层 [0,11,20,32,47,51,71,84.852] km；层底气压/密度按 ISA 水静力学逐层精确递推；"
        "层内等温指数，变温层标高按层底/层顶密度两端衔接拟合——§3.1 强制 ISA 1976 口径）"
    )
    provenance: dict[str, str] = {
        "earth_model": (
            "球面不旋转地球，发射点相对系积分（v₀=0、γ₀=90°）；WGS-84 常数复用 "
            "perf.losses（R=6378137 m、GM=3.986004418e14）；g(h)=μ/(R+h)² 变重力；"
            "离心卸载 v²/(R+h) 进弹道倾角方程；科氏/牵连惯性力未建模（~0.03–0.3 m/s²，"
            "M6 简化口径）；自转加成以独立信用项输出（面内分量、不施加 L1 相容因子）"
        ),
        "atmosphere": atmosphere_note,
        "integrator": (
            f"RK4 固定步长 {dt_s} s（步内不跨级间边界）；增广状态含四项损失累计量与"
            f"理想 ΔV 累计，闭合残差 {closure:.2e} m/s"
        ),
        "program": (
            f"重力转弯程序（γ 程序剖面 + 逆动力学攻角）：垂直段 {prog.vertical_rise_s:.1f} s → "
            f"{prog.mode} 模式 γ 随高度收敛到 {prog.final_pitch_deg:.1f}°（"
            + (
                f"标高 {prog.scale_height_m:.0f} m"
                if mode_exponential
                else f"斜率 {prog.pitch_rate_deg_per_km:.2f} °/km"
            )
            + f"）；跟踪所需攻角由弹道倾角方程反解，饱和界 {prog.alpha_atmo_max_deg:.0f}°（稠密段，"
            f"{_ALPHA_TRANSITION_LOW_M / 1000:.0f}–{_ALPHA_TRANSITION_HIGH_M / 1000:.0f} km 过渡）"
            f"→ {prog.alpha_vac_max_deg:.0f}°（真空段，工程惯例）——失配时 γ 滞后剖面，不虚构控制力"
        ),
        "drag": (
            f"Cd 常数 = {cd:.2f}（Vehicle.aero 或缺省 0.3——与 L1 DEFAULT_DRAG_COEFFICIENT "
            f"同源）；参考面积 {a_ref:.2f} m²（缺省取整箭最大截面积，与 budget.py 同口径）；"
            "Ma 分段不引入（无来源系数不进计算，AGENTS 红线）"
        ),
        "back_pressure": (
            "F(h) = F_vac − p(h)·A_e，A_e = (F_vac − F_sl)/p₀（ISA 海平面 101325 Pa，"
            "由 Schema 海平面/真空推力对反推——与 §1.7.5 OI-35 同源）；损失取真空账本"
            "口径 ∫(F_vac − F(h))/m·dt——与 L1「压力/控制余量」同名同义（任务书速记式"
            "∫(F_sl−F(h))/ṁ·dt 按字面积为负值且缺 ṁ/m 权重，实现差异已注明）"
        ),
        "steering": (
            "面内攻角操纵损失 ∫F(h)(1−cosα)/m·dt——L1 转向项是方位角/倾角失配的面外"
            "经验罚项，2D 面内模型无其对应物（口径差异如实对照，不硬凑量级）"
        ),
        "mass_ledger": (
            "质量账复用 perf.capacity.vehicle_ledger（几何解析推进剂 + σ 干重）；"
            "0 级段复用 params.staging.resolve_zero_stage（OI-36 合并口径：并联推力并入"
            "芯一级段、芯级跨段连续核算、段末抛助推器干重）——不另写第二套合并规则；"
            "燃时由质量账派生（m_prop/ṁ），Stage.burn_time_s 仅交叉核对（>20% 告警）"
        ),
    }

    return TrajectoryResult(
        losses_km_s=TrajectoryLosses(
            gravity_km_s=gravity_km_s,
            aero_km_s=aero_km_s,
            steering_km_s=steering_km_s,
            back_pressure_km_s=back_pressure_km_s,
            total_km_s=losses_total,
        ),
        burnout=TrajectoryBurnoutState(
            time_s=t,
            altitude_m=y[0],
            velocity_m_s=y[1],
            flight_path_angle_deg=math.degrees(y[2]),
            downrange_km=y[3] / 1000.0,
            mass_kg=y[4],
            horizontal_velocity_m_s=y[1] * math.cos(y[2]),
        ),
        ideal_dv_km_s=ideal_dv_m_s / 1000.0,
        dv_gained_km_s=dv_gained_m_s / 1000.0,
        closure_residual_m_s=closure,
        rotation_assist_m_s=rotation_assist,
        liftoff_twr=twr,
        glow_kg=glow,
        stage_timeline=timeline,
        warnings=tuple(warnings),
        provenance=provenance,
        compute_ms=compute_ms,
    )


__all__ = [
    "AIR_GAS_CONSTANT_J_KG_K",
    "DEFAULT_DT_S",
    "DT_MAX_S",
    "DT_MIN_S",
    "ISA_DENSITY_SEA_LEVEL_KG_M3",
    "ISA_PRESSURE_SEA_LEVEL_PA",
    "MAX_BURN_TIME_S",
    "StageTimelineEntry",
    "TrajectoryBurnoutState",
    "TrajectoryLosses",
    "TrajectoryProgram",
    "TrajectoryResult",
    "integrate_ascent",
    "isa_density_pressure",
]
