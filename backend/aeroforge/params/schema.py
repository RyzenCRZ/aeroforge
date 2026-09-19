"""参数系统 Schema 分层（规格 §6.1）。

分层（与 §6.1 表逐行对应）
--------------------------
``Vehicle`` 总纲 · ``Stage`` 单级 · ``Geometry`` 构型 · ``Engine`` 发动机 ·
``Tank`` 贮箱 · ``Aero`` 气动 · ``LaunchSite`` 发射场 · ``Sequence`` 任务时序 ·
``Recovery`` 回收复用 · ``Mission`` 任务。

本模块只管**结构**（类型 / 枚举 / 值域 / 必填）
----------------------------------------------
跨字段与领域约束（加注比例上限、共底隔热、轨道要素齐备……）落在
:mod:`aeroforge.params.constraints`：它们要给出**字段级** ``suggestion``，
而 pydantic 的校验器一旦抛出，``loc`` 只剩模型层级、字段路径就丢了（跨层断言
被包成 ``ValidationError`` 是本项目已踩过的坑）。这样分工之后：

- 结构错 → :func:`to_diagnostics` 把 ``ValidationError`` 翻成字段级裁定；
- 领域错 → 约束引擎逐条给出带字段路径的裁定；
- 两者输出**同一形状**（§6.3 / §6.5），前端只解析一种。

唯一权威原则（§6.1）
--------------------
- **σ（``structure_coefficient``）是结构质量的唯一存储权威**：干质比 ``σ/(1−σ)``
  与推进剂质量分数 ``1−σ`` 是同一自由度的等价表示，一律由
  :mod:`aeroforge.params.dag` 派生，**禁止**再设独立字段（否则同一物理量有三份可
  互相矛盾的存储）。
- **加注比例**上限 1.0（OI-03）；干质比、容积、标称满装量、液面高度、总长、最大直径
  均为派生量，不在此层存储。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aeroforge.geometry.meridian import MeridianProfile
from aeroforge.params.propellants import PropellantCombination
from aeroforge.params.report import Diagnostic
from aeroforge.params.units import si_field

#: 参数 Schema 版本号：随飞行器一起存储（§6.1）。破坏性变更需迁移函数。
PARAMS_SCHEMA_VERSION = "1"

# ---------------------------------------------------------------------------
# 枚举（一律用 Literal：既进 OpenAPI 枚举、又让前端由 gen:api 拿到字面量联合）
# ---------------------------------------------------------------------------

TankType = Literal["separate", "common_bulkhead"]
"""贮箱类型：非共底 / 共底（§6.1 Tank 层）。"""

CommonBulkheadType = Literal["insulated_sandwich", "plain"]
"""共底类型：隔热夹层（蜂窝，§5.9 共性 3）/ 普通隔板。"""

TankArrangement = Literal["oxidizer_upper", "fuel_upper"]
"""储箱排列。⚠ §5.9 明示这是**非铁律**：S-IC / S-II / Falcon 9 是氧在上，
S-IVB 是氢在上——故必须是用户可配置项，不得在实现里写死。"""

FeedSystem = Literal["pump_fed", "pressure_fed"]
"""输送方式两类（§5.9 共性 6）：泵压式 / 挤压式。"""

DeliveryPipeRouting = Literal["external", "internal"]
"""输送管走法（§5.9 四项口径 3）：外置沿箭体 / 内置穿越下箱。"""

StageSeparationType = Literal["cold_staging", "hot_staging", "none"]
"""级间段类型。⚠ **级间段（两级之间）** 与 **级间舱 intertank（同级两箱之间）**
是两个不同部件（§5.9 共性 2），不可混用同一字段。"""

IspSource = Literal["default", "custom"]
"""比冲来源：取自发动机定义 / 用户自定义。"""

EngineCycle = Literal["gas_generator", "staged_combustion", "expander", "pressure_fed"]
"""发动机循环方式。"""

PropellantPhase = Literal["liquid", "solid", "hybrid"]
"""发动机的推进剂相态（§1.7.3 OI-30）。

业界通行做法是把它做成**发动机上的一个枚举属性**——OpenRocket 的发动机定义带
``Type ∈ {single-use, hybrid, reloadable}``，RocketPy 直接把电机分成
``SolidMotor`` / ``HybridMotor`` / ``LiquidMotor`` 等变体类。**不**由别处（如推进剂
组合或贮箱构成）推导，也**不**另设"是否固体助推"布尔字段——那会与相态形成两份真相
（唯一权威原则 §6.1 的同族错误，R-28）。

