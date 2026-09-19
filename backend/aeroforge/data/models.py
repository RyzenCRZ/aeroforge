"""数据目录的读取侧模型（§7.7 仓储层返回值）。

字段名与 ETL 的内部字段（``aeroforge.data.etl`` 的 ``FieldMap.internal``）
**逐一同名**——``test_gcat_db.py`` 有同步门禁：dataclass 字段集合 − 元数据列
== FieldMap 声明集，防止两处名单漂移。

数值字段一律 SI（ETL 已换算）；``None`` = 源缺失（§7.5 规则 2：缺失是状态，
不是 0）。``tag_incomplete`` 见 §7.2：**源字段存在却缺失**的标签维度清单。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SnapshotMeta:
    """``snapshots`` 表单行：本库由哪份快照整体重建而来（§7.8 溯源锚点）。"""

    id: str
    release: str
    total_records: int


@dataclass(frozen=True)
class TagInfo:
    """一枚规范化标签枚举（§7.2：不收自由文本）。"""

    tag_id: int
    level: int
    kind: str
    value: str


@dataclass(frozen=True)
class StageLink:
    """``stage_links``（lvs.tsv）：完整火箭 ↔ 级的装配关系。"""

    lv_name: str | None
    lv_variant: str | None
    stage_no: str | None
    stage_name: str | None
    qualifier: str | None


@dataclass(frozen=True)
class VehicleRecord:
    """完整火箭（GCAT ``lv`` 表）。"""

    record_id: int
    snapshot: str
    source_row: int
    quality: str
    unit_uncertain: str
    source_flags: str
    tag_incomplete: str
    name: str
    family: str | None
    variant: str | None
    manufacturer: str | None
    #: 级号范围（非级数）：助推器类别记 0/−1；核心级数 ≈ max_stage_no（§7.3 更正）
    min_stage_no: float | None
    max_stage_no: float | None
    length_m: float | None
    diameter_m: float | None
    launch_mass_kg: float | None
    payload_leo_kg: float | None
    payload_gto_kg: float | None
    liftoff_thrust_n: float | None
    vehicle_class: str | None


@dataclass(frozen=True)
class StageRecord:
    """级（GCAT ``stages`` 表）。推力真空/海平面分列（§7.5 规则 3 / OI-35）。"""

    record_id: int
    snapshot: str
    source_row: int
    quality: str
    unit_uncertain: str
    source_flags: str
    tag_incomplete: str
    name: str
    family: str | None
    manufacturer: str | None
    length_m: float | None
    diameter_m: float | None
    full_mass_kg: float | None
    dry_mass_kg: float | None
    thrust_vacuum_n: float | None
    thrust_sea_level_n: float | None
    burn_duration_s: float | None
    engine_name: str | None
    engine_count: float | None


@dataclass(frozen=True)
class EngineRecord:
    """发动机（GCAT ``engines`` 表）。``typical_thrust_n`` 环境未声明，不参与配对（OI-35）。"""

    record_id: int
    snapshot: str
    source_row: int
    quality: str
    unit_uncertain: str
    source_flags: str
    tag_incomplete: str
    name: str
    manufacturer: str | None
    family: str | None
    oxidizer: str | None
    fuel: str | None
    loaded_mass_kg: float | None
    total_impulse_ns: float | None
    typical_thrust_n: float | None
    isp_vacuum_s: float | None
    burn_duration_s: float | None
    #: 首次飞行年份（§7.4 族谱口径）：GCAT ``Date`` 列 TEXT 原样（含 ``?`` 尾标）。
    first_flight: str | None
    #: 当前状态 / 用途备注（§7.4 族谱口径）：GCAT ``Usage`` 列 TEXT 原样。
    usage_notes: str | None


@dataclass(frozen=True)
class EngineFamilyCount:
    """族谱聚合行（§7.4）：族名 + 该族在 engines 表中的记录数。"""

    family: str
    count: int
