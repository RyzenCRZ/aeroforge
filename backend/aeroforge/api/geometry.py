"""几何同步端点（规格 §10.1 / §16.3）。

- ``POST /api/geometry/contour`` / ``GET /api/geometry/contour/{id}`` —— 母线保存/载入
- ``POST /api/geometry/validate`` —— 纯 Python 校验（**不触 OCCT**，可高频调用）
- ``POST /api/geometry/build`` —— 缓存命中同步返回，否则建异步作业
- ``POST /api/geometry/sections`` —— §5.9 九段分区 + 液面 + 尺寸标注下发
  （纯解析毫秒级，**不走缓存**——与 evaluate 式缓存不同，本端点无内核调用）

硬约束（规格 §9.1 规则 1）：**本模块的任何 handler 都不得调用 OCCT**。
故 ``validate`` 走纯 Python 的 :func:`~aeroforge.geometry.validate.validate_meridian`，
``build`` 只做键计算 + 入队，``sections`` 只消费 :func:`~aeroforge.geometry.assembly.plan_stage`
的纯数值布局（无实体生成）。
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.api.deps import get_runner, get_store
from aeroforge.cache.store import ArtifactStore, compute_key, compute_vehicle_key
from aeroforge.errors import GeometryError
from aeroforge.geometry.assembly import (
    SECTION_ADAPTER,
    SECTION_AVIONICS,
    SECTION_BULKHEAD,
    SECTION_ENGINE_BAY,
    SECTION_FAIRING,
    SECTION_FORWARD_SKIRT,
    SECTION_FUEL_TANK,
    SECTION_INTERTANK,
    SECTION_OX_TANK,
    SECTION_THRUST_STRUCTURE,
    AssemblyError,
    fairing_adapter_heights,
    plan_stage,
)
from aeroforge.geometry.bundle import BoosterSummary
from aeroforge.geometry.meridian import (
    MeridianProfile,
    canonical_json,
    parse_profile,
)
from aeroforge.geometry.validate import ValidationReport, validate_meridian
from aeroforge.params.propellants import fuel_is_lh2, properties
from aeroforge.params.schema import DeliveryPipeRouting, Stage, Vehicle
from aeroforge.paths import contours_root, ensure_dir
from aeroforge.worker.jobs import GeometryJobRunner

router = APIRouter(tags=["geometry"])

#: 母线标识白名单：直接参与文件名拼接，故必须限定字符集（防目录穿越）。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

ContourId = Annotated[str, Path(description="母线标识", pattern=_ID_PATTERN.pattern)]


class ContourSaveRequest(BaseModel):
    """保存母线的请求体。"""

    id: str | None = Field(default=None, description="母线标识；省略时按 name 派生或随机生成")
    profile: MeridianProfile = Field(description="母线剖面（段链，单位米）")


class GeometryBuildRequest(BaseModel):
    """``POST /api/geometry/build`` 的**捆绑构型**请求体（OI-36，M4 简化摘要）。

    与裸剖面请求体并存：省略 ``boosters`` 即等价于旧的裸剖面请求——
    既有客户端的构建路径与缓存键逐字节不变（§9.2）。
    """

    model_config = ConfigDict(extra="forbid")

    profile: MeridianProfile = Field(description="母线剖面（段链，单位米）")
    boosters: BoosterSummary | None = Field(
        default=None,
        description=(
            "M4 简化捆绑摘要（OI-36：count / 直径 / 长度，周向均布）；"
            "省略 = 无助推器，GLB 场景图只含 seg-<i> 节点"
        ),
    )


class VehicleBuildRequest(BaseModel):
    """``POST /api/geometry/build`` 的**车辆形态**请求体（M5，九段分区装配树）。

    从 ``Vehicle`` 的 Stage 参数映射 §5.9 的 9 段轴向分区，GLB 节点名 = 分区名
    （``s<级序>-<分区>`` / ``fin-<k>`` / ``booster-<k>``）；metrics 扩
    ``assembly_tree`` 与 ``common_bulkhead_saving_m``。与剖面形态并存——
    自由母线走既有 profile 形态（节点名 ``seg-<i>``），两条通路互不污染缓存。
    """

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="飞行器参数（含 boosters / 共底 / 扁度 / 尾翼）")


class ContourResponse(BaseModel):
    """母线响应体。``canonical`` 即落盘内容，往返一致的判据（规格 §16.3）。"""

    id: str
    canonical: str = Field(description="canonical JSON：键序固定、浮点定量、无多余空白")
    profile: MeridianProfile


class BuildResponse(BaseModel):
    """``POST /api/geometry/build`` 的响应体（§9.3 缓存优先）。"""

    cache_hit: bool = Field(description="True 表示产物已存在，metrics 直接可用，未创建作业")
    key: str = Field(description="内容寻址缓存键（sha256 十六进制）")
    job_id: str | None = Field(
        default=None, description="未命中时返回；用 /ws/jobs/{job_id} 订阅进度"
    )
    metrics: dict[str, Any] | None = Field(default=None, description="命中时的产物指标")


def _slug(name: str) -> str:
    """把剖面名转成可作文件名的标识；无可保留字符时回退随机串。"""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")[:48]
    return slug or uuid.uuid4().hex[:12]


def _path_for(contour_id: str) -> str:
    if not _ID_PATTERN.match(contour_id):
        msg = f"非法的母线标识：{contour_id!r}"
        raise GeometryError(
            msg,
            suggestion=(
                "标识只允许字母、数字、点、下划线与连字符，且以字母或数字开头（最长 64 字符）"
            ),
            details={"id": contour_id},
        )
    return f"{contour_id}.json"


@router.post("/api/geometry/contour", response_model=ContourResponse)
def save_contour(request: ContourSaveRequest) -> ContourResponse:
    """保存母线：归一化为 canonical JSON 落盘 ``data/contours/<id>.json``。

    落盘内容就是 canonical JSON 本体（不含包装元数据），因此
    "读取 → 再规范化" 必须与原文件**逐字节相同**——这正是往返一致的判据。
    """
    profile = request.profile
    contour_id = request.id or _slug(profile.name)
    filename = _path_for(contour_id)

    canonical = canonical_json(profile)
    target = ensure_dir(contours_root()) / filename
    target.write_text(canonical, encoding="utf-8")

    return ContourResponse(id=contour_id, canonical=canonical, profile=profile)


@router.get("/api/geometry/contour/{contour_id}", response_model=ContourResponse)
def load_contour(contour_id: ContourId) -> ContourResponse:
    """载入母线。文件不存在时返回 404（由 :class:`ArtifactNotFoundError` 映射）。"""
    filename = _path_for(contour_id)
    path = contours_root() / filename
    if not path.is_file():
        msg = f"母线 {contour_id!r} 不存在"
        raise GeometryError(
            msg,
            code="CONTOUR_NOT_FOUND",
            suggestion="先用 POST /api/geometry/contour 保存，或确认标识拼写",
            details={"id": contour_id, "path": str(path)},
        )

    raw = path.read_text(encoding="utf-8")
    profile = parse_profile(raw)
    # 以重新规范化的结果作为返回值：若磁盘内容被外部改动，此处会暴露差异而非静默接受。
    return ContourResponse(id=contour_id, canonical=canonical_json(profile), profile=profile)


@router.post("/api/geometry/validate", response_model=ValidationReport)
def validate_profile(profile: MeridianProfile) -> ValidationReport:
    """母线层校验（纯 Python，无内核）：几何自洽 + G1 + 尖点 + 设计意图断言。

    响应**同时返回分段采样点**（``sample`` / ``outline``），供前端 2D 剖面与示意通道消费。
    这是 ADR-011 的落实方式：端点数值全部来自后端，前端不做任何几何计算。
    """
    return validate_meridian(profile)


@router.post("/api/geometry/build", response_model=BuildResponse)
def build_geometry(
    request: MeridianProfile | GeometryBuildRequest | VehicleBuildRequest,
    runner: Annotated[GeometryJobRunner, Depends(get_runner)],
    store: Annotated[ArtifactStore, Depends(get_store)],
) -> BuildResponse:
    """构建回转几何：**命中即同步返回**，未命中建异步作业（规格 §9.3）。

    请求体兼容三种形态：裸剖面（既有契约）、``{"profile", "boosters"}`` 捆绑构型
    （OI-36）、``{"vehicle"}`` 车辆形态（M5 九段分区装配树）。剖面形态的键对
    boosters 敏感、对省略 boosters 逐字节不敏感（§9.2）；车辆形态的键走
    vehicle canonical，与剖面形态天然分离。
    """
    if isinstance(request, VehicleBuildRequest):
        key = compute_vehicle_key(request.vehicle).key
        if store.is_cached(key):
            metrics = store.load_metrics(key)
            if metrics is not None:
                return BuildResponse(cache_hit=True, key=key, metrics=metrics)
        record = runner.submit_vehicle(request.vehicle)
        return BuildResponse(cache_hit=False, key=key, job_id=record.job_id)

    if isinstance(request, MeridianProfile):
        profile, boosters = request, None
    else:
        profile, boosters = request.profile, request.boosters

    key = compute_key(profile, boosters=boosters).key
    if store.is_cached(key):
        metrics = store.load_metrics(key)
        if metrics is not None:
            return BuildResponse(cache_hit=True, key=key, metrics=metrics)

    record = runner.submit(profile, boosters=boosters)
    return BuildResponse(cache_hit=False, key=key, job_id=record.job_id)


# ---------------------------------------------------------------------------
# POST /api/geometry/sections —— §5.9 九段分区下发（§5.9 / §11.10，M5 第二片）
# ---------------------------------------------------------------------------


class SectionsRequest(BaseModel):
    """``POST /api/geometry/sections`` 的请求体：整箭参数即全部输入。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="飞行器参数（与 /api/geometry/build 车辆形态同构）")