默认 ``liquid``：绝大多在役运载火箭为液体构型，且 Schema 扩展不得破坏既有参数
（与 OI-21～OI-26 同口径）。本次实际采用的档位会写进 §6.5 推重比规则的账目并随报告
下发，故默认值**不构成隐式判定**。
"""

OrbitType = Literal["LEO", "SSO", "GTO", "GEO", "TLI", "TMI", "escape", "custom"]
"""目标轨道类型（§6.1 Mission 层；TLI / TMI 由 OI-22 补入）。"""

RecoveryMethod = Literal["parachute", "propulsive"]
"""回收方式：伞降 / 动力反推着陆。"""

SequenceEventType = Literal[
    "ignition", "stage_separation", "fairing_jettison", "orbit_insertion", "landing"
]
"""任务时序事件类型（§6.1 Sequence 层）。"""


class ParamsModel(BaseModel):
    """参数层模型的共同基类：``frozen`` + 拒绝未知字段。

    ``extra="forbid"`` 不是洁癖：本项目**裁决即规格**，未声明的参数一律不得进入
    （R-28）——静默吞掉一个拼错的字段，等于让"用户以为改了、其实没改"。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# 构件层
# ---------------------------------------------------------------------------


class Tank(ParamsModel):
    """贮箱（§6.1 Tank 层）。

    ``volume_m3`` / ``nominal_full_mass_kg`` / ``liquid_level_m`` 是**派生量**，
    不在此层存储（唯一权威原则）——它们由后端算出并随结果下发。
    """

    tank_type: TankType = Field(description="贮箱类型：非共底 / 共底")
    diameter_m: float | None = si_field(
        "length", "贮箱直径（省略 = 继承该级直径）", default=None, gt=0.0
    )
    length_m: float | None = si_field(
        "length", "用户显式给定的箱长（省略 = 由后端按 §5.9 派生）", default=None, gt=0.0
    )
    wall_thickness_m: float = si_field("length", "壁厚", gt=0.0)
    material: str = Field(description="材料（M3 起由材料库校验）")
    fill_fraction: float = Field(
        gt=0.0,
        description="加注比例 = 实际加注量 / 满装量；上限 1.0（OI-03，由约束引擎判定）",
    )
    max_fill_mass_kg: float | None = si_field(
        "mass", "最大允许加注量（仅在超装场景显式启用，§6.1 / OI-03）", default=None, gt=0.0
    )
    feed_system: FeedSystem = Field(description="输送方式：泵压式 / 挤压式")
    delivery_pipe_routing: DeliveryPipeRouting = Field(
        default="external", description="输送管走法（§5.9 默认外置沿箭体）"
    )
    common_bulkhead_insulation_m: float | None = si_field(
        "length", "共底隔热层厚度（共底且 LH₂ 侧必填，§5.9 口径 2）", default=None, gt=0.0
    )


class Engine(ParamsModel):
    """发动机（§6.1 Engine 层）。混合比是箱体比例派生的输入（§5.9）。"""

    model: str = Field(description="型号")
    propellant_phase: PropellantPhase = Field(
        default="liquid",
        description=(
            "推进剂相态（§1.7.3 OI-30）：§6.5 起飞推重比**固体档（1.5）的唯一判据**。"
            "默认 liquid —— 实际采用的档位会印在推重比规则的账目里，故非隐式判定"
        ),
    )
    cycle: EngineCycle = Field(description="循环方式")
    chamber_pressure_pa: float = si_field("pressure", "室压", gt=0.0)
    expansion_ratio: float = Field(gt=1.0, description="喷管膨胀比 ε（面积比）")
    efficiency_factor: float = Field(
        gt=0.0, le=1.0, description="效率因子（c* 或 C_F 效率，1.0 = 理想）"
    )
    # 环境条件口径（§1.7.5 OI-35）：海平面 = ISA 标准海平面（0 m、15 °C、101.325 kPa）；
    # 真空 = 理想真空（背压 0）。字段名必须携带 _sea_level_ / _vacuum_，无环境限定的
    # "推力/比冲"视为缺陷；配对规则：ṁ 只用真空配真空，T/W 只用海平面总推力配 GLOW。
    thrust_sea_level_n: float = si_field(
        "force",
        "海平面推力（ISA 标准海平面：海拔 0 m、15 °C、101.325 kPa）",
        gt=0.0,
    )
    thrust_vacuum_n: float = si_field(
        "force",
        "真空推力（理想真空、背压 0 的理论值）",
        gt=0.0,
    )
    isp_sea_level_s: float = si_field(
        "isp",
        "海平面比冲（ISA 标准海平面：海拔 0 m、15 °C、101.325 kPa）",
        gt=0.0,
    )
    isp_vacuum_s: float = si_field(
        "isp",
        "真空比冲（理想真空、背压 0 的理论值）",
        gt=0.0,
    )
    mixture_ratio: float = Field(
        gt=0.0, description="混合比 O/F（氧化剂/燃料 质量比）——§5.9 箱体比例派生的唯一输入"
    )


