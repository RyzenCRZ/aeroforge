"""质量估算层（规格 §8.4，M4 计算内核第二片）。

双来源 + 交叉校验（§8.4 表逐行）
--------------------------------
- **几何解析**：柱段 + 椭球封头的贮箱容积 / 湿面积 × 面密度。纯数值解析公式，
  **不碰 OCCT**（几何内核属 M5 装配树，本层只做质量量级的工程估算）。
- **统计回归**：GCAT stages 表按（级序位置、推进剂大类）分箱的 σ 中位数 + 分位
  区间。**只作对照 / 缺省建议，不覆盖用户输入的 σ**（σ 是存储权威，§6.1）。
- **交叉校验**：几何解析干重 vs 用户 σ 推算干重，偏差 > 20% 报警（§8.4 一致性
  校验）——**单来源不得作为结论**（AGENTS 领域规则）。

几何模型口径（工程惯例估算，非权威）
------------------------------------
- 封头 = 半椭球，矢高 ``h = 扁度系数 × 直径 / 2``（OI-37；None 兜底 0.5，
  即 2:1 椭圆封头 → h = d/4，与压力容器标准 2:1 封头口径一致）。
- 封头表面积用 Knud Thomsen 近似（p = 1.6075，误差 < 1.5%，半球 / 圆盘
  退化极限均正确）。
- 两箱柱段长度：``Tank.length_m`` 显式给定 → 用户权威（§5.9 派生规则 2，不静默
  覆盖）；缺失 → 按 §5.9 容积比 ``V_ox/V_fuel = (O/F)·(ρ_fuel/ρ_ox)`` 分配
  **可用长度**（级长 − 发动机高度）。Schema 的 Tank 层**不存容积**（派生量不
  存储是 §6.1 唯一权威原则），用户显式通道只有 ``length_m``，故优先级为：
  显式箱长 > §5.9 容积比派生。
- 推进剂质量 = 氧箱容积 × ρ_ox + 燃料箱容积 × ρ_fuel，再乘**级层**加注比例
  （QA-2：级层加注比例是 M4 定尺求解的整体输入）。
- 几何干重 = 湿面积 × 面密度；面密度 = 材料库典型壁厚 × ρ_material（**工程惯例
  估算**：刻意不用用户的 Tank.wall_thickness_m，保持几何来源独立于用户细观输入）。
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from aeroforge.data.models import EngineRecord
from aeroforge.data.repository import CatalogRepository
from aeroforge.params import propellants
from aeroforge.params.dag import volumetric_ratio
from aeroforge.params.materials import get_material
from aeroforge.params.schema import Stage

#: OI-37：扁度系数缺省值（2:1 椭圆封头）。兜底发生在本层（派生处），Schema 存 None。
DEFAULT_FLATNESS_RATIO = 0.5

#: §8.4 一致性校验阈值：几何解析干重与 σ 推算干重偏差超过 20% 即报警。
CROSS_CHECK_THRESHOLD = 0.2

#: 回归分箱的最小样本数：低于该值的箱并入相邻箱（§8.4 回归要求 + 任务口径）。
MIN_BIN_SIZE = 5

#: §8.4 回归要求「只用 official / literature 标签的样本」——其余 quality 值跳过并计数。
_ALLOWED_QUALITY = frozenset({"official", "literature"})

#: GCAT 中「氧化剂缺失但燃料为已知液体单组元」的燃料清单（它们不是固体）：
#: 双组元液体机必填氧化剂，固体与单组元都不填；不区分会把单组元上面级误入固体箱。
_MONOPROPELLANT_FUELS = frozenset({"Hydrazine", "Hydrazine?", "N2", "NH3OHNO3 AF-M315E"})

Position = Literal["booster", "first", "upper", "unknown", "pooled"]
"""样本的级序位置（由 stage_links 的 Stage_No 投票；pooled = 小箱合并兜底）。"""

PropellantClass = Literal["liquid", "solid", "unknown", "mixed"]
"""推进剂大类（由发动机氧化剂/燃料字段判定；mixed = 合并兜底箱）。"""

#: §8.4 的 σ 有效域（按 位置 × 大类）。助推器液体箱规格未单列——按一级液体口径
#: 对照并显式注明；unknown / pooled 箱无规格区间，只报告不判定。
SIGMA_INTERVALS: dict[tuple[str, str], tuple[float, float]] = {
    ("first", "liquid"): (0.04, 0.08),
    ("upper", "liquid"): (0.08, 0.15),
    ("booster", "liquid"): (0.04, 0.08),
    ("first", "solid"): (0.08, 0.12),
    ("upper", "solid"): (0.08, 0.12),
    ("booster", "solid"): (0.08, 0.12),
}

#: 分箱合并时的位置邻接序（同级最近箱按此距离选取）。
_POSITION_ORDER: dict[str, int] = {"booster": 0, "first": 1, "upper": 2, "unknown": 3}


# ---------------------------------------------------------------------------
# 几何解析（纯数值公式）
# ---------------------------------------------------------------------------


def dome_height_m(diameter_m: float, flatness_ratio: float | None) -> float:
    """封头矢高（m）：``h = 扁度系数 × d / 2``。

    扁度系数 = 封头椭球短轴 / 长轴（OI-37）。2:1 椭圆封头（系数 0.5）的矢高为
    d/4，与压力容器标准的 2:1 半椭圆封头一致；系数 1.0 退化为半球（h = d/2）。
    """
    ratio = DEFAULT_FLATNESS_RATIO if flatness_ratio is None else flatness_ratio
    return ratio * diameter_m / 2.0


def dome_volume_m3(diameter_m: float, dome_height_m_: float) -> float:
    """单个半椭球封头容积（m³）：``(2/3)·π·(d/2)²·h``。"""
    return 2.0 / 3.0 * math.pi * (diameter_m / 2.0) ** 2 * dome_height_m_


def dome_surface_area_m2(diameter_m: float, dome_height_m_: float) -> float:
    """单个半椭球封头表面积（m²），Knud Thomsen 近似（p = 1.6075，误差 < 1.5%）。

    旋转椭球取赤道半径 ``a = d/2``、极半轴 ``b = h``：整球面积
    ``S ≈ 4π·((2·(ab)^p + a^(2p))/3)^(1/p)``，封头取其半。退化极限自检：
    ``h = d/2``（半球）→ 2π(d/2)² 精确成立；``h → 0`` → π(d/2)²（圆盘）成立。
    """
    a = diameter_m / 2.0
    b = dome_height_m_
    p = 1.6075
    spheroid: float = 4.0 * math.pi * ((2.0 * (a**p) * (b**p) + a ** (2.0 * p)) / 3.0) ** (1.0 / p)
    return spheroid / 2.0


def tank_volume_m3(diameter_m: float, cylinder_length_m: float, dome_height_m_: float) -> float:
    """贮箱容积（m³）= 柱段 + 前后两个椭球封头。"""
    cylinder = math.pi * diameter_m**2 / 4.0 * cylinder_length_m
    return cylinder + 2.0 * dome_volume_m3(diameter_m, dome_height_m_)


def tank_wetted_area_m2(
    diameter_m: float, cylinder_length_m: float, dome_height_m_: float
) -> float:
    """贮箱湿面积（m²）= 柱段侧面积 + 两个封头表面积。"""
    cylinder = math.pi * diameter_m * cylinder_length_m
    return cylinder + 2.0 * dome_surface_area_m2(diameter_m, dome_height_m_)


@dataclass(frozen=True, slots=True)
class TankGeometryEstimate:
    """单箱几何解析结果（工程惯例估算，非权威）。"""

    role: str
    """oxidizer / fuel。"""

    diameter_m: float
    cylinder_length_m: float
    dome_height_m: float
    volume_m3: float
    wetted_area_m2: float
    length_source: Literal["user", "derived"]
    """柱段长度来源：显式箱长（user，§5.9 规则 2）/ §5.9 容积比派生（derived）。"""


def resolve_tank_geometry(
    stage: Stage, *, reserved_m: float = 0.0
) -> tuple[TankGeometryEstimate, TankGeometryEstimate]:
    """解析一级的两箱几何（氧化剂箱、燃料箱）。

    箱长优先级（读 Schema 裁定）：Tank 层**不存容积**（派生量不存储，§6.1），
    用户显式通道只有 ``Tank.length_m``——两箱都显式 → 直接用；只有一箱显式 →
    另一箱按 §5.9 容积比由显式箱长锚定；都缺 → 按容积比分配可用长度（级长 −
    发动机高度 − ``reserved_m``）。

    ``reserved_m``（M5 装配树引入，默认 0 = 既有口径）：从可用长度中额外扣除的
    轴向预留（封头占位 / 共底隔板段等）。装配布局传该参数使九段分区恰好铺满
    级长；性能评估链不传（保持 §8.4 既有工程估算口径）——**同一函数、两种调用
    约定**，不另立第二套箱体解析（M5 任务口径）。
    """
    ox_tank = stage.geometry.oxidizer_tank
    fuel_tank = stage.geometry.fuel_tank
    props = propellants.properties(stage.propellant)

    ox_diameter = ox_tank.diameter_m or stage.diameter_m
    fuel_diameter = fuel_tank.diameter_m or stage.diameter_m
    ratio = volumetric_ratio(
        stage.engine.mixture_ratio, props.density_ox_kg_m3, props.density_fuel_kg_m3
    )

    ox_length: float | None = ox_tank.length_m
    fuel_length: float | None = fuel_tank.length_m
    ox_source: Literal["user", "derived"] = "user" if ox_length is not None else "derived"
    fuel_source: Literal["user", "derived"] = "user" if fuel_length is not None else "derived"

    if ox_length is not None and fuel_length is not None:
        pass  # 两箱均显式：用户权威，直接用（§5.9 规则 2，不静默覆盖）
    elif ox_length is not None:
        fuel_length = ox_length / ratio  # 由显式氧箱长按 §5.9 容积比锚定
    elif fuel_length is not None:
        ox_length = fuel_length * ratio
    else:
        available = stage.length_m - stage.engine_height_m - reserved_m
        if available <= 0.0:
            msg = (
                f"第 {stage.index} 级可用箱长非正（级长 {stage.length_m} m − 发动机高度 "
                f"{stage.engine_height_m} m − 轴向预留 {reserved_m:.6f} m = {available:.6f} m），"
                "无法按 §5.9 分配两箱柱长"
            )
            raise ValueError(msg)
        # §5.9 容积比 → 柱长比（两箱截面积按各自直径，容积比与柱长比仅在等直径时
        # 严格相等；分母按各箱截面积折算，避免异径箱分配失真）
        ox_area = math.pi * ox_diameter**2 / 4.0
        fuel_area = math.pi * fuel_diameter**2 / 4.0
        # V_ox/V_fuel = ratio，V = A·L（柱段）⇒ L_ox·A_ox = ratio·L_fuel·A_fuel
        # L_ox + L_fuel = available ⇒ L_fuel = available·A_ox/(A_ox + ratio·A_fuel)
        fuel_length = available * ox_area / (ox_area + ratio * fuel_area)
        ox_length = available - fuel_length

    def _estimate(
        role: str, diameter: float, length: float, source: Literal["user", "derived"]
    ) -> TankGeometryEstimate:
        height = dome_height_m(diameter, stage.flatness_ratio)
        return TankGeometryEstimate(
            role=role,
            diameter_m=diameter,
            cylinder_length_m=length,
            dome_height_m=height,
            volume_m3=tank_volume_m3(diameter, length, height),
            wetted_area_m2=tank_wetted_area_m2(diameter, length, height),
            length_source=source,
        )

    return (
        _estimate("oxidizer", ox_diameter, ox_length, ox_source),
        _estimate("fuel", fuel_diameter, fuel_length, fuel_source),
    )


def propellant_mass_kg(stage: Stage) -> float:
    """几何解析的推进剂质量（kg）：各箱容积 × 本剂密度之和 × 级层加注比例。"""
    ox, fuel = resolve_tank_geometry(stage)
    props = propellants.properties(stage.propellant)
    return (
        ox.volume_m3 * props.density_ox_kg_m3 + fuel.volume_m3 * props.density_fuel_kg_m3
    ) * stage.fill_fraction


def tank_dry_masses_kg(stage: Stage, *, reserved_m: float = 0.0) -> tuple[float, float]:
    """按箱分列的几何解析干重（kg）：``(氧化剂箱, 燃料箱)``。

    与 :func:`dry_mass_geometric_kg` 同式（湿面积 × 面密度；面密度 = 材料库
    ``typical_min_wall_thickness_m × density_kg_m3``，工程惯例估算——刻意不用
    用户的壁厚输入，保持本来源独立于细观参数）。M5 装配树按箱取质量贡献时调用
    （``reserved_m`` 语义同 :func:`resolve_tank_geometry`）。
    """
    ox, fuel = resolve_tank_geometry(stage, reserved_m=reserved_m)
    masses: list[float] = []
    for estimate, material_id in (
        (ox, stage.geometry.oxidizer_tank.material),
        (fuel, stage.geometry.fuel_tank.material),
    ):
        material = get_material(material_id)
        areal_density = material.typical_min_wall_thickness_m * material.density_kg_m3
        masses.append(estimate.wetted_area_m2 * areal_density)
    return (masses[0], masses[1])


def dry_mass_geometric_kg(stage: Stage) -> float:
    """几何解析干重（kg）= 两箱湿面积 × 面密度之和。"""
    return sum(tank_dry_masses_kg(stage))


@dataclass(frozen=True, slots=True)
class CrossCheckOutcome:
    """§8.4 一致性校验结果（几何解析 vs σ 推算）。"""

    m_dry_geometric_kg: float
    """几何解析干重（湿面积 × 面密度）。"""

    m_dry_sigma_kg: float
    """σ 推算干重 = 几何解析推进剂质量 × σ/(1−σ)（σ 是存储权威，作为对照基准）。"""

    relative_deviation: float
    """相对偏差 = |几何 − σ| / σ（σ 推算值为基准）。"""

    exceeds_threshold: bool
    """是否超过 §8.4 的 20% 阈值（超过 → 调用方须以 warning 呈现）。"""

    warning: str | None
    """超过阈值时的警示文案；未超过为 None。"""


def cross_check(stage: Stage) -> CrossCheckOutcome:
    """§8.4 一致性校验：几何解析干重 vs 用户 σ 推算干重。

    σ 推算干重以**几何解析推进剂质量**为基数（同一容积口径下比较结构效率），
    偏差 > 20% 意味着构型异常或外推（§8.4）——单来源不得作为结论。
    """
    m_prop = propellant_mass_kg(stage)
    sigma = stage.structure_coefficient
    m_dry_sigma = m_prop * sigma / (1.0 - sigma)
    m_dry_geo = dry_mass_geometric_kg(stage)
    deviation = abs(m_dry_geo - m_dry_sigma) / m_dry_sigma if m_dry_sigma > 0.0 else math.inf
    exceeds = deviation > CROSS_CHECK_THRESHOLD
    return CrossCheckOutcome(
        m_dry_geometric_kg=m_dry_geo,
        m_dry_sigma_kg=m_dry_sigma,
        relative_deviation=deviation,
        exceeds_threshold=exceeds,
        warning=(
            f"第 {stage.index} 级双来源交叉校验：几何解析干重 {m_dry_geo:.1f} kg 与 "
            f"σ={sigma} 推算干重 {m_dry_sigma:.1f} kg 偏差 {deviation:.1%}，"
            f"超过 {CROSS_CHECK_THRESHOLD:.0%} 阈值（§8.4：构型异常或回归外推，"
            "单来源不得作为结论）"
            if exceeds
            else None
        ),
    )


# ---------------------------------------------------------------------------
# 统计回归（GCAT stages 表；取数只经仓储层公共 API，本模块零 SQL）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SigmaSample:
    """一个回归样本：某级记录的 σ 及其分箱维度。"""

    stage_name: str
    position: Position
    propellant_class: PropellantClass
    sigma: float


@dataclass(frozen=True, slots=True)
class SigmaBin:
    """一个分箱的统计（中位数 + 四分位，§8.4 口径的简化回归）。"""

    position: Position
    propellant_class: PropellantClass
    sample_count: int
    sigma_median: float
    sigma_q1: float
    sigma_q3: float
    interval: tuple[float, float] | None
    """§8.4 的 σ 有效域（无对应区间时为 None，只报告不判定）。"""

    out_of_interval: bool
    """箱中位数是否越出 §8.4 有效域（越界 → 显式警告，不得静默外推）。"""


@dataclass(frozen=True, slots=True)
class SigmaBinSummary:
    """分箱统计汇总（含合并留痕与越界警告）。"""

    bins: tuple[SigmaBin, ...]
    merge_notes: tuple[str, ...]
    """样本不足箱的合并去向（§8.4：合并必须记录，不静默）。"""

    warnings: tuple[str, ...]
    """σ 中位数越出 §8.4 有效域的箱级警告。"""

    total_samples: int


@dataclass(frozen=True, slots=True)
class SigmaRegressionReport(SigmaBinSummary):
    """GCAT 回归完整报告（含覆盖率账目，供 provenance / 测试如实断言）。"""

    snapshot_id: str
    snapshot_release: str
    total_stage_rows: int
    """stages 表总行数（覆盖率分母）。"""

    dual_non_missing_rows: int
    """full_mass_kg / dry_mass_kg 双非缺失的行数（含 dry ≥ full 的病态行）。"""

    valid_rows: int
    """物理有效样本数（0 < dry < full 且 quality 合格）——进入分箱的样本。"""

    skipped_quality_rows: int = 0
    """因 quality 不在 official/literature 白名单而被跳过的行数。"""

    method_note: str = field(
        default=(
            "分箱中位数 + 四分位（inclusive 分位）统计；非连续回归——"
            "§8.4 的 R² 要求按任务口径降为本统计形态"
        )
    )


def classify_propellant(engine: EngineRecord | None) -> PropellantClass:
    """推进剂大类判定：有氧化剂 → 液体（双组元）；氧化剂缺失且燃料非已知单组元
    液体 → 固体；无发动机记录 → unknown。"""
    if engine is None:
        return "unknown"
    if engine.oxidizer is not None:
        return "liquid"
    if engine.fuel in _MONOPROPELLANT_FUELS:
        return "liquid"
    return "solid"


def resolve_position(votes: Counter[str]) -> Position:
    """由 stage_links 的 Stage_No 投票裁定级序位置。

     GCAT 的 Stage_No：1 = 一级、≥ 2 = 上面级、≤ 0（0 / −1）= 助推器；非数字
    （' F' 整流罩 / ' C' 过渡段等）不参与投票。同一级名出现在多种位置时取**多数**
     （并列时按 一级 > 上面级 > 助推器 优先，保证确定性）。
    """
    if not votes:
        return "unknown"
    precedence = ("first", "upper", "booster")
    best = max(votes.items(), key=lambda pair: (pair[1], -precedence.index(pair[0])))
    return best[0]  # type: ignore[return-value]  # votes 的键只来自 precedence 三值


def summarize_sigma_bins(samples: Sequence[SigmaSample]) -> SigmaBinSummary:
    """把样本按（位置 × 大类）分箱并统计（纯函数，供回归与测试共用）。

    合并规则：样本 < :data:`MIN_BIN_SIZE` 的箱并入**同级**（同推进剂大类）中位置
    最近的大箱；同级无大箱 → 并入混合兜底箱（pooled / mixed）。每次合并都写入
    ``merge_notes``，不静默。§8.4 区间对照：箱中位数越界 → warnings 显式警告。
    """
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sample in samples:
        groups[(sample.position, sample.propellant_class)].append(sample.sigma)

    big_keys = {key for key, values in groups.items() if len(values) >= MIN_BIN_SIZE}
    merged: dict[tuple[str, str], list[float]] = {key: list(groups[key]) for key in big_keys}
    merge_notes: list[str] = []
    pooled: list[float] = []

    for key in sorted(groups, key=lambda k: (k[1], _POSITION_ORDER.get(k[0], 9))):
        if key in big_keys:
            continue
        n = len(groups[key])
        same_class = sorted(
            (candidate for candidate in big_keys if candidate[1] == key[1]),
            key=lambda candidate: (
                abs(_POSITION_ORDER[candidate[0]] - _POSITION_ORDER.get(key[0], 9)),
                _POSITION_ORDER[candidate[0]],
            ),
        )
        if same_class:
            target = same_class[0]
            merged[target].extend(groups[key])
            merge_notes.append(
                f"箱 {key}（n={n} < {MIN_BIN_SIZE}）并入同级最近箱 {target}（§8.4 合并留痕）"
            )
        else:
            pooled.extend(groups[key])
            merge_notes.append(
                f"箱 {key}（n={n} < {MIN_BIN_SIZE}）无同级大箱，并入混合兜底箱（pooled/mixed）"
            )
    if pooled:
        merged[("pooled", "mixed")] = pooled

    bins: list[SigmaBin] = []
    warnings: list[str] = []
    for key in sorted(merged, key=lambda k: (k[1], _POSITION_ORDER.get(k[0], 9))):
        values = merged[key]
        median = statistics.median(values)
        quartiles = statistics.quantiles(values, n=4, method="inclusive")
        interval = SIGMA_INTERVALS.get(key)
        out = interval is not None and not (interval[0] <= median <= interval[1])
        bins.append(
            SigmaBin(
                position=key[0],  # type: ignore[arg-type]
                propellant_class=key[1],  # type: ignore[arg-type]
                sample_count=len(values),
                sigma_median=median,
                sigma_q1=quartiles[0],
                sigma_q3=quartiles[2],
                interval=interval,
                out_of_interval=out,
            )
        )
        if out and interval is not None:
            note = (
                "（§8.4 未单列助推器，按一级液体口径对照）" if key == ("booster", "liquid") else ""
            )
            warnings.append(
                f"箱 {key} 的 σ 中位数 {median:.4f} 越出 §8.4 有效域 "
                f"[{interval[0]}, {interval[1]}]{note}——统计口径与规格区间存在分歧，"
                "该箱只作对照，不得作为缺省建议直接引用"
            )

    return SigmaBinSummary(
        bins=tuple(bins),
        merge_notes=tuple(merge_notes),
        warnings=tuple(warnings),
        total_samples=len(samples),
    )


def regress_structure_coefficients(repository: CatalogRepository) -> SigmaRegressionReport:
    """从 GCAT stages 表回归结构系数 σ（§8.4 统计回归来源）。

    样本口径：``full_mass_kg`` / ``dry_mass_kg`` 双非缺失、0 < dry < full（σ ∈ (0,1)）、
    quality ∈ {official, literature}。级序位置由 vehicles × stage_links 的 Stage_No
    投票裁定；推进剂大类由 engines 表氧化剂/燃料判定。回归值**只作对照 / 缺省建议**，
    不覆盖用户输入的 σ（σ 是存储权威）。
    """
    stages = repository.query_stages()
    engines: dict[str, EngineRecord] = {}
    for engine in repository.query_engines():
        engines.setdefault(engine.name, engine)  # 同名取首见（record_id 序，留痕口径同仓储层）

    votes: dict[str, Counter[str]] = {}
    for vehicle in repository.query_vehicles():
        variant = vehicle.variant if vehicle.variant else "-"
        for link in repository.stage_links(vehicle.name, variant):
            if not link.stage_name:
                continue
            try:
                stage_no = float(link.stage_no) if link.stage_no else math.nan
            except ValueError:
                continue  # ' F' / ' C' 等非数字段号不参与投票
            if math.isnan(stage_no):
                continue
            if stage_no == 1:
                position = "first"
            elif stage_no >= 2:
                position = "upper"
            else:
                position = "booster"
            votes.setdefault(link.stage_name, Counter())[position] += 1

    samples: list[SigmaSample] = []
    dual_non_missing = 0
    valid = 0
    skipped_quality = 0
    for record in stages:
        if record.full_mass_kg is None or record.dry_mass_kg is None:
            continue
        dual_non_missing += 1
        if record.quality not in _ALLOWED_QUALITY:
            skipped_quality += 1
            continue
        full = record.full_mass_kg
        dry = record.dry_mass_kg
        if not (full > 0.0 and dry > 0.0 and dry < full):
            continue  # dry ≥ full 的病态行（适配器 / 舱段等无推进剂构件）如实剔除
        valid += 1
        engine_record = engines.get(record.engine_name) if record.engine_name else None
        samples.append(
            SigmaSample(
                stage_name=record.name,
                position=resolve_position(votes.get(record.name, Counter())),
                propellant_class=classify_propellant(engine_record),
                sigma=dry / full,
            )
        )

    summary = summarize_sigma_bins(samples)
    snapshot = repository.snapshot()
    return SigmaRegressionReport(
        bins=summary.bins,
        merge_notes=summary.merge_notes,
        warnings=summary.warnings,
        total_samples=summary.total_samples,
        snapshot_id=snapshot.id,
        snapshot_release=snapshot.release,
        total_stage_rows=len(stages),
        dual_non_missing_rows=dual_non_missing,
        valid_rows=valid,
        skipped_quality_rows=skipped_quality,
    )


__all__ = [
    "CROSS_CHECK_THRESHOLD",
    "DEFAULT_FLATNESS_RATIO",
    "MIN_BIN_SIZE",
    "SIGMA_INTERVALS",
    "CrossCheckOutcome",
    "Position",
    "PropellantClass",
    "SigmaBin",
    "SigmaBinSummary",
    "SigmaRegressionReport",
    "SigmaSample",
    "TankGeometryEstimate",
    "classify_propellant",
    "cross_check",
    "dome_height_m",
    "dome_surface_area_m2",
    "dome_volume_m3",
    "dry_mass_geometric_kg",
    "propellant_mass_kg",
    "regress_structure_coefficients",
    "resolve_position",
    "resolve_tank_geometry",
    "summarize_sigma_bins",
    "tank_dry_masses_kg",
    "tank_volume_m3",
    "tank_wetted_area_m2",
]
