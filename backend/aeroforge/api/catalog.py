"""目录域端点（规格 §7.4 / §10.1）。

- ``GET /api/catalog/materials`` —— 自建材料库全量查询（§7.4「材料库数据源裁决」：
  SPACEMATDB 无明示开放许可，不抓取；本库 Python 内嵌，逐条带来源与质量标签）。
- ``GET /api/catalog/engines`` —— 发动机族谱（按族聚合；缺省返回族清单 + 计数，
  给 ``family`` 返回该族记录）。
- ``GET /api/catalog/vehicles/search`` —— OI-39 型号检索（GCAT lv 全库规范化子串
  匹配；内置精校模板置顶标「精校」，用户点选，系统不静默代选）。
- ``GET /api/catalog/vehicles/record`` —— OI-39 型号已知参数集（lv + stage_links +
  stages + engines 逐字段带出处；缺失显式列出、禁止编造）。

族谱落地口径（§7.4，M3 第五片）
-------------------------------
数据取自 GCAT engines 表（``family`` 列）。GCAT **结构性缺失**的字段（循环方式、
室压、膨胀比、海平面推力、海平面 Isp）在响应里以 ``missing_fields`` **显式列出**
（§7.9 手动补录的缺口，禁止编造）；``typical_thrust_n`` 官方文档未声明环境口径
（OI-35），**不参与任何配对计算、不派生推重比**——比强度 / 比刚度同理只出现在
材料端点的**派生值**字段里（P1：派生量不存储）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from aeroforge.api.deps import get_catalog
from aeroforge.data.models import (
    EngineRecord,
    SnapshotMeta,
    StageRecord,
    VehicleRecord,
)
from aeroforge.data.repository import CatalogRepository, VehicleRecordBundle
from aeroforge.errors import CatalogNotFoundError
from aeroforge.params.materials import (
    MATERIALS,
    MaterialCategory,
    MaterialQuality,
    specific_stiffness,
    specific_strength,
)

router = APIRouter(tags=["catalog"])

#: GCAT 结构性缺失、须 §7.9 手动补录的字段（§7.4：显式列出，禁止编造）。
ENGINE_MISSING_FIELDS: tuple[str, ...] = (
    "cycle",
    "chamber_pressure_pa",
    "expansion_ratio",
    "thrust_sea_level_n",
    "isp_sea_level_s",
)


class MaterialOut(BaseModel):
    """一条材料记录（§7.4 材料库表 + 派生比强度 / 比刚度计算值）。"""

    id: str = Field(description="材料 id（Schema material 字段的引用值）")
    name: str = Field(description="牌号名（含热处理态）")
    category: MaterialCategory = Field(
        description="类别：铝合金 / 不锈钢 / 钛合金 / 复材 / 高温合金"
    )
    density_kg_m3: float = Field(description="密度（kg/m³）")
    elastic_modulus_pa: float = Field(description="弹性模量（Pa，室温）")
    yield_strength_pa: float = Field(description="屈服强度（Pa，室温典型值；复材为许用典型）")
    service_temp_min_c: float = Field(description="工作温度下限（°C，量级表述）")
    service_temp_max_c: float = Field(description="工作温度上限（°C，量级表述）")
    typical_min_wall_thickness_m: float = Field(
        description="典型工艺壁厚下限（m，工程惯例典型值；壁厚判据未显式配置时的回落档）"
    )
    heat_treatment: str = Field(description="热处理 / 试验条件（同一合金不同状态性能差数倍）")
    source: str | None = Field(
        description="出处文字（公开手册 / 标准 / 厂商 datasheet）；typical 条目可为 null"
    )
    quality: MaterialQuality = Field(
        description="质量标签：literature = 实测手册值；typical = 工程典型值（不得用于结论性导出）"
    )
    specific_strength_m2_s2: float = Field(
        description="比强度 σ_y/ρ（m²/s²，即 N·m/kg）——派生计算值，不随库存储（P1）"
    )
    specific_stiffness_m2_s2: float = Field(
        description="比刚度 E/ρ（m²/s²，即 N·m/kg）——派生计算值，不随库存储（P1）"
    )


class MaterialsResponse(BaseModel):
    """``GET /api/catalog/materials`` 的响应体。"""

    materials: tuple[MaterialOut, ...] = Field(description="材料库全量（§7.4 自建库）")


class EngineFamilySummary(BaseModel):
    """族谱清单中的一行：族名 + 计数。"""

    family: str = Field(description="族名（GCAT engines 的 family 列）")
    count: int = Field(description="该族的发动机记录数")


class EngineRecordOut(BaseModel):
    """族谱里的一台发动机（GCAT engines 行的读取侧投影）。

    ⚠ 不含任何派生字段：推重比**不派生**——``typical_thrust_n`` 环境未声明
    （OI-35），代入任何配对计算都是编造。
    """

    record_id: int = Field(description="记录主键（溯源锚点，provenance 用）")
    name: str = Field(description="型号名称")
    manufacturer: str | None = Field(description="制造商（机构代码，经 orgs 可解析）")
    family: str | None = Field(description="族名")
    oxidizer: str | None = Field(description="氧化剂（源值原样）")
    fuel: str | None = Field(description="燃料（源值原样）")
    loaded_mass_kg: float | None = Field(
        description="满装质量（kg，官方口径：仅固体；GCAT 文档明载为 loaded）"
    )
    total_impulse_ns: float | None = Field(description="总冲（N·s，多为固体）")
    typical_thrust_n: float | None = Field(
        description="典型推力（N）——⚠ 环境未声明，不参与配对（OI-35）；与推力/总冲禁止互称"
    )
    isp_vacuum_s: float | None = Field(
        description="真空比冲（s，官方定义 'vacuum Isp where available'）"
    )
    burn_duration_s: float | None = Field(description="典型工作时间（s）")
    first_flight: str | None = Field(
        description="首次飞行（GCAT Date 列 TEXT 原样，含 ? 尾标等可信度痕迹）"
    )
    usage_notes: str | None = Field(description="用途 / 状态备注（GCAT Usage 列 TEXT 原样）")
    quality: str = Field(description="§7.6 质量标签（GCAT 全库为 literature）")


class SnapshotOut(BaseModel):
    """本目录库的快照来源（§7.8：数值随 provenance 下发的锚点）。"""

    id: str = Field(description="快照 id（如 gcat-2026Q3）")
    release: str = Field(description="GCAT release 号")
    total_records: int = Field(description="快照登记的总记录数（六表合计）")


class EnginesCatalogResponse(BaseModel):
    """``GET /api/catalog/engines`` 的响应体（两种形态按 ``family`` 参数分岔）。

    - 缺省：``families`` 非空（族清单 + 计数），``engines`` 为空；
    - 给 ``family``：回显 ``family``，``missing_fields`` 恒在，``engines`` 为该族记录。
    """

    snapshot: SnapshotOut = Field(description="目录库的快照来源（§7.8 溯源锚点）")
    family: str | None = Field(
        default=None, description="查询的族名（仅在按族查询时回显；缺省查询为 null）"
    )
    families: tuple[EngineFamilySummary, ...] = Field(
        default=(),
        description="族清单 + 每族计数（缺省查询时返回；仅列确有发动机记录的族）",
    )
    missing_fields: tuple[str, ...] = Field(
        default=(),
        description=("GCAT 结构性缺失、须 §7.9 手动补录的字段（按族查询时恒在；禁止编造数值）"),
    )
    engines: tuple[EngineRecordOut, ...] = Field(
        default=(), description="族内发动机记录（按族查询时返回；无此族为空表）"
    )


@router.get("/api/catalog/materials", response_model=MaterialsResponse)
def list_materials() -> MaterialsResponse:
    """材料库全量查询（含派生比强度 / 比刚度计算值）。"""
    return MaterialsResponse(
        materials=tuple(
            MaterialOut(
                id=entry.id,
                name=entry.name,
                category=entry.category,
                density_kg_m3=entry.density_kg_m3,
                elastic_modulus_pa=entry.elastic_modulus_pa,
                yield_strength_pa=entry.yield_strength_pa,
                service_temp_min_c=entry.service_temp_min_c,
                service_temp_max_c=entry.service_temp_max_c,
                typical_min_wall_thickness_m=entry.typical_min_wall_thickness_m,
                heat_treatment=entry.heat_treatment,
                source=entry.source,
                quality=entry.quality,
                specific_strength_m2_s2=specific_strength(
                    entry.yield_strength_pa, entry.density_kg_m3
                ),
                specific_stiffness_m2_s2=specific_stiffness(
                    entry.elastic_modulus_pa, entry.density_kg_m3
                ),
            )
            for entry in MATERIALS
        )
    )


@router.get("/api/catalog/engines", response_model=EnginesCatalogResponse)
def engine_catalog(
    catalog: Annotated[CatalogRepository, Depends(get_catalog)],
    family: str = "",
) -> EnginesCatalogResponse:
    """发动机族谱（§7.4）：缺省返回族清单 + 计数；给 ``family`` 返回该族记录。

    无此族返回空记录表（不报错——族清单就在缺省形态里，客户端可先行校验）。
    """
    snapshot = _snapshot_out(catalog.snapshot())
    if not family:
        return EnginesCatalogResponse(
            snapshot=snapshot,
            families=tuple(
                EngineFamilySummary(family=item.family, count=item.count)
                for item in catalog.engine_families()
            ),
        )
    return EnginesCatalogResponse(
        snapshot=snapshot,
        family=family,
        missing_fields=ENGINE_MISSING_FIELDS,
        engines=tuple(_engine_out(record) for record in catalog.engines_in_family(family)),
    )


def _snapshot_out(meta: SnapshotMeta) -> SnapshotOut:
    return SnapshotOut(id=meta.id, release=meta.release, total_records=meta.total_records)


def _engine_out(record: EngineRecord) -> EngineRecordOut:
    return EngineRecordOut(
        record_id=record.record_id,
        name=record.name,
        manufacturer=record.manufacturer,
        family=record.family,
        oxidizer=record.oxidizer,
        fuel=record.fuel,
        loaded_mass_kg=record.loaded_mass_kg,
        total_impulse_ns=record.total_impulse_ns,
        typical_thrust_n=record.typical_thrust_n,
        isp_vacuum_s=record.isp_vacuum_s,
        burn_duration_s=record.burn_duration_s,
        first_flight=record.first_flight,
        usage_notes=record.usage_notes,
        quality=record.quality,
    )


# ---------------------------------------------------------------------------
# OI-39：型号检索 + 型号已知参数集（GCAT lv 全库）
# ---------------------------------------------------------------------------

#: 发动机比冲的口径注记（OI-35）：GCAT Isp 列官方定义为 "vacuum Isp where available"。
_NOTE_ISP_VACUUM = "真空口径（OI-35）"
#: 发动机推力的口径注记（OI-35）：GCAT Thrust 列环境未声明，禁止参与配对计算。
_NOTE_THRUST_UNDECLARED = "环境未声明（OI-35）"

#: 检索可用性摘要 ↔ vehicles 列名（glow = launch_mass_kg，§6.1 起飞质量口径）。
_AVAILABILITY_BY_COLUMN: dict[str, str] = {
    "launch_mass_kg": "glow",
    "length_m": "length_m",
    "diameter_m": "diameter_m",
    "payload_leo_kg": "payload_leo_kg",
}


class SourcedField(BaseModel):
    """单个已知参数字段：值 + 出处锚点 + 单位存疑标记（§7.8 溯源红线）。

    ``value`` 为 ``null`` 表示 GCAT 源缺失——**缺失是状态不是 0**（§7.5 规则 2），
    一律入响应顶层的 ``missing`` 清单，禁止编造替代值。
    """

    value: float | str | None = Field(
        description="字段值（ETL 已换算 SI；GCAT 缺失为 null，禁止编造）"
    )
    source: str = Field(
        description='出处锚点，如 "GCAT gcat-2026Q3 lv#175"（表前缀 lv/st/en + 源行号）'
    )
    unit_uncertain: bool = Field(
        description="该字段是否被 ETL 标记单位/量值存疑（解析失败或出合理区间，值仍保留原样）"
    )


class AvailabilityOut(BaseModel):
    """检索候选的可用性摘要：四个概览字段各自是否可得（缺列不入摘要，§7.5）。"""

    glow: bool = Field(description="起飞质量（GCAT launch_mass_kg，§6.1 GLOW 口径）是否可得")
    length_m: bool = Field(description="全长是否可得")
    diameter_m: bool = Field(description="直径是否可得")
    payload_leo_kg: bool = Field(description="LEO 运力是否可得")


class VehicleSearchHitOut(BaseModel):
    """一条检索候选（GCAT lv 记录的检索投影）。

    内置精校模板的置顶与「精校」标注由**前端**完成（复用 OI-34 匹配通路），
    后端不掺合模板逻辑。
    """

    record_id: int = Field(description="记录主键（溯源锚点）")
    name: str = Field(description="型号名称（GCAT 原样，检索点选后以此名取参数集）")
    variant: str | None = Field(description="变体（GCAT 原样；无变体为 null）")
    family: str | None = Field(description="型号族（GCAT 原样）")
    country: str | None = Field(description="国家/地区（第三层标签解析后的标签值；无标签为 null）")
    stage_count: int | None = Field(
        description="核心级数（max_stage_no 取整；助推器记 0/−1，GCAT 未给为 null）"
    )
    availability: AvailabilityOut = Field(description="概览字段可用性摘要")


class VehicleSearchResponse(BaseModel):
    """``GET /api/catalog/vehicles/search`` 的响应体。"""

    query: str = Field(description="回显的检索词（原样，未规范化）")
    hits: tuple[VehicleSearchHitOut, ...] = Field(
        description=(
            "候选清单（规范化子串匹配，按「完全匹配 > 前缀 > 子串 > 族/变体」排序，"
            "上限 12 条）；检索词为空时为空表"
        )
    )


class VehicleFieldsOut(BaseModel):
    """完整火箭（GCAT lv 行）的逐字段投影——字段名与仓储 ``VehicleRecord`` 一致。"""

    name: SourcedField = Field(description="型号名称")
    family: SourcedField = Field(description="型号族")
    variant: SourcedField = Field(description="变体")
    manufacturer: SourcedField = Field(description="制造商（GCAT 机构代码原样）")
    min_stage_no: SourcedField = Field(description="级号范围下界（助推器记 0/−1，§7.3）")
    max_stage_no: SourcedField = Field(description="级号范围上界（核心级数 ≈ 此值，§7.3）")
    length_m: SourcedField = Field(description="全长（m）")
    diameter_m: SourcedField = Field(description="最大直径（m）")
    launch_mass_kg: SourcedField = Field(description="起飞质量（kg，§6.1 GLOW 口径）")
    payload_leo_kg: SourcedField = Field(description="LEO 运力（kg）")
    payload_gto_kg: SourcedField = Field(description="GTO 运力（kg）")
    liftoff_thrust_n: SourcedField = Field(description="起飞推力（N）")
    vehicle_class: SourcedField = Field(description="GCAT 类别码（O/B/S/R 原样）")


class StageFieldsOut(BaseModel):
    """级（GCAT stages 行）的逐字段投影——字段名与仓储 ``StageRecord`` 一致。

    推力真空/海平面分列（§7.5 规则 3 / OI-35）；比冲 GCAT 不提供（结构性缺失）。
    """

    name: SourcedField = Field(description="级名称")
    family: SourcedField = Field(description="级族")
    manufacturer: SourcedField = Field(description="制造商（GCAT 机构代码原样）")
    length_m: SourcedField = Field(description="级长（m）")
    diameter_m: SourcedField = Field(description="级直径（m）")
    full_mass_kg: SourcedField = Field(description="级满装质量（kg）")
    dry_mass_kg: SourcedField = Field(description="级干重（kg）")
    thrust_vacuum_n: SourcedField = Field(description="级真空推力（N，§7.5 规则 3）")
    thrust_sea_level_n: SourcedField = Field(description="级海平面推力（N，§7.5 规则 3）")
    burn_duration_s: SourcedField = Field(description="工作时间（s）")
    engine_name: SourcedField = Field(description="级引用的发动机名（GCAT 原样）")
    engine_count: SourcedField = Field(description="发动机台数")


class EngineFieldsOut(BaseModel):
    """发动机（GCAT engines 行）的逐字段投影——字段名与仓储 ``EngineRecord`` 一致。

    ⚠ 两个口径注记（OI-35）：``isp_vacuum_s`` 为真空口径；``typical_thrust_n``
    环境未声明——二者只作参照展示，禁止据此派生推重比或海平面/真空换算。
    """

    name: SourcedField = Field(description="发动机型号")
    manufacturer: SourcedField = Field(description="制造商（GCAT 机构代码原样）")
    family: SourcedField = Field(description="族名")
    oxidizer: SourcedField = Field(description="氧化剂（源值原样）")
    fuel: SourcedField = Field(description="燃料（源值原样）")
    loaded_mass_kg: SourcedField = Field(description="满装质量（kg，官方口径：仅固体）")
    total_impulse_ns: SourcedField = Field(description="总冲（N·s，多为固体）")
    typical_thrust_n: SourcedField = Field(
        description="典型推力（N）——⚠ source 注明「环境未声明（OI-35）」，不参与配对"
    )
    isp_vacuum_s: SourcedField = Field(
        description="真空比冲（s）——⚠ source 注明「真空口径（OI-35）」"
    )
    burn_duration_s: SourcedField = Field(description="典型工作时间（s）")
    first_flight: SourcedField = Field(description="首次飞行（GCAT Date 列 TEXT 原样）")
    usage_notes: SourcedField = Field(description="用途/状态备注（GCAT Usage 列 TEXT 原样）")


class StageAssemblyOut(BaseModel):
    """一条装配行：stage_links 关系 + 该级的逐字段投影（缺失引用时 ``record`` 为 null）。"""

    stage_no: str | None = Field(description="级号（stage_links 的 Stage_No 原样，含空格）")
    qualifier: str | None = Field(description="限定符（stage_links 原样）")
    record: StageFieldsOut | None = Field(
        description="级记录投影；null = 缺失引用（stage_links 指向的级不在 stages 表，不编造）"
    )
    missing_reference: bool = Field(description="是否缺失引用（与 record 为 null 同步）")


class VehicleRecordResponse(BaseModel):
    """``GET /api/catalog/vehicles/record`` 的响应体：型号已知参数集（OI-39 ③）。

    - ``missing``：显式缺失清单——每个值为 null 的字段一条（路径与控件 ``field_path``
      同口径）+ 缺失引用条目；GCAT 没有的一律 null + missing，**禁止编造**；
    - ``warnings``：装配过程的非致命问题（缺失引用、同名发动机多行取首见），原样透传；
    - ``reference_only``：可用字段不足一半（GCAT 以 0 填未知，0 值按 §7.5「缺失是
      状态不是 0」计为不可用）→ true，前端应提示「仅可作参照」。
    """

    name: str = Field(description="型号名称（回显）")
    variant: str | None = Field(description="变体（回显；无变体为 null）")
    record_id: int = Field(description="lv 记录主键（溯源锚点）")
    quality: str = Field(description="§7.6 质量标签（GCAT 全库为 literature）")
    snapshot: SnapshotOut = Field(description="目录库的快照来源（§7.8 溯源锚点）")
    vehicle: VehicleFieldsOut = Field(description="lv 行逐字段投影（出处逐字段携带）")
    stages: tuple[StageAssemblyOut, ...] = Field(
        description="装配行（按 Stage_No 数值序；含缺失引用的 null 槽位）"
    )
    engines: tuple[EngineFieldsOut, ...] = Field(
        description="本型号各级引用的发动机（按装配序去重；口径注记见字段说明）"
    )
    missing: tuple[str, ...] = Field(
        description="显式缺失清单（null 字段路径 + 缺失引用说明；禁止编造的缺口都在这里）"
    )
    warnings: tuple[str, ...] = Field(description="装配过程的非致命问题（原样透传，不吞不掉）")
    reference_only: bool = Field(
        description="可用字段不足一半时为 true——数据缺口较多，仅可作参照，建模需手动补参"
    )


def _sourced(
    record: VehicleRecord | StageRecord | EngineRecord,
    field_name: str,
    table_prefix: str,
    *,
    note: str | None = None,
) -> SourcedField:
    """记录字段 → 带出处的投影：锚点 = ``GCAT <快照> <表>#<源行号>``。

    ``unit_uncertain`` 逐字段判定：ETL 把解析失败/出合理区间的**字段名**记在记录的
    ``unit_uncertain`` 逗号清单里（§7.5），与字段一一对应。
    """
    value: float | str | None = getattr(record, field_name)
    anchor = f"GCAT {record.snapshot} {table_prefix}#{record.source_row}"
    return SourcedField(
        value=value,
        source=f"{anchor}；{note}" if note is not None else anchor,
        unit_uncertain=field_name in record.unit_uncertain.split(","),
    )


def _vehicle_fields(record: VehicleRecord) -> VehicleFieldsOut:
    return VehicleFieldsOut(
        name=_sourced(record, "name", "lv"),
        family=_sourced(record, "family", "lv"),
        variant=_sourced(record, "variant", "lv"),
        manufacturer=_sourced(record, "manufacturer", "lv"),
        min_stage_no=_sourced(record, "min_stage_no", "lv"),
        max_stage_no=_sourced(record, "max_stage_no", "lv"),
        length_m=_sourced(record, "length_m", "lv"),
        diameter_m=_sourced(record, "diameter_m", "lv"),
        launch_mass_kg=_sourced(record, "launch_mass_kg", "lv"),
        payload_leo_kg=_sourced(record, "payload_leo_kg", "lv"),
        payload_gto_kg=_sourced(record, "payload_gto_kg", "lv"),
        liftoff_thrust_n=_sourced(record, "liftoff_thrust_n", "lv"),
        vehicle_class=_sourced(record, "vehicle_class", "lv"),
    )


def _stage_fields(record: StageRecord) -> StageFieldsOut:
    return StageFieldsOut(
        name=_sourced(record, "name", "st"),
        family=_sourced(record, "family", "st"),
        manufacturer=_sourced(record, "manufacturer", "st"),
        length_m=_sourced(record, "length_m", "st"),
        diameter_m=_sourced(record, "diameter_m", "st"),
        full_mass_kg=_sourced(record, "full_mass_kg", "st"),
        dry_mass_kg=_sourced(record, "dry_mass_kg", "st"),
        thrust_vacuum_n=_sourced(record, "thrust_vacuum_n", "st"),
        thrust_sea_level_n=_sourced(record, "thrust_sea_level_n", "st"),
        burn_duration_s=_sourced(record, "burn_duration_s", "st"),
        engine_name=_sourced(record, "engine_name", "st"),
        engine_count=_sourced(record, "engine_count", "st"),
    )


def _engine_fields(record: EngineRecord) -> EngineFieldsOut:
    return EngineFieldsOut(
        name=_sourced(record, "name", "en"),
        manufacturer=_sourced(record, "manufacturer", "en"),
        family=_sourced(record, "family", "en"),
        oxidizer=_sourced(record, "oxidizer", "en"),
        fuel=_sourced(record, "fuel", "en"),
        loaded_mass_kg=_sourced(record, "loaded_mass_kg", "en"),
        total_impulse_ns=_sourced(record, "total_impulse_ns", "en"),
        typical_thrust_n=_sourced(record, "typical_thrust_n", "en", note=_NOTE_THRUST_UNDECLARED),
        isp_vacuum_s=_sourced(record, "isp_vacuum_s", "en", note=_NOTE_ISP_VACUUM),
        burn_duration_s=_sourced(record, "burn_duration_s", "en"),
        first_flight=_sourced(record, "first_flight", "en"),
        usage_notes=_sourced(record, "usage_notes", "en"),
    )


def _iter_sourced(model: BaseModel) -> list[tuple[str, SourcedField]]:
    """按声明序取出模型里的 ``SourcedField`` 字段（路径登记与可用性统计共用）。"""
    return [
        (name, value)
        for name in type(model).model_fields
        for value in [getattr(model, name)]
        if isinstance(value, SourcedField)
    ]


def _register_missing(model: BaseModel, prefix: str, missing: list[str]) -> None:
    """值为 null 的字段登记进 ``missing``——缺失与响应体机械一致，不漏不多算。"""
    missing.extend(f"{prefix}{name}" for name, entry in _iter_sourced(model) if entry.value is None)


def _count_usable(model: BaseModel) -> tuple[int, int]:
    """（可用, 总数）统计：值非 null 且非 0 计可用（GCAT 以 0 填未知，§7.5 缺失口径）。"""
    entries = _iter_sourced(model)
    usable = sum(
        1
        for _, entry in entries
        if entry.value is not None
        and not (isinstance(entry.value, (int, float)) and entry.value == 0)
    )
    return usable, len(entries)


def _vehicle_record_response(
    catalog: CatalogRepository, bundle: VehicleRecordBundle
) -> VehicleRecordResponse:
    """仓储装配 → API 投影：逐字段出处、缺失清单、警告透传、参照判定。"""
    missing: list[str] = []
    vehicle_fields = _vehicle_fields(bundle.vehicle)
    _register_missing(vehicle_fields, "vehicle.", missing)

    stages_out: list[StageAssemblyOut] = []
    engines_out: list[EngineFieldsOut] = []
    seen_engines: set[int] = set()
    usable, total = _count_usable(vehicle_fields)
    for index, assembly in enumerate(bundle.assemblies):
        link = assembly.link
        if assembly.stage is None:
            stage_name = link.stage_name if link.stage_name is not None else "（空）"
            missing.append(f"stages[{index}]（stage_links 指向的级 {stage_name!r} 不存在）")
            stages_out.append(
                StageAssemblyOut(
                    stage_no=link.stage_no,
                    qualifier=link.qualifier,
                    record=None,
                    missing_reference=True,
                )
            )
            continue
        stage_fields = _stage_fields(assembly.stage)
        _register_missing(stage_fields, f"stages[{index}].", missing)
        stage_usable, stage_total = _count_usable(stage_fields)
        usable += stage_usable
        total += stage_total
        if assembly.engine is not None:
            engine_fields = _engine_fields(assembly.engine)
            _register_missing(engine_fields, f"stages[{index}].engine.", missing)
            engine_usable, engine_total = _count_usable(engine_fields)
            usable += engine_usable
            total += engine_total
            if assembly.engine.record_id not in seen_engines:
                seen_engines.add(assembly.engine.record_id)
                engines_out.append(engine_fields)
        elif assembly.stage.engine_name is not None:
            missing.append(
                f"stages[{index}].engine（级引用的发动机 {assembly.stage.engine_name!r} 不存在）"
            )
        stages_out.append(
            StageAssemblyOut(
                stage_no=link.stage_no,
                qualifier=link.qualifier,
                record=stage_fields,
                missing_reference=False,
            )
        )

    return VehicleRecordResponse(
        name=bundle.vehicle.name,
        variant=bundle.vehicle.variant,
        record_id=bundle.vehicle.record_id,
        quality=bundle.vehicle.quality,
        snapshot=_snapshot_out(catalog.snapshot()),
        vehicle=vehicle_fields,
        stages=tuple(stages_out),
        engines=tuple(engines_out),
        missing=tuple(missing),
        warnings=bundle.warnings,
        reference_only=total == 0 or 2 * usable < total,
    )


@router.get("/api/catalog/vehicles/search", response_model=VehicleSearchResponse)
def search_vehicle_records(
    catalog: Annotated[CatalogRepository, Depends(get_catalog)],
    name: str = "",
) -> VehicleSearchResponse:
    """型号检索（OI-39 ②）：GCAT lv 全库规范化子串匹配，排序透明化。

    检索词为空时返回空表（``hits=[]``，不是错误）；内置精校模板的置顶由前端完成。
    """
    hits = catalog.search_vehicles(name) if name.strip() != "" else []
    mapped: list[VehicleSearchHitOut] = []
    for hit in hits:
        available = set(hit.available_fields)
        mapped.append(
            VehicleSearchHitOut(
                record_id=hit.record_id,
                name=hit.name,
                variant=hit.variant,
                family=hit.family,
                country=hit.country_tag,
                stage_count=hit.stage_count,
                availability=AvailabilityOut(
                    **{
                        param: column in available
                        for column, param in _AVAILABILITY_BY_COLUMN.items()
                    }
                ),
            )
        )
    return VehicleSearchResponse(query=name, hits=tuple(mapped))


@router.get("/api/catalog/vehicles/record", response_model=VehicleRecordResponse)
def vehicle_record(
    catalog: Annotated[CatalogRepository, Depends(get_catalog)],
    name: str,
    variant: str = "",
) -> VehicleRecordResponse:
    """型号已知参数集（OI-39 ③）：lv + stage_links + stages + engines 逐字段带出处。

    按名称 + 变体**精确**定位（检索候选点选后的下一跳，不做模糊）；查无此型号 →
    404（``CATALOG_NOT_FOUND``）。缺失显式列出、禁止编造；``reference_only`` 提示
    数据缺口较多、仅可作参照。
    """
    bundle = catalog.get_vehicle_record(name, variant if variant != "" else None)
    if bundle is None:
        raise CatalogNotFoundError(
            f"GCAT 目录中没有 {name!r}（variant={variant or '（无）'}）的记录",
            suggestion=(
                "回到检索下拉从候选中点选（名称与变体须与候选完全一致）；"
                "该型号也可能不在 GCAT gcat-2026Q3 的覆盖范围内"
            ),
        )
    return _vehicle_record_response(catalog, bundle)