class Aero(ParamsModel):
    """气动（简化，§6.1 Aero 层）。

    ⚠ 二者均为**工程惯例值，非权威来源**（规格表已标注）；MVP 只做简化模型，
    **不做 CFD**（非目标 §1.3）。缺失时按默认值并附 ``warning``。
    """

    drag_coefficient: float = Field(
        gt=0.0, description="阻力系数 Cd（工程惯例值，非权威来源；M4 起可用基准火箭反标定）"
    )
    reference_area_m2: float | None = Field(
        default=None, gt=0.0, description="参考面积（m²；省略 = 取最大截面积，由后端派生）"
    )


class LaunchSite(ParamsModel):
    """发射场（§6.1 LaunchSite 层）。

    纬度是**自转加成与转向损失的唯一输入**（§8.6）：计算中不得另设常量，
    否则"改纬度运力不变"这类缺陷会在任何一处悄悄复现。
    """

    name: str = Field(description="发射场名称")
    latitude_deg: float = Field(ge=-90.0, le=90.0, description="纬度（°，北正南负）")
    altitude_m: float = si_field("length", "海拔（m，可为负）")
    azimuth_deg: float = Field(
        ge=0.0, lt=360.0, description="发射方位角（°，自正北顺时针；默认值，可被 Mission 覆盖）"
    )


class LossFactors(ParamsModel):
    """ΔV 损失项系数（§8.6 的损失构成）。

    M2 只做**存储与透传**；损失模型 L1 的实算属 M4（§16）。刻意**不设默认值猜测**：
    缺省即 0 并在结果中标注为"未计入"，而不是填一个看着合理的经验数（§1.4-4）。
    """

    gravity: float = Field(default=0.0, ge=0.0, description="重力损失（Δv 份额）")
    drag: float = Field(default=0.0, ge=0.0, description="气动阻力损失（Δv 份额）")
    steering: float = Field(default=0.0, ge=0.0, description="转向损失（Δv 份额）")
    back_pressure: float = Field(
        default=0.0, ge=0.0, description="背压损失（与「压力/控制余量」同义，§8.8）"
    )


class Mission(ParamsModel):
    """任务与轨道要素（§6.1 Mission 层；轨道要素由 OI-21 补入）。"""

    orbit_type: OrbitType = Field(description="目标轨道类型")
    altitude_m: float | None = si_field(
        "length", "圆轨道高度（LEO / SSO / GEO 用）", default=None, gt=0.0
    )
    inclination_deg: float | None = Field(
        default=None, ge=0.0, le=180.0, description="轨道倾角（°）"
    )
    eccentricity: float | None = Field(default=None, ge=0.0, lt=1.0, description="偏心率")
    perigee_altitude_m: float | None = si_field(
        "length", "近地点高度（GTO / TLI / TMI 用）", default=None, gt=0.0
    )
    apogee_altitude_m: float | None = si_field(
        "length", "远地点高度（GTO / TLI / TMI 用）", default=None, gt=0.0
    )
    launch_site: LaunchSite | None = Field(default=None, description="内联发射场（与 id 二选一）")
    launch_site_id: str | None = Field(default=None, description="发射场引用（与内联二选一）")
    loss_factors: LossFactors | None = Field(default=None, description="ΔV 损失项系数（§8.6）")
    wind_profile_ref: str | None = Field(default=None, description="风廓线引用（M6，可空）")


