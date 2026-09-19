"""GCAT ETL（规格 §7.5）：列映射 → 单位规范化(SI) → 缺失策略 → 异常标记 → 质量标签 → Parquet。

两条铁律在这里落地：

1. **源单位逐字段显式声明，不允许从数据反推**——下方 FIELD 声明表里每个数值列的
   单位都抄自 GCAT 官方字段文档（URL 见 ``UNIT_SOURCE_URLS``），stages 表
   "Full mass 吨 / Dry mass kg" 的同表混用即官方明载事实。
2. **无法确定或量纲存疑 → 标记 ``unit_uncertain`` 并保留原值**，由统计拟合环节
   排除（§7.5 规则 2），**绝不静默删除**——删了就等于把"未验证"伪装成"没有"。

比冲口径（§7.5 规则 3）：GCAT engines 的 ``Isp`` 官方定义即"vacuum Isp where
available"，无海平面对应列 ⇒ 无混用可拆；stages 的推力本就真空/海平面分列。
 ``isp_vacuum_s`` 的合理区间门禁（100–500 s）专防 m/s 与 s 混读。

Parquet 只物化带数值口径的三张表（lv / stages / engines）；``lvs`` / ``orgs`` /
``family`` 是纯文本关联表，无单位风险，由标签层（§7.2）直接消费 TSV。
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pandas as pd

from aeroforge.data.snapshot import MISSING_TOKENS, SNAPSHOT_TABLES, load_manifest, read_table

#: Parquet 产物名（§7.1 快照规范的第四件套）
RECORDS_PARQUET = "records.parquet"

#: 源单位的权威出处（§7.5 规则 1：声明要能回指原文）
UNIT_SOURCE_URLS = {
    "lv": "https://planet4589.org/space/gcat/web/lvs/lv/index.html",
    "stages": "https://planet4589.org/space/gcat/web/lvs/stages/index.html",
    "engines": "https://planet4589.org/space/gcat/web/lvs/engines/index.html",
}

#: 源单位 → SI 换算因子。目标一律 SI（§1.4-3）。``"1"`` = 无量纲计数。
UNIT_FACTORS = {"m": 1.0, "kg": 1.0, "s": 1.0, "t": 1000.0, "kN": 1000.0, "kNs": 1000.0, "1": 1.0}


class FieldMap(NamedTuple):
    """单字段映射：GCAT 原始列 → 内部字段 + 源单位（``None`` = 非数值列）。"""

    raw: str
    internal: str
    source_unit: str | None = None
    #: 原样保留的溯源列（GCAT 的 *Flag 列，语义官方未文档化——不发明，只留证）
    keep_raw: bool = False


LV_FIELDS: tuple[FieldMap, ...] = (
    FieldMap("LV_Name", "name"),
    FieldMap("LV_Family", "family"),
    FieldMap("LV_Variant", "variant"),
    FieldMap("LV_Manufacturer", "manufacturer"),
    FieldMap("LV_Min_Stage", "min_stages", "1"),
    FieldMap("Length", "length_m", "m"),
    FieldMap("Diameter", "diameter_m", "m"),
    # 官方文档：Launch mass (tonnes)——而 Dry_Mass（stages 表）是 kg，勿"顺手统一"
    FieldMap("Launch_Mass", "launch_mass_kg", "t"),
    # 官方文档：LEO/GTO payload capacity in kg, "especially tentative"
    FieldMap("LEO_Capacity", "payload_leo_kg", "kg"),
    FieldMap("GTO_Capacity", "payload_gto_kg", "kg"),
    FieldMap("TO_Thrust", "liftoff_thrust_n", "kN"),
    FieldMap("Class", "vehicle_class"),
    FieldMap("LFlag", "source_flags", keep_raw=True),
    FieldMap("MFlag", "source_flags", keep_raw=True),
    FieldMap("DFlag", "source_flags", keep_raw=True),
)

STAGE_FIELDS: tuple[FieldMap, ...] = (
    FieldMap("Stage_Name", "name"),
    FieldMap("Stage_Family", "family"),
    FieldMap("Stage_Manufacturer", "manufacturer"),
    FieldMap("Length", "length_m", "m"),
    FieldMap("Diameter", "diameter_m", "m"),
    # 官方文档：Full mass (tonne) / Dry mass (kg)——同一张表两种质量单位
    FieldMap("Launch_Mass", "full_mass_kg", "t"),
    FieldMap("Dry_Mass", "dry_mass_kg", "kg"),
    FieldMap("Thrust", "thrust_vacuum_n", "kN"),
    FieldMap("ThrustSL", "thrust_sea_level_n", "kN"),
    FieldMap("Duration", "burn_duration_s", "s"),
    FieldMap("Engine", "engine_name"),
    FieldMap("NEng", "engine_count", "1"),
    FieldMap("Length_Flag", "source_flags", keep_raw=True),
    FieldMap("Diameter_Flag", "source_flags", keep_raw=True),
    FieldMap("Launch_Mass_Flag", "source_flags", keep_raw=True),
    FieldMap("Dry_Mass_Flag", "source_flags", keep_raw=True),
    FieldMap("Thrust_Flag", "source_flags", keep_raw=True),
    FieldMap("ThrustSL_Flag", "source_flags", keep_raw=True),
)

ENGINE_FIELDS: tuple[FieldMap, ...] = (
    FieldMap("Name", "name"),
    FieldMap("Manufacturer", "manufacturer"),
    FieldMap("Family", "family"),
    FieldMap("Oxidizer", "oxidizer"),
    FieldMap("Fuel", "fuel"),
    # 官方文档：Mass, kg, loaded (solids only)
    FieldMap("Mass", "loaded_mass_kg", "kg"),
    # 官方文档：Total impulse, kNs (mostly for solids)——SI 目标 N·s
    FieldMap("Impulse", "total_impulse_ns", "kNs"),
    FieldMap("Thrust", "typical_thrust_n", "kN"),
    # 官方文档：Typical Isp (s), (vacuum Isp where available)——§7.5 规则 3 的真空口径
    FieldMap("Isp", "isp_vacuum_s", "s"),
    FieldMap("Duration", "burn_duration_s", "s"),
    FieldMap("MFlag", "source_flags", keep_raw=True),
    FieldMap("ImpFlag", "source_flags", keep_raw=True),
    FieldMap("TFlag", "source_flags", keep_raw=True),
    FieldMap("IspFlag", "source_flags", keep_raw=True),
    FieldMap("DurFlag", "source_flags", keep_raw=True),
)

TABLE_FIELDS: dict[str, tuple[FieldMap, ...]] = {
    "lv": LV_FIELDS,
    "stages": STAGE_FIELDS,
    "engines": ENGINE_FIELDS,
}

#: 合理区间（SI 值域，§7.5 "异常值标记"）。超出 ⇒ 值保留 + 标 ``unit_uncertain``。
#: 区间含义：不是"物理上限"，而是"若落到区间外，几乎必然是量纲/单位读错"。
#: ``isp_vacuum_s`` 的 100–500 专防把 s 读成 m/s（×10 错值会被立刻揪出）。
PLAUSIBLE_BOUNDS: dict[str, tuple[float, float]] = {
    "length_m": (0.1, 150.0),
    "diameter_m": (0.05, 20.0),
    # 1e7 kg（万吨级）：gcat-2026Q3 实测 Starship V1.0 起飞质量 5,020 t 越过旧上界
    # 3,500 t——上界按现役最大构型放宽，占位 0 值仍由下界拦住
    "launch_mass_kg": (10.0, 10_000_000.0),
    "payload_leo_kg": (0.0, 200_000.0),
    "payload_gto_kg": (0.0, 100_000.0),
    "liftoff_thrust_n": (0.0, 100_000_000.0),
    "full_mass_kg": (10.0, 10_000_000.0),
    "dry_mass_kg": (10.0, 10_000_000.0),
    "thrust_vacuum_n": (0.0, 100_000_000.0),
    "thrust_sea_level_n": (0.0, 100_000_000.0),
    "typical_thrust_n": (0.0, 100_000_000.0),
    "total_impulse_ns": (0.0, 100_000_000_000.0),
    "isp_vacuum_s": (100.0, 500.0),
    "burn_duration_s": (0.0, 5_000.0),
    # 官方文档：minimum stage number 可为 0（strapon）甚至 -1（Atlas IIAS 式两类助推器）
    "min_stages": (-1.0, 50.0),
    "engine_count": (0.0, 50.0),
}

#: §7.6 质量标签：GCAT 是文献汇编型目录（官方自述"approximate nature of the
#: numerical values"），全部记录按 ``literature``（权重 0.7）入账；
#: ``official`` 保留给 §13.2 有公开出处核对过的基准数据。
GCAT_QUALITY = "literature"


def _clean_number(value: str) -> str:
    """GCAT 数值列常见干扰：千分位逗号、尾部 ``?``（未经证实标记）。"""
    return value.replace(",", "").rstrip("?").strip()


def _convert(raw_value: str, field: FieldMap, uncertain: set[str]) -> float | str | None:
    """单值转换：缺失 → None；数值列换算 SI；解析失败或出合理区间 → 标记后返回。"""
    cleaned = raw_value.strip()
    if cleaned in MISSING_TOKENS:
        return None
    if field.source_unit is None:
        return cleaned
    # 因子表查不到 ⇒ FIELD 声明表配置错误：当场炸（KeyError），绝不吞成数据标记——
    # 否则配置错会伪装成"全表 unit_uncertain"（gcat-2026Q3 实测踩过："1" 忘登记）。
    factor = UNIT_FACTORS[field.source_unit]
    try:
        si = float(_clean_number(cleaned)) * factor
    except ValueError:
        uncertain.add(field.internal)
        return None
    bounds = PLAUSIBLE_BOUNDS.get(field.internal)
    if bounds is not None and not (bounds[0] <= si <= bounds[1]):
        uncertain.add(field.internal)
    return si


def table_frame(snapshot_dir: Path, table: str) -> pd.DataFrame:
    """单表 → 规范化 DataFrame（含 ``table`` / ``quality`` / ``unit_uncertain`` / 溯源列）。"""
    fields = TABLE_FIELDS[table]
    text = (snapshot_dir / SNAPSHOT_TABLES[table]).read_text(encoding="utf-8", errors="replace")
    _, rows = read_table(text)

    records: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        uncertain: set[str] = set()
        record: dict[str, object] = {
            "table": table,
            "source_row": index,
            "snapshot": snapshot_dir.name,
            "quality": GCAT_QUALITY,
        }
        flags: list[str] = []
        for field in fields:
            raw = row.get(field.raw, "")
            if field.keep_raw:
                if raw.strip() not in MISSING_TOKENS:
                    flags.append(f"{field.raw}={raw.strip()}")
                continue
            record[field.internal] = _convert(raw, field, uncertain)
        record["unit_uncertain"] = ",".join(sorted(uncertain))
        record["source_flags"] = ";".join(flags)
        records.append(record)

    columns = ["table", "source_row", "snapshot", "quality", "unit_uncertain", "source_flags"]
    columns += [field.internal for field in fields if not field.keep_raw]
    return pd.DataFrame(records, columns=columns)


class EtlStats(NamedTuple):
    """ETL 结果摘要（CLI 打印与门禁断言用）。"""

    rows: dict[str, int]
    uncertain: dict[str, int]
    total_records: int


def run_etl(snapshot_dir: Path) -> EtlStats:
    """三表规范化 → 合并写 ``records.parquet`` → 行数对账 manifest（不一致即抛错）。

    幂等：重跑覆盖同名产物。快照本体（TSV + manifest）只读，绝不回写。
    """
    manifest = load_manifest(snapshot_dir)
    manifest_rows = {record.table: record.records for record in manifest.tables}

    frames: list[pd.DataFrame] = []
    stats_rows: dict[str, int] = {}
    stats_uncertain: dict[str, int] = {}
    for table in ("lv", "stages", "engines"):
        frame = table_frame(snapshot_dir, table)
        # 行数对账：ETL 读到的行数必须与 manifest 一致——快照对账（verify）只管
        # 字节级一致，这里再拦一道"解析丢了行"（如注释误判吞行）。
        expected = manifest_rows.get(table)
        if expected is not None and len(frame) != expected:
            raise ValueError(
                f"{table}: ETL 解析出 {len(frame)} 行，manifest 登记 {expected} 行——解析丢行"
            )
        stats_rows[table] = len(frame)
        stats_uncertain[table] = int((frame["unit_uncertain"].fillna("") != "").sum())
        frames.append(frame)

    merged = pd.concat(frames, ignore_index=True)
    merged.to_parquet(snapshot_dir / RECORDS_PARQUET, index=False)
    return EtlStats(stats_rows, stats_uncertain, len(merged))