class SectionBand(BaseModel):
    """一个 §5.9 分区条带（§11.10：段高 / 液面 / 标注数值全部后端下发，前端零计算）。

    条带按**自上而下**（§5.9 表次序）排列；缺失分区（无整流罩 / 0 高退化段）不进
    数组——前端按 ``section`` 名寻址渲染，不按下标。
    """

    model_config = ConfigDict(frozen=True)

    section: str = Field(description="§5.9 分区枚举（共底隔板段下发为 common_bulkhead）")
    label_zh: str = Field(description="分区中文名（2D 剖面引线标注用）")
    length_m: float = Field(ge=0.0, description="分区轴向高度（m，后端算出，ADR-011）")
    liquid_level_m: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "液面高度（m，§6.1 h_liq 口径）：**自该箱箱底向上**到液面的轴向高度 = "
            "该箱加注比例 × 箱段柱高；仅贮箱段下发，2D 剖面自箱底向上填充、"
            "气枕区（同色 15% 透明）在液面之上"
        ),
    )
    propellant_oxidizer: str | None = Field(
        default=None, description="氧化剂名（如 LOX）；仅氧箱段下发"
    )
    propellant_fuel: str | None = Field(
        default=None, description="燃料名（如 RP-1）；仅燃料箱段下发"
    )
    color_key: str | None = Field(
        default=None, description="推进剂编码色键（§11.2：lox / rp1 / lh2 / ch4…）；仅贮箱段下发"
    )
    bulkhead_saving_m: float | None = Field(
        default=None,
        description="共底级长缩减量（m，两箱相邻封头矢高和 − 隔板矢高，公式反算）；仅隔板段下发",
    )
    insulation: bool | None = Field(
        default=None,
        description="LH₂ 侧隔热标志（燃料为液氢 = true）；仅隔板段下发",
    )