class Geometry(ParamsModel):
    """构型（§6.1 Geometry 层），**每级一个**。

    整箭外轮廓（§5.2 的母线段列表）挂在 :attr:`Vehicle.profile`：M1 的母线描述的是
    **整箭纵剖面**，而共底 / 储箱排列 / 两箱本身是**逐级**属性——若把二者放进一个
    ``Geometry``，多级火箭就会出现"一份轮廓对应多个构型"的歧义。

    ⚠ 装配关系（§6.1 的示例字段之一）**不在此层**：它随 M5 装配树一并建模（§16）。
    """

    common_bulkhead: bool = Field(default=False, description="共底设计开关（§5.9 默认关闭）")
    common_bulkhead_type: CommonBulkheadType | None = Field(
        default=None, description="共底类型（共底开启时必填，由约束引擎判定）"
    )
    tank_arrangement: TankArrangement = Field(
        default="oxidizer_upper", description="储箱排列（§5.9 非铁律，用户可配置）"
    )
    fins_enabled: bool = Field(default=False, description="尾翼 / 稳定面是否启用")
    oxidizer_tank: Tank = Field(description="氧化剂箱")
    fuel_tank: Tank = Field(description="燃料箱")


class Stage(ParamsModel):
    """单级（§6.1 Stage 层）。自下而上编号，``index`` 1 = 第一级。"""

    index: int = Field(ge=1, description="级序号：1 = 第一级（自下而上）")
    propellant: PropellantCombination = Field(description="该级推进剂组合")
    diameter_m: float = si_field("length", "级直径", gt=0.0)
    length_m: float = si_field("length", "级高度（含级间段）", gt=0.0)
    wall_thickness_m: float = si_field("length", "级壁厚", gt=0.0)
    material: str = Field(description="材料（M3 起由材料库校验）")
    structure_coefficient: float = Field(
        gt=0.0, lt=1.0, description="结构系数 σ = m_dry/(m_dry+m_prop)（**存储权威**，§6.1）"
    )
    fill_fraction: float = Field(gt=0.0, description="加注比例（上限 1.0，OI-03）")
    max_fill_mass_kg: float | None = si_field(
        "mass", "最大允许加注量（仅超装场景显式启用）", default=None, gt=0.0
    )
    engine_count: int = Field(ge=1, description="发动机台数")
    engine: Engine = Field(description="该级发动机定义")
    engine_height_m: float = si_field(
        "length", "发动机高度（安装基准面 → 喷管出口端面，含喷管）", gt=0.0
    )
    engine_height_nozzle_excluded_m: float | None = si_field(
        "length", "发动机高度（不含喷管）", default=None, gt=0.0
    )
    burn_time_s: float | None = Field(
        default=None, gt=0.0, description="工作时间（s）；省略时由后端按 m_prop/ṁ 派生"
    )
    interstage_type: StageSeparationType = Field(
        description="级间段类型（该级与其**上级之间**的分离段；不是级间舱，§5.9 共性 2）"
    )
    # 唯一权威原则（QA-1，v0.6.1 续）：发动机标称比冲是权威，级层只在**覆写**时才存值。
    # isp_source="default" ⇒ 两字段必须省略，后端从 engine 复制下发；
    # "custom" ⇒ 两字段必填（约束引擎判定 HARD_ISP_CUSTOM_REQUIRES_VALUES）。
    isp_vacuum_s: float | None = si_field(
        "isp",
        "该级真空比冲（仅 isp_source=custom 时填写；省略 = 取发动机标称值）",
        default=None,
        gt=0.0,
    )
    isp_sea_level_s: float | None = si_field(
        "isp",
        "该级海平面比冲（仅 isp_source=custom 时填写；省略 = 取发动机标称值）",
        default=None,
        gt=0.0,
    )
    isp_source: IspSource = Field(
        description=(
            "比冲来源：default = 取自 engine 定义（省略 isp_* 字段）；custom = 用户覆写（必填）"
        )
    )
    recoverable: bool = Field(default=False, description="可回收性（回收方案见 Recovery 层）")
    geometry: Geometry = Field(description="该级构型（共底 / 储箱排列 / 两箱）")


# ---------------------------------------------------------------------------
# 任务层
# ---------------------------------------------------------------------------


class SequenceEvent(ParamsModel):
    """一个任务时序事件（§6.1 Sequence 层）。"""

    event: SequenceEventType = Field(description="事件类型")
    time_s: float | None = Field(default=None, ge=0.0, description="相对时刻（s）")
    trigger: str | None = Field(default=None, description="触发条件（自由文本，如「燃尽压降 5%」）")
    stage_index: int | None = Field(default=None, ge=1, description="关联级号（可空）")


