"""§7.7 仓储层：业务代码取数的**唯一**入口（禁止业务代码拼 SQL）。

本模块与 ``db.py`` 是仅有的两处允许出现 ``sqlite3`` / SQL 的地方（机检门禁
``test_gcat_db.py::test_sql_confined_to_storage_modules``）。所有列表查询都经
``record_tags`` 标签索引（§7.2"查询必须走标签索引，禁止全表扫描"）；单条取数走
``record_id``（rowid 别名）。

一份库 = 一份快照（``db.build_database`` 整体重建），故查询不携带快照过滤；
本库对应哪份快照由 :meth:`CatalogRepository.snapshot` 声明，调用方应将其纳入
provenance（§7.8 红线：没有溯源的数值不得出现在报告或界面上）。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import TypeVar

from aeroforge.data.models import (
    EngineFamilyCount,
    EngineRecord,
    SnapshotMeta,
    StageLink,
    StageRecord,
    TagInfo,
    VehicleRecord,
)

_Record = TypeVar("_Record", VehicleRecord, StageRecord, EngineRecord)


class CatalogRepository:
    """GCAT 目录库的只读仓储。

    生命周期：构造即开连接，:meth:`close` 或 with 块退出时关闭。
    连接为默认单线程模式——API 层在同一事件循环线程内使用即可。
    """

    def __init__(self, db_path: Path) -> None:
        if not db_path.is_file():
            raise FileNotFoundError(
                f"目录库不存在：{db_path}——先建库：uv run python tools/gcat_db.py"
            )
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row

    def __enter__(self) -> CatalogRepository:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # ── 元信息 ────────────────────────────────────────────────────────────────

    def snapshot(self) -> SnapshotMeta:
        """本库的快照来源（§7.8：进入 provenance 的锚点）。"""
        rows = self._conn.execute("SELECT id, release, total_records FROM snapshots").fetchall()
        if len(rows) != 1:
            raise ValueError(f"snapshots 表应有且仅有一行，实际 {len(rows)} 行")
        row = rows[0]
        return SnapshotMeta(
            id=str(row["id"]),
            release=str(row["release"]),
            total_records=int(row["total_records"]),
        )

    # ── 标签索引（§7.2） ─────────────────────────────────────────────────────

    def tags(self, *, level: int | None = None, kind: str | None = None) -> list[TagInfo]:
        """枚举标签清单（可按层级 / 维度过滤），按 ``level, kind, value`` 排序。"""
        sql = "SELECT tag_id, level, kind, value FROM tags"
        clauses: list[str] = []
        params: list[object] = []
        if level is not None:
            clauses.append("level = ?")
            params.append(level)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY level, kind, value"
        return [
            TagInfo(
                tag_id=int(row["tag_id"]),
                level=int(row["level"]),
                kind=row["kind"],
                value=row["value"],
            )
            for row in self._conn.execute(sql, params)
        ]

    # ── 实体查询（一律走 record_tags 索引） ──────────────────────────────────

    def query_vehicles(
        self,
        *,
        tags: Mapping[str, str] | None = None,
        include_incomplete: bool = True,
    ) -> list[VehicleRecord]:
        """按标签组合筛选完整火箭；``tags`` 为 kind → value 的 AND 条件。"""
        return self._query("vehicles", VehicleRecord, tags or {}, include_incomplete)

    def query_stages(
        self,
        *,
        tags: Mapping[str, str] | None = None,
        include_incomplete: bool = True,
    ) -> list[StageRecord]:
        """按标签组合筛选级（推力字段真空 / 海平面分列，§7.5 规则 3）。"""
        return self._query("stages", StageRecord, tags or {}, include_incomplete)

    def query_engines(
        self,
        *,
        tags: Mapping[str, str] | None = None,
        include_incomplete: bool = True,
    ) -> list[EngineRecord]:
        """按标签组合筛选发动机（``typical_thrust_n`` 环境未声明，OI-35）。"""
        return self._query("engines", EngineRecord, tags or {}, include_incomplete)

    def get_vehicle(self, record_id: int) -> VehicleRecord:
        return self._get("vehicles", VehicleRecord, record_id)

    def get_stage(self, record_id: int) -> StageRecord:
        return self._get("stages", StageRecord, record_id)

    def get_engine(self, record_id: int) -> EngineRecord:
        return self._get("engines", EngineRecord, record_id)

    # ── 装配关系（lvs.tsv → stage_links） ────────────────────────────────────

    def stage_links(self, lv_name: str, lv_variant: str) -> list[StageLink]:
        """完整火箭 → 各级（按 ``Stage_No`` 数值序）。无装配记录返回空表。"""
        rows = self._conn.execute(
            "SELECT LV_Name, LV_Variant, Stage_No, Stage_Name, Qualifier FROM stage_links"
            " WHERE LV_Name = ? AND LV_Variant = ?"
            " ORDER BY CAST(Stage_No AS REAL), Stage_No",
            (lv_name, lv_variant),
        ).fetchall()
        return [
            StageLink(
                lv_name=row["LV_Name"],
                lv_variant=row["LV_Variant"],
                stage_no=row["Stage_No"],
                stage_name=row["Stage_Name"],
                qualifier=row["Qualifier"],
            )
            for row in rows
        ]

    # ── 发动机族谱（§7.4） ────────────────────────────────────────────────────

    def engine_families(self) -> list[EngineFamilyCount]:
        """族清单 + 每族发动机计数（按族名序）。

        只列**确有发动机记录**的族（engines 表按 ``family`` 列聚合）；GCAT 的
        engines 行有相当比例 ``family`` 缺失（多为固体助推器），那些记录不归任何族
        ——如实排除，不得归并成「未知族」这种发明出来的桶。
        """
        rows = self._conn.execute(
            "SELECT family, COUNT(*) AS n FROM engines"
            " WHERE family IS NOT NULL AND family != ''"
            " GROUP BY family ORDER BY family"
        ).fetchall()
        return [EngineFamilyCount(family=str(row["family"]), count=int(row["n"])) for row in rows]

    def engines_in_family(self, family: str) -> list[EngineRecord]:
        """单族内的发动机记录（按 record_id 序）；无此族返回空表。"""
        rows = self._conn.execute(
            'SELECT * FROM "engines" WHERE family = ? ORDER BY record_id', (family,)
        ).fetchall()
        return [self._to_record(EngineRecord, row) for row in rows]

    # ── 内部：SQL 的全部形态都在这两个私有方法里 ─────────────────────────────

    def _query(
        self,
        db_table: str,
        record_cls: type[_Record],
        tag_filter: Mapping[str, str],
        include_incomplete: bool,
    ) -> list[_Record]:
        """标签组合查询：每个 kind 一个 EXISTS 子查询（全部命中 ``idx_record_tags_by_tag``）。

        表名来自本模块常量、值走绑定参数——无拼接注入面。
        """
        clauses: list[str] = []
        params: list[object] = []
        if not include_incomplete:
            clauses.append("tag_incomplete = ''")
        for kind, value in sorted(tag_filter.items()):
            clauses.append(
                "EXISTS (SELECT 1 FROM record_tags rt JOIN tags t ON t.tag_id = rt.tag_id"
                " WHERE rt.record_table = ? AND rt.record_id = e.record_id"
                " AND t.kind = ? AND t.value = ?)"
            )
            params += [db_table, kind, value]
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f'SELECT * FROM "{db_table}" e{where} ORDER BY e.record_id'
        return [self._to_record(record_cls, row) for row in self._conn.execute(sql, params)]

    def _get(self, db_table: str, record_cls: type[_Record], record_id: int) -> _Record:
        row = self._conn.execute(
            f'SELECT * FROM "{db_table}" WHERE record_id = ?', (record_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"{db_table}: record_id={record_id} 不存在")
        return self._to_record(record_cls, row)

    @staticmethod
    def _to_record(record_cls: type[_Record], row: sqlite3.Row) -> _Record:
        """行 → dataclass：列名与字段名逐一对齐，缺列即炸（防 schema 漂移静默丢字段）。"""
        names = {f.name for f in dataclass_fields(record_cls)}
        available = set(row.keys())
        missing = names - available
        if missing:
            raise ValueError(f"{record_cls.__name__} 缺少列：{sorted(missing)}")
        payload = {name: row[name] for name in names}
        return record_cls(**payload)