class StageSections(BaseModel):
    """一级（或一组助推器）的条带布置与输送管走法。"""

    model_config = ConfigDict(frozen=True)

    stage_index: int = Field(
        description="级序：芯级为 stages[] 的 0 基下标；助推器记 0（GCAT 记法）"
    )
    level: int = Field(description="级号：芯级自 1 起（自下而上）；助推器 = 0")
    bands: tuple[SectionBand, ...] = Field(description="§5.9 分区条带（自上而下）")
    delivery_pipe_routing: DeliveryPipeRouting = Field(
        description="输送管走法（取上箱字段：穿越下箱的管段属上箱，§5.9 口径 3）"
    )
    tank_order: Literal["oxidizer_first", "fuel_first"] = Field(
        description="储箱排列：自顶向下先出现的是氧化剂箱（oxidizer_first）还是燃料箱（fuel_first）"
    )


class DimensionLabel(BaseModel):
    """一条尺寸标注（§5.9 共性 7：数值后端下发，前端只排版）。"""

    model_config = ConfigDict(frozen=True)

    key: str = Field(
        description=(
            "标注键：整箭级 total_length / max_diameter / fairing_diameter；"
            "逐级前缀 s<级号>_（助推器 b<组序>_）+ ox_tank_length / fuel_tank_length / "
            "forward_skirt_height / thrust_structure_height / "
            "intertank_height | common_bulkhead_height"
        )
    )
    text: str = Field(description="显示文本（如 12.30 m）")