class Sequence(ParamsModel):
    """任务时序（§6.1 Sequence 层）。"""

    events: tuple[SequenceEvent, ...] = Field(min_length=1, description="事件列表（按发生顺序）")


class Recovery(ParamsModel):
    """回收与复用（§6.1 Recovery 层）。"""

    enabled: bool = Field(default=False, description="是否回收")
    stage_indices: tuple[int, ...] = Field(default=(), description="回收的级号")
    method: RecoveryMethod | None = Field(default=None, description="回收方式")
    landing_propellant_margin_fraction: float | None = Field(
        default=None, ge=0.0, le=1.0, description="着陆推进剂余量（占该级满装量）"
    )
    system_mass_kg: float | None = si_field("mass", "回收系统质量代价", default=None, ge=0.0)


class Vehicle(ParamsModel):
    """飞行器总纲（§6.1 Vehicle 层）——前端与后端之间传递的唯一顶层参数对象。

    ``total_length_m`` / ``max_diameter_m`` 等**派生量不在此层**：它们由
    :mod:`aeroforge.params.dag` 算出并随结果下发（前端零几何计算，ADR-011）。
    """

    schema_version: str = Field(default=PARAMS_SCHEMA_VERSION, description="参数 Schema 版本号")
    name: str = Field(description="火箭名称")
    stages: tuple[Stage, ...] = Field(min_length=1, description="自下而上（index 1 = 第一级）")
    payload_mass_kg: float = si_field("mass", "有效载荷质量", ge=0.0)
    fairing_diameter_m: float | None = si_field("length", "整流罩直径", default=None, gt=0.0)
    material: str = Field(description="箭体材料（全局默认，可被 Stage 覆盖）")
    propellant: PropellantCombination = Field(description="推进剂组合（全局默认，可被 Stage 覆盖）")
    aero: Aero | None = Field(default=None, description="气动（缺失时按默认值并附 warning）")
    profile: MeridianProfile | None = Field(
        default=None, description="整箭母线剖面（§5.2 的 2D→3D 入口；M1 已落地）"
    )
    mission: Mission = Field(description="任务与轨道")
    sequence: Sequence | None = Field(default=None, description="任务时序")
    recovery: Recovery | None = Field(default=None, description="回收与复用")


# ---------------------------------------------------------------------------
# ValidationError → 字段级裁定（§6.3：硬约束必须给出字段级错误）
# ---------------------------------------------------------------------------

#: pydantic 错误类型 → 修复方向。缺项走兜底文案，**不允许**留空。
_SUGGESTIONS: dict[str, str] = {
    "missing": "该字段为必填项，请补齐后再提交",
    "extra_forbidden": "存在未声明的字段：请核对拼写（新增参数须先过规格 §1.7 裁决，R-28）",
    "float_parsing": "需要数值：请检查是否误填了单位、千分位或文本",
    "int_parsing": "需要整数",
    "bool_parsing": "需要 true / false",
    "string_type": "需要文本",
    "literal_error": "取值必须来自该字段的枚举清单（见 OpenAPI schema 的 enum）",
    "model_type": "需要对象：请按 OpenAPI schema 的分层结构填写",
    "list_type": "需要数组",
    "value_error": "取值不满足字段约束，请检查该项的取值范围与单位",
}

#: 上下界错误类型 → 判据符号，用于把 pydantic 的 ctx 还原成一句可读的判据。
_BOUND_SYMBOLS: dict[str, str] = {
    "greater_than": ">",
    "greater_than_equal": "≥",
    "less_than": "<",
    "less_than_equal": "≤",
}


def _field_path(loc: tuple[str | int, ...]) -> str:
    """pydantic 的 ``loc`` → 字段路径字符串（``('stages', 0, 'x')`` → ``stages[0].x``）。"""
    path = ""
    for item in loc:
        if isinstance(item, int):
            path += f"[{item}]"
        else:
            path += ("." if path else "") + str(item)
    return path or "vehicle"


#: 请求位置标记（FastAPI 的 ``RequestValidationError`` 会在 ``loc`` 前加一个）。
_REQUEST_LOCATIONS = frozenset({"body", "query", "path", "header", "cookie"})


