"""目录域端点（规格 §7.4 / §10.1）。

- ``GET /api/catalog/materials`` —— 自建材料库全量查询（§7.4「材料库数据源裁决」：
  SPACEMATDB 无明示开放许可，不抓取；本库 Python 内嵌，逐条带来源与质量标签）。
- ``GET /api/catalog/engines`` —— 发动机族谱（按族聚合；缺省返回族清单 + 计数，
  给 ``family`` 返回该族记录）。

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
from aeroforge.data.models import EngineRecord, SnapshotMeta
from aeroforge.data.repository import CatalogRepository
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