class SectionDimensions(BaseModel):
    """整箭量测 + 尺寸标注全清单（§11.10 坐标映射基准）。"""

    model_config = ConfigDict(frozen=True)

    total_length_m: float = Field(description="整箭总长（m，含整流罩 / 适配器；不含助推器）")
    max_diameter_m: float = Field(description="芯级最大直径（m；整流罩直径单列）")
    fairing_diameter_m: float | None = Field(
        default=None, description="整流罩直径（m；无整流罩为 null）"
    )
    labels: tuple[DimensionLabel, ...] = Field(description="尺寸标注全清单（§5.9 共性 7）")


class SectionsResponse(BaseModel):
    """``POST /api/geometry/sections`` 的响应体：2D 外观图与工程剖面图共用的唯一数据源。"""

    model_config = ConfigDict(frozen=True)

    stages: tuple[StageSections, ...] = Field(
        description="芯级条带（自下而上排列，与 stages[] 同序）"
    )
    boosters: tuple[StageSections, ...] = Field(
        description="助推器组条带（每组一项，同构 bands；级号 0）"
    )
    dimensions: SectionDimensions = Field(description="整箭量测与尺寸标注")
    warnings: tuple[str, ...] = Field(description="绘制相关警告（如共底 LH₂ 侧缺隔热层）")
    provenance: dict[str, str] = Field(description="近似方式与口径声明（§5.9 派生规则 1）")


#: §5.9 分区枚举 → 2D 剖面中文名（引线标注）。
_BAND_LABELS_ZH: dict[str, str] = {
    SECTION_FAIRING: "整流罩",
    SECTION_ADAPTER: "载荷适配器",
    SECTION_AVIONICS: "仪器舱",
    SECTION_FORWARD_SKIRT: "前裙",
    SECTION_OX_TANK: "氧化剂箱",
    SECTION_INTERTANK: "级间舱",
    SECTION_BULKHEAD: "共底隔板",
    SECTION_FUEL_TANK: "燃料箱",
    SECTION_THRUST_STRUCTURE: "推力结构",
    SECTION_ENGINE_BAY: "发动机舱",
}

#: 装配树分区枚举 → sections 契约的 section 名（契约定死：共底隔板段 = common_bulkhead）。
_BAND_SECTION_NAMES: dict[str, str] = {
    SECTION_BULKHEAD: "common_bulkhead",
}


def _color_key(propellant_name: str) -> str:
    """推进剂名 → 编码色键（§11.2）：LOX→lox、RP-1→rp1、LH2→lh2……确定性派生。"""
    return propellant_name.lower().replace("-", "").replace(" ", "")


def _stage_bands(stage: Stage, *, top_stage: bool, vehicle: Vehicle) -> list[SectionBand]:
    """一级的条带列表（自上而下）：plan_stage 布局 + 顶级整流罩 / 适配器前置。"""
    layout = plan_stage(stage)
    props = properties(stage.propellant)
    bands: list[SectionBand] = []

    if top_stage:
        top_heights = fairing_adapter_heights(vehicle)
        if top_heights is not None:
            adapter_height, fairing_height = top_heights
            bands.append(
                SectionBand(
                    section=SECTION_FAIRING,
                    label_zh=_BAND_LABELS_ZH[SECTION_FAIRING],
                    length_m=fairing_height,
                )
            )
            bands.append(
                SectionBand(
                    section=SECTION_ADAPTER,
                    label_zh=_BAND_LABELS_ZH[SECTION_ADAPTER],
                    length_m=adapter_height,
                )
            )

    for band in reversed(layout.bands):
        if band.length <= 1e-12:
            continue  # 0 高退化段（如缺省仪器舱）不进数组
        section_name = _BAND_SECTION_NAMES.get(band.section, band.section)
        item = SectionBand(
            section=section_name,
            label_zh=_BAND_LABELS_ZH[band.section],
            length_m=band.length,
        )
        if band.section in (SECTION_OX_TANK, SECTION_FUEL_TANK):
            role = "oxidizer" if band.section == SECTION_OX_TANK else "fuel"
            tank = stage.geometry.oxidizer_tank if role == "oxidizer" else stage.geometry.fuel_tank
            # 液面 = 该箱加注比例 × 箱段柱高（2D 显示口径；QA-2：级层比例用于性能账）
            item = item.model_copy(
                update={
                    "liquid_level_m": tank.fill_fraction * band.length,
                    "propellant_oxidizer": props.oxidizer if role == "oxidizer" else None,
                    "propellant_fuel": props.fuel if role == "fuel" else None,
                    "color_key": _color_key(props.oxidizer if role == "oxidizer" else props.fuel),
                }
            )
        elif band.section == SECTION_BULKHEAD:
            item = item.model_copy(
                update={
                    "bulkhead_saving_m": layout.saving_m,
                    "insulation": fuel_is_lh2(stage.propellant),
                }
            )
        bands.append(item)
    return bands