def _normalise_loc(loc: tuple[str | int, ...]) -> tuple[str | int, ...]:
    """去掉 FastAPI 加在 ``loc`` 前的**请求位置标记**（通常是 ``"body"``）。

    实测缺陷（由 ``test_api_params`` 暴露）：同一个嵌套字段错误，
    pydantic 给的 ``loc`` 是 ``('stages', 0, 'engine', 'mixture_ratio')``，
    而 FastAPI 给的是 ``('body', 'stages', 0, ...)``——若原样翻译，
    界面拿到的路径就成了 ``body.stages[0].engine.mixture_ratio``，与 §6.3 约束层产出的
    ``stages[0].engine.mixture_ratio`` 差一层前缀。两处清单本应共用同一张
    "路径 → 控件"映射（见 :mod:`aeroforge.params.report`），差这一层就全部对不上，
    而错误本身仍然"看起来很正常"。
    """
    if loc and isinstance(loc[0], str) and loc[0] in _REQUEST_LOCATIONS:
        return loc[1:]
    return loc


def _suggestion(error_type: str, ctx: dict[str, Any]) -> str:
    """把错误类型翻成**可操作**的建议；带上下界的把界值写进去。"""
    symbol = _BOUND_SYMBOLS.get(error_type)
    if symbol is not None:
        bound = next(iter(ctx.values()), None)
        return f"取值必须 {symbol} {bound}（内部一律 SI，红线 §1.4-3）"
    return _SUGGESTIONS.get(error_type, "请按 OpenAPI schema 校正该字段的值与类型")


def to_diagnostics(exc: ValidationError) -> list[Diagnostic]:
    """pydantic 结构校验失败 → §6.3 的字段级裁定清单。

    一律判为 ``hard``：请求体不符合 Schema 即无法进入任何后续计算，
    放行只会把错误推到更晚、更难定位的地方（"没报错 ≠ 正确"）。
    """
    return errors_to_diagnostics(exc.errors())


def errors_to_diagnostics(errors: Iterable[Mapping[str, Any]]) -> list[Diagnostic]:
    """把 pydantic 风格的错误列表翻成裁定清单。

    与 :func:`to_diagnostics` 分开，是因为 FastAPI 的 ``RequestValidationError``
    **不是** pydantic 的 ``ValidationError``（两者只有同名方法 ``errors()``，
    MRO 上无继承关系）——API 层只能交出错误列表，不能交出那个异常类型。
    两者的 ``loc`` 还差一层请求位置前缀，故此处先统一规范化（见 :func:`_normalise_loc`）。
    """
    collected: list[Mapping[str, Any]] = [
        {**error, "loc": _normalise_loc(tuple(error["loc"]))} for error in errors
    ]
    items: list[Diagnostic] = []
    for error in collected:
        if _is_spurious_length_error(error, collected):
            continue
        error_type = str(error["type"])
        ctx = error.get("ctx") or {}
        items.append(
            Diagnostic(
                level="hard",
                code=f"PARAMS_{error_type.upper()}",
                field_path=_field_path(tuple(error["loc"])),
                message=str(error["msg"]),
                suggestion=_suggestion(error_type, dict(ctx)),
            )
        )
    return items


def _is_spurious_length_error(error: Mapping[str, Any], errors: list[Mapping[str, Any]]) -> bool:
    """容器级 ``too_short`` 是否由某个**元素自身报错**连带产生。

    pydantic 判定带 ``min_length`` 的序列字段时，会**丢掉校验失败的元素再数个数**：
    于是只要 ``stages[0]`` 里某个字段错了，就会**同时**报一条"stages 至少 1 项"——
    而用户明明填了两级。这条附带错误会把人引向完全错误的方向（去数级数，而不是改那个字段）。

    判据是**可证伪的**：只要还有一条错误落在该容器的某个**下标**之下（证明容器里确实有元素），
    这条 ``too_short`` 就一定是假的。只处理 ``too_short`` 这一形态，不碰其它错误类型，
    也不会掩盖真正的"空列表"——此时不存在任何下标错误，本函数返回 ``False``。
    """
    if error.get("type") != "too_short":
        return False
    loc = tuple(error["loc"])
    for other in errors:
        other_loc = tuple(other["loc"])
        if (
            len(other_loc) > len(loc)
            and other_loc[: len(loc)] == loc
            and isinstance(other_loc[len(loc)], int)
        ):
            return True
    return False
