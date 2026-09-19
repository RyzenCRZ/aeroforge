"""SQLite 存储（ADR-007 / §7.7）：建库、装载与三层标签索引（§7.2）。

职责边界：

- 本模块与 ``repository.py`` 是**仅有的两处**允许出现 ``sqlite3`` 与 SQL 的地方；
  业务代码一律走仓储层，``test_gcat_db.py`` 有机检门禁。
- 建库是**整体重建**：一份 ``aeroforge.db`` = 一份快照。先写 ``*.building`` 临时
  文件再原子替换，中途失败不破坏旧库。
- 实体表的 DDL 从 ETL 的 ``TABLE_FIELDS`` **程序化生成**：源单位非空 → REAL，
  否则 TEXT。ETL 改字段，建库自动跟上，不另抄一份名单（另有 dataclass 同步门禁）。

三层标签的落点（§7.2）：

- 第一层 ``entity``：vehicle / stage / engine——每条记录恰一枚；
- 第二层技术特征：vehicle → ``stages``（末级号 ≈ 核心级数）/ ``class``；
  stage → ``engine``；engine → ``propellant``（Oxidizer/Fuel 规范化为 ``OX/FUEL``）；
- 第三层地理/机构：``manufacturer`` + ``country``——GCAT 厂商列存的是**机构代码**
  （如 ``SPX``），经 orgs.tsv 解析为机构名（``SpaceX``）并带出 ``StateCode`` 作国家
  枚举（``US``）；代码解析失败回退原码且**不给**国家标签（不猜）。这是 §7.2 核心
  场景「美国 + LOX/CH4 → 定位 Raptor 系列」能成立的唯一途径。

只有**源字段存在却缺失**的维度才计入 ``tag_incomplete``（§7.2"标签缺失"）。
GCAT 结构性不提供的维度（室压、循环方式）不算缺失——那属于 §7.9 手动补录的缺口，
把它们标成缺失会让 ``tag_incomplete`` 变成无信息噪声。
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from aeroforge.data.etl import (
    ENGINE_FIELDS,
    LV_FIELDS,
    RECORDS_PARQUET,
    STAGE_FIELDS,
    FieldMap,
)
from aeroforge.data.snapshot import SNAPSHOT_TABLES, load_manifest, read_table

#: SQLite 产物名（§7.7：``data/aeroforge.db``）
DB_NAME = "aeroforge.db"

#: ETL 表键 → SQLite 实体表名（§7.7 表设计用规范化复数名）
ENTITY_TABLES: dict[str, str] = {"lv": "vehicles", "stages": "stages", "engines": "engines"}

#: 实体表 → 各自的 ETL 字段声明（DDL 程序化生成的唯一来源）
_TABLE_FIELDS: dict[str, tuple[FieldMap, ...]] = {
    "vehicles": LV_FIELDS,
    "stages": STAGE_FIELDS,
    "engines": ENGINE_FIELDS,
}

#: 第一层标签值（§7.2 第一层：完整火箭 / 级 / 发动机）
ENTITY_TAG_VALUE: dict[str, str] = {
    "vehicles": "vehicle",
    "stages": "stage",
    "engines": "engine",
}

#: 每类实体的**期望标签维度**（kind 集合）——源字段有而未派生出的计入 ``tag_incomplete``
EXPECTED_TAG_KINDS: dict[str, tuple[str, ...]] = {
    "vehicles": ("manufacturer", "stages", "class"),
    "stages": ("manufacturer", "engine"),
    "engines": ("manufacturer", "propellant"),
}

#: 纯文本关联表（§7.5：无单位风险，由标签层直接消费 TSV）：
#: (快照表键, SQLite 表名, 建查询索引的列——仅当表头确实含这些列时才建)
_LINK_TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("lvs", "stage_links", ("LV_Name", "LV_Variant")),
    ("orgs", "organizations", ("Code",)),
    ("family", "families", ()),
)

_META_COLUMNS: tuple[tuple[str, str], ...] = (
    ("record_id", "INTEGER PRIMARY KEY"),
    ("snapshot", "TEXT NOT NULL REFERENCES snapshots (id)"),
    ("source_row", "INTEGER NOT NULL"),
    ("quality", "TEXT NOT NULL"),
    ("unit_uncertain", "TEXT NOT NULL DEFAULT ''"),
    ("source_flags", "TEXT NOT NULL DEFAULT ''"),
    ("tag_incomplete", "TEXT NOT NULL DEFAULT ''"),
)

#: 装载时逐行写入的元数据列（跳过自增的 record_id）
_ROW_META_COLUMNS: tuple[str, ...] = tuple(name for name, _ in _META_COLUMNS[1:])


def _entity_ddl(db_table: str) -> str:
    """单实体表 DDL：元数据列 + FieldMap 派生列（有源单位 → REAL，否则 TEXT）。"""
    columns = [f"{name} {decl}" for name, decl in _META_COLUMNS]
    for field in _TABLE_FIELDS[db_table]:
        if field.keep_raw:
            continue
        decl = "REAL" if field.source_unit is not None else "TEXT"
        columns.append(f'"{field.internal}" {decl}')
    columns.append("UNIQUE (snapshot, source_row)")
    body = ",\n  ".join(columns)
    return f'CREATE TABLE "{db_table}" (\n  {body}\n)'


SCHEMA = ";\n".join(
    [
        "CREATE TABLE snapshots (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  release TEXT NOT NULL,\n"
        "  total_records INTEGER NOT NULL\n"
        ")",
        *(_entity_ddl(name) for name in ENTITY_TABLES.values()),
        "CREATE TABLE tags (\n"
        "  tag_id INTEGER PRIMARY KEY,\n"
        "  level INTEGER NOT NULL,\n"
        "  kind TEXT NOT NULL,\n"
        "  value TEXT NOT NULL,\n"
        "  UNIQUE (level, kind, value)\n"
        ")",
        "CREATE TABLE record_tags (\n"
        "  tag_id INTEGER NOT NULL REFERENCES tags (tag_id),\n"
        "  record_table TEXT NOT NULL,\n"
        "  record_id INTEGER NOT NULL,\n"
        "  PRIMARY KEY (tag_id, record_table, record_id)\n"
        ")",
        "CREATE INDEX idx_record_tags_by_tag ON record_tags (tag_id)",
        "CREATE INDEX idx_record_tags_by_record ON record_tags (record_table, record_id)",
    ]
)


def _cell(value: Any) -> Any:
    """pandas → sqlite3 可绑定类型：NaN → None、numpy 标量 → Python 标量。"""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):  # np.float64 / np.int64 等
        item = value.item()
        if isinstance(item, float) and math.isnan(item):
            return None
        return item
    return value


def _text(value: Any) -> str | None:
    """文本单元格：缺失（``None`` 或 NaN）→ ``None``，否则修剪后的字符串。

    ⚠ 必须先过 :func:`_cell` 再判空：parquet 读回的缺失值可能是 NaN，
    直接 ``str(value)`` 会得到 ``"None"``/``"nan"`` 假串（实测给固体机
    生成了 ``propellant NONE/NONE`` 的幽灵标签）。
    """
    cell = _cell(value)
    if cell is None:
        return None
    cleaned = str(cell).strip()
    return cleaned or None


@dataclass(frozen=True)
class BuildStats:
    """建库结果摘要（CLI 打印与门禁断言用）。"""

    snapshot: str
    rows: dict[str, int]
    tags: int
    associations: int


def _row_tags(
    db_table: str,
    row: dict[str, Any],
    org_names: Mapping[str, tuple[str, str]],
) -> list[tuple[int, str, str]]:
    """单记录的标签枚举（三层，§7.2）。返回 ``(level, kind, value)`` 列表。"""
    tags = [(1, "entity", ENTITY_TAG_VALUE[db_table])]

    manufacturer = _text(row.get("manufacturer"))
    if manufacturer:
        org_name, state = org_names.get(manufacturer, ("", ""))
        tags.append((3, "manufacturer", org_name or manufacturer))
        if state:
            tags.append((3, "country", state.upper()))

    if db_table == "engines":
        oxidizer, fuel = _text(row.get("oxidizer")), _text(row.get("fuel"))
        if oxidizer and fuel:
            tags.append((2, "propellant", f"{oxidizer.upper()}/{fuel.upper()}"))
    elif db_table == "stages":
        engine = _text(row.get("engine_name"))
        if engine:
            tags.append((2, "engine", engine))
    else:  # vehicles
        # "级数"维度用 max_stage_no（末级号）：GCAT 的 Min/Max stage 是级号范围
        # （助推器记 0/−1），max 侧才与"几级火箭"对齐（§7.3 更正）
        max_stage_no = _cell(row.get("max_stage_no"))
        if max_stage_no is not None:
            tags.append((2, "stages", str(int(max_stage_no))))
        vehicle_class = _text(row.get("vehicle_class"))
        if vehicle_class:
            tags.append((2, "class", vehicle_class.upper()))
    return tags


class _TagIndex:
    """标签枚举缓存：同值只入 ``tags`` 表一次，返回稳定 ``tag_id``。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._ids: dict[tuple[int, str, str], int] = {}

    def id_for(self, level: int, kind: str, value: str) -> int:
        key = (level, kind, value)
        existing = self._ids.get(key)
        if existing is not None:
            return existing
        cursor = self._conn.execute(
            "INSERT INTO tags (level, kind, value) VALUES (?, ?, ?)", (level, kind, value)
        )
        tag_id = cursor.lastrowid
        assert tag_id is not None  # 新插入行必有 rowid
        self._ids[key] = tag_id
        return tag_id

    def __len__(self) -> int:
        return len(self._ids)