@router.post("/api/geometry/sections", response_model=SectionsResponse)
def vehicle_sections(request: SectionsRequest) -> SectionsResponse:
    """§5.9 九段分区下发：2D 工程剖面图与外观图的**同一份**后端数据源（§11.10）。

    纯解析（plan_stage 布局 + 分区高度事实），毫秒级、不触 OCCT、**不走缓存**
    （evaluate 式缓存针对重内核，此处无必要）。段高 / 液面 / 标注数值全部由后端
    算出（ADR-011），前端只做映射与排版。布局不可行（分区铺不满 / 矢高干涉）时
    返回 422（GEOMETRY_INVALID）。
    """
    vehicle = request.vehicle
    try:
        stages: list[StageSections] = []
        total_length = 0.0
        max_band_radius = 0.0
        provenance: dict[str, str] = {
            "近似方式": (
                "分段柱体近似：箱长 = 容积/截面积（截面积按该箱直径），封头为椭球"
                "（矢高 = 扁度系数×直径/2，缺省 0.5）；"
                "容积比 V_ox/V_fuel = (O/F)·ρ_fuel/ρ_ox（§5.9）"
            ),
            "液面高度": ("液面 = 该箱加注比例 × 箱段柱高（2D 显示口径；内核按 build123d 对拍）"),
            "级间段": (
                "两级之间的级间段（interstage）不单独切分：Schema 无高度输入，"
                "级长全部预算按九段分摊（§5.9 共性 2 / 装配树留白 2）"
            ),
            "共底缩减量": (
                "saving = 两箱相邻封头矢高和 − 隔板矢高（公式反算，不得手填，§5.9 口径 2）；"
                "隔板段高度 = 隔板矢高（装配树分区事实，绘制为单线）"
            ),
        }
        warnings: list[str] = []

        for position, stage in enumerate(sorted(vehicle.stages, key=lambda s: s.index)):
            layout = plan_stage(stage)
            bands = _stage_bands(
                stage, top_stage=position == len(vehicle.stages) - 1, vehicle=vehicle
            )
            upper_tank = (
                stage.geometry.oxidizer_tank
                if stage.geometry.tank_arrangement == "oxidizer_upper"
                else stage.geometry.fuel_tank
            )
            stages.append(
                StageSections(
                    stage_index=position,
                    level=stage.index,
                    bands=tuple(bands),
                    delivery_pipe_routing=upper_tank.delivery_pipe_routing,
                    tank_order=(
                        "oxidizer_first"
                        if stage.geometry.tank_arrangement == "oxidizer_upper"
                        else "fuel_first"
                    ),
                )
            )
            total_length += layout.height
            for band in layout.bands:
                max_band_radius = max(max_band_radius, band.radius_start, band.radius_end)
            if stage.intertank_height_m is not None and not stage.geometry.common_bulkhead:
                provenance[f"stages[{position}].intertank_height_m"] = (
                    "用户显式指定（覆盖「两箱相邻封头矢高和」派生规则）"
                )
            if stage.avionics_height_m:
                provenance[f"stages[{position}].avionics_height_m"] = "用户显式指定（缺省 0 高）"
            if (
                stage.geometry.common_bulkhead
                and fuel_is_lh2(stage.propellant)
                and stage.geometry.fuel_tank.common_bulkhead_insulation_m is None
            ):
                warnings.append(
                    f"第 {stage.index} 级共底且燃料为液氢，但未给出隔热层厚度——"
                    "2D 剖面的隔热层（斜线填充）无法绘制（§5.9 口径 2③）"
                )

        # 总长 = Σ级装配高 + 顶级适配器 / 整流罩（与装配树 z_offset 同口径）
        top_heights = fairing_adapter_heights(vehicle)
        if top_heights is not None:
            adapter_height, fairing_height = top_heights
            total_length += adapter_height + fairing_height

        boosters: list[StageSections] = []
        for group_position, group in enumerate(vehicle.boosters):
            layout = plan_stage(group.stage)
            bands = _stage_bands(group.stage, top_stage=False, vehicle=vehicle)
            upper_tank = (
                group.stage.geometry.oxidizer_tank
                if group.stage.geometry.tank_arrangement == "oxidizer_upper"
                else group.stage.geometry.fuel_tank
            )
            boosters.append(
                StageSections(
                    stage_index=0,
                    level=0,
                    bands=tuple(bands),
                    delivery_pipe_routing=upper_tank.delivery_pipe_routing,
                    tank_order=(
                        "oxidizer_first"
                        if group.stage.geometry.tank_arrangement == "oxidizer_upper"
                        else "fuel_first"
                    ),
                )
            )
            if group_position == 0:
                provenance["boosters"] = "助推器条带按侧级 Stage 同构推导（每组一项；级号 0）"

        labels: list[DimensionLabel] = [
            DimensionLabel(key="total_length", text=f"{total_length:.2f} m"),
            DimensionLabel(key="max_diameter", text=f"{max_band_radius * 2.0:.2f} m"),
        ]
        if vehicle.fairing_diameter_m is not None:
            labels.append(
                DimensionLabel(key="fairing_diameter", text=f"{vehicle.fairing_diameter_m:.2f} m")
            )
            provenance["fairing_height_m"] = (
                "用户显式指定"
                if vehicle.fairing_height_m is not None
                else "工程惯例常量 min(max(2.2×整流罩直径, 5), 20) m（可显式指定 fairing_height_m）"
            )
        # 尺寸标注全清单（§5.9 共性 7）：键逐级前缀 s<级号>（助推器 b<组序>，0 基）
        labelled = [*((f"s{entry.level}", entry) for entry in stages)]
        labelled += [(f"b{position}", entry) for position, entry in enumerate(boosters)]
        for prefix, entry in labelled:
            for item in entry.bands:
                if item.section == SECTION_OX_TANK:
                    labels.append(
                        DimensionLabel(
                            key=f"{prefix}_ox_tank_length", text=f"{item.length_m:.2f} m"
                        )
                    )
                elif item.section == SECTION_FUEL_TANK:
                    labels.append(
                        DimensionLabel(
                            key=f"{prefix}_fuel_tank_length", text=f"{item.length_m:.2f} m"
                        )
                    )
                elif item.section == SECTION_FORWARD_SKIRT:
                    labels.append(
                        DimensionLabel(
                            key=f"{prefix}_forward_skirt_height", text=f"{item.length_m:.2f} m"
                        )
                    )
                elif item.section == SECTION_THRUST_STRUCTURE:
                    labels.append(
                        DimensionLabel(
                            key=f"{prefix}_thrust_structure_height", text=f"{item.length_m:.2f} m"
                        )
                    )
                elif item.section == SECTION_INTERTANK:
                    labels.append(
                        DimensionLabel(
                            key=f"{prefix}_intertank_height", text=f"{item.length_m:.2f} m"
                        )
                    )
                elif item.section == "common_bulkhead":
                    labels.append(
                        DimensionLabel(
                            key=f"{prefix}_common_bulkhead_height", text=f"{item.length_m:.2f} m"
                        )
                    )

        return SectionsResponse(
            stages=tuple(stages),
            boosters=tuple(boosters),
            dimensions=SectionDimensions(
                total_length_m=total_length,
                max_diameter_m=max_band_radius * 2.0,
                fairing_diameter_m=vehicle.fairing_diameter_m,
                labels=tuple(labels),
            ),
            warnings=tuple(warnings),
            provenance=provenance,
        )
    except AssemblyError as exc:
        raise GeometryError(
            str(exc),
            suggestion=(
                "分区必须恰好铺满级长且封头不干涉（§5.9 / §5.5）；请调整级长、扁度或发动机高度"
            ),
        ) from exc