def _load_org_names(snapshot_dir: Path) -> dict[str, tuple[str, str]]:
    """orgs.tsv → ``{Code: (机构名, 国家码)}``——第三层标签的解析字典。

    ``Code`` 唯一（GCAT 主键），首见优先；``Name`` 缺失回退 ``ShortName``。
    解析失败（代码不在表内）由调用方回退原码，这里不做任何猜测。
    """
    text = (snapshot_dir / SNAPSHOT_TABLES["orgs"]).read_text(encoding="utf-8", errors="replace")
    _, rows = read_table(text)
    mapping: dict[str, tuple[str, str]] = {}
    for row in rows:
        code = (row.get("Code") or "").strip()
        if not code:
            continue
        name = (row.get("Name") or "").strip() or (row.get("ShortName") or "").strip()
        state = (row.get("StateCode") or "").strip()
        mapping.setdefault(code, (name, state))
    return mapping


def _insert_entities_rows(
    conn: sqlite3.Connection,
    snapshot_id: str,
    frame: pd.DataFrame,
    etl_key: str,
    tag_index: _TagIndex,
    org_names: Mapping[str, tuple[str, str]],
) -> tuple[int, list[tuple[int, str, int]]]:
    """（由 build_database 调用）实体表装载的行级循环。"""
    db_table = ENTITY_TABLES[etl_key]
    fields = _TABLE_FIELDS[db_table]
    value_columns = [f.internal for f in fields if not f.keep_raw]
    insert_columns = list(_ROW_META_COLUMNS) + value_columns
    placeholders = ", ".join("?" for _ in insert_columns)
    sql = f'INSERT INTO "{db_table}" ({", ".join(insert_columns)}) VALUES ({placeholders})'

    associations: list[tuple[int, str, int]] = []
    frame_cols = list(frame.columns)
    loaded = 0
    for raw in frame.itertuples(index=False, name=None):
        row = dict(zip(frame_cols, raw, strict=True))
        derived = _row_tags(db_table, row, org_names)
        present = {kind for _, kind, _ in derived}
        missing = [kind for kind in EXPECTED_TAG_KINDS[db_table] if kind not in present]

        meta_values: list[Any] = [
            snapshot_id,
            _cell(row.get("source_row")),
            _cell(row.get("quality")),
            _cell(row.get("unit_uncertain")),
            _cell(row.get("source_flags")),
            ",".join(missing),
        ]
        entity_values = meta_values + [_cell(row.get(name)) for name in value_columns]
        cursor = conn.execute(sql, entity_values)
        record_id = int(cursor.lastrowid)  # type: ignore[arg-type]
        for level, kind, value in derived:
            associations.append((tag_index.id_for(level, kind, value), db_table, record_id))
        loaded += 1
    return loaded, associations


def _load_link_tables(
    conn: sqlite3.Connection, snapshot_dir: Path, snapshot_id: str
) -> dict[str, int]:
    """三张纯文本关联表按 TSV 原样装载（全 TEXT 列 + snapshot 溯源列）。"""
    counts: dict[str, int] = {}
    for table_key, db_name, index_columns in _LINK_TABLES:
        text = (snapshot_dir / SNAPSHOT_TABLES[table_key]).read_text(
            encoding="utf-8", errors="replace"
        )
        header, rows = read_table(text)
        if not header:
            raise ValueError(f"{SNAPSHOT_TABLES[table_key]}：空表头，无法建关联表 {db_name}")

        column_sql = ", ".join(f'"{name}" TEXT' for name in header)
        conn.execute(f'CREATE TABLE "{db_name}" ({column_sql}, "snapshot" TEXT NOT NULL)')
        placeholders = ", ".join("?" for _ in header) + ", ?"
        payload = [(*(_cell(row.get(name)) for name in header), snapshot_id) for row in rows]
        conn.executemany(f'INSERT INTO "{db_name}" VALUES ({placeholders})', payload)

        if index_columns and set(index_columns) <= set(header):
            index_sql = ", ".join(f'"{name}"' for name in index_columns)
            conn.execute(f'CREATE INDEX "idx_{db_name}_lookup" ON "{db_name}" ({index_sql})')
        counts[db_name] = len(payload)
    return counts


def build_database(snapshot_dir: Path, db_path: Path) -> BuildStats:
    """快照 + records.parquet → SQLite（整体重建，原子替换）。

    快照本体只读；``records.parquet`` 缺失时指路 ETL，而不是静默建出空库。
    """
    manifest = load_manifest(snapshot_dir)
    parquet_path = snapshot_dir / RECORDS_PARQUET
    if not parquet_path.is_file():
        raise FileNotFoundError(
            f"{parquet_path} 不存在——先跑 ETL：uv run python tools/gcat_etl.py --out {snapshot_dir}"
        )
    frame = pd.read_parquet(parquet_path)
    snapshot_id = snapshot_dir.name

    db_path.parent.mkdir(parents=True, exist_ok=True)
    building = db_path.with_name(db_path.name + ".building")
    conn = sqlite3.connect(building)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO snapshots (id, release, total_records) VALUES (?, ?, ?)",
            (snapshot_id, manifest.gcat_release, manifest.total_records),
        )

        tag_index = _TagIndex(conn)
        org_names = _load_org_names(snapshot_dir)
        rows: dict[str, int] = {}
        associations: list[tuple[int, str, int]] = []
        for etl_key, db_table in ENTITY_TABLES.items():
            sub = frame[frame["table"] == etl_key]
            count, entity_associations = _insert_entities_rows(
                conn, snapshot_id, sub, etl_key, tag_index, org_names
            )
            rows[db_table] = count
            associations.extend(entity_associations)

        conn.executemany(
            "INSERT INTO record_tags (tag_id, record_table, record_id) VALUES (?, ?, ?)",
            associations,
        )
        rows.update(_load_link_tables(conn, snapshot_dir, snapshot_id))
        conn.commit()
    except BaseException:
        conn.close()
        building.unlink(missing_ok=True)
        raise
    conn.close()

    building.replace(db_path)
    return BuildStats(
        snapshot=snapshot_id,
        rows=rows,
        tags=len(tag_index),
        associations=len(associations),
    )
