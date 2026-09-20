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
from dataclasses import dataclass
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
from aeroforge.params.templates import normalize_name

_Record = TypeVar("_Record", VehicleRecord, StageRecord, EngineRecord)

#: 检索命中项的可用性摘要检查列（§7.5：缺失是状态不是 0，缺列不入摘要）。
#: glow = launch_mass_kg（§6.1 起飞质量口径）；leo_capacity = payload_leo_kg。
#: 键 = vehicles 表列名（与 unit_uncertain / missing 的内部字段名同口径，前端做中文标注）。
_AVAILABILITY_COLUMNS: tuple[str, ...] = (
    "launch_mass_kg",
    "length_m",
    "diameter_m",
    "payload_leo_kg",
)

#: 检索排序档位（OI-39：名称完全匹配 > 前缀 > 子串；族/变体命中殿后）。
_MATCH_KIND_BY_RANK: dict[int, str] = {
    0: "exact",
    1: "prefix",
    2: "substring",
    3: "family_or_variant",
}


@dataclass(frozen=True)
class VehicleSearchHit:
    """型号检索的一条候选（OI-39 ②：GCAT lv 记录的检索投影）。

    ``manufacturer_tag`` / ``country_tag`` 来自第三层标签（orgs 解析后的机构名与
    国家码，§7.2），**不是** vehicles 表的原始厂商代码列。
    """

    record_id: int
    name: str
    family: str | None
    variant: str | None
    manufacturer_tag: str | None
    country_tag: str | None
    #: 核心级数 = max_stage_no 取整（§7.3：级号范围的最大侧；助推器记 0/−1）
    stage_count: int | None
    #: 可用性摘要：非缺失的概览字段（launch_mass_kg / length_m / diameter_m / payload_leo_kg）
    available_fields: tuple[str, ...]
    #: 命中方式：exact / prefix / substring / family_or_variant（排序透明化）
    match_kind: str


@dataclass(frozen=True)
class StageAssembly:
    """一条装配行：stage_links 行 + 解析出的级记录 + 该级的发动机记录。

    ``stage`` / ``engine`` 为 ``None`` 表示**缺失引用**（link 指向的名字在对应表
    中不存在）——这是显式状态而非异常，由调用方列入 missing，禁止编造替代值。
    """

    link: StageLink
    stage: StageRecord | None
    engine: EngineRecord | None


@dataclass(frozen=True)
class VehicleRecordBundle:
    """型号已知参数集的原始装配（OI-39 ③：API 层在其上做逐字段出处投影）。"""

    vehicle: VehicleRecord
    #: 经第三层标签解析的机构名 / 国家码（无标签即 None，不猜）
    manufacturer_tag: str | None
    country_tag: str | None
    #: 装配行（按 Stage_No 数值序；含缺失引用的 ``None`` 槽位）
    assemblies: tuple[StageAssembly, ...]
    #: 装配过程的非致命问题（缺失引用、同名发动机多行取首见等），须原样透传给用户
    warnings: tuple[str, ...]


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

    # ── 型号检索与已知参数集（OI-39） ─────────────────────────────────────────

    def search_vehicles(self, name: str, limit: int = 12) -> list[VehicleSearchHit]:
        """型号检索（OI-39 ②）：规范化子串匹配 lv 三列，按「完全匹配 > 前缀 > 子串」排序。

        规范化口径与 OI-34 的 :func:`aeroforge.params.templates.normalize_name`
        **同源复用**（NFKC + casefold + 去空白与连字符变体）——SQLite 的 LIKE 对
        ASCII 大小写不敏感、对中文敏感，规范化必须在 Python 侧做，故 LIKE 无法下推。

        关于全表扫描：本查询按**名称子串**检索，不在 §7.2 标签索引的射程内（那条
        规则管的是标签组合查询，标签查询仍全部走 ``idx_record_tags_by_tag``）。
        vehicles 表为 ETL 后的 lv 记录（gcat-2026Q3 实测 1 836 行，GCAT 原始 lvs.tsv
        的 4 744 行是含杂项的装配清单），Python 侧规范化扫描在此量级为毫秒级，且
        结果恒以 ``limit`` 截断——量级依据与索引缺席理由如上，量级增长到数十万行时
        再议 FTS5（届时先改规格）。
        """
        query = normalize_name(name)
        if query == "":
            return []
        rows = self._conn.execute(
            "SELECT record_id, name, family, variant, max_stage_no,"
            " launch_mass_kg, length_m, diameter_m, payload_leo_kg FROM vehicles"
        ).fetchall()

        scored: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            norm_name = normalize_name(str(row["name"] or ""))
            norm_family = normalize_name(str(row["family"] or ""))
            norm_variant = normalize_name(str(row["variant"] or ""))
            if norm_name == query:
                rank = 0
            elif norm_name.startswith(query):
                rank = 1
            elif query in norm_name:
                rank = 2
            elif query in norm_family or query in norm_variant:
                rank = 3  # 族 / 变体命中（如 "cz-5" → 族 CZ5）也算候选，但排在名称命中之后
            else:
                continue
            scored.append((rank, row))
        scored.sort(
            key=lambda pair: (pair[0], str(pair[1]["name"] or ""), str(pair[1]["variant"] or ""))
        )
        return [self._to_search_hit(rank, row) for rank, row in scored[:limit]]

    def get_vehicle_record(
        self, name: str, variant: str | None = None
    ) -> VehicleRecordBundle | None:
        """型号已知参数集的原始装配（OI-39 ③）：lv 行 + 装配关系 + 各级 + 各发动机。

        按 name + variant **精确**定位 lv 行（检索候选点选后的下一跳，不做模糊）；
        无此型号返回 ``None``（由调用方决定 404）。缺失引用不抛不编造：落在
        :class:`StageAssembly` 的 ``None`` 槽位与 ``warnings`` 里显式呈现。
        """
        key = name.strip()
        if key == "":
            return None
        rows = self._conn.execute(
            "SELECT * FROM vehicles WHERE name = ? ORDER BY record_id", (key,)
        ).fetchall()
        wanted = variant or None
        vehicle_row = next((row for row in rows if (row["variant"] or None) == wanted), None)
        if vehicle_row is None:
            return None

        vehicle = self._to_record(VehicleRecord, vehicle_row)
        tags = self._entity_tag_map("vehicles", vehicle.record_id)
        # GCAT 的装配清单（lvs.tsv → stage_links）用 '-' 表示无变体；vehicles 侧经
        # ETL 已把 '-' 规范化为 NULL——两侧键在此对齐。
        links = self.stage_links(vehicle.name, variant if variant else "-")

        warnings: list[str] = []
        assemblies: list[StageAssembly] = []
        for link in links:
            stage = self._stage_by_name(link.stage_name)
            if stage is None:
                warnings.append(
                    f"stage_links 指向的级 {link.stage_name!r} 在 stages 表中不存在"
                    "（缺失引用，如实列出，不编造）"
                )
                assemblies.append(StageAssembly(link=link, stage=None, engine=None))
                continue
            engine = (
                self._engine_first_by_name(stage.engine_name, warnings)
                if stage.engine_name is not None
                else None
            )
            assemblies.append(StageAssembly(link=link, stage=stage, engine=engine))
        return VehicleRecordBundle(
            vehicle=vehicle,
            manufacturer_tag=tags.get("manufacturer"),
            country_tag=tags.get("country"),
            assemblies=tuple(assemblies),
            warnings=tuple(warnings),
        )

    # ── 内部：SQL 的全部形态都在本节私有方法里 ────────────────────────────────

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

    def _entity_tag_map(self, db_table: str, record_id: int) -> dict[str, str]:
        """单记录的标签映射（kind → value），走 ``idx_record_tags_by_record`` 索引。"""
        rows = self._conn.execute(
            "SELECT t.kind AS kind, t.value AS value FROM record_tags rt"
            " JOIN tags t ON t.tag_id = rt.tag_id"
            " WHERE rt.record_table = ? AND rt.record_id = ?",
            (db_table, record_id),
        ).fetchall()
        return {str(row["kind"]): str(row["value"]) for row in rows}

    def _stage_by_name(self, stage_name: str | None) -> StageRecord | None:
        """按名取级；不存在或名为空返回 ``None``（缺失引用，不抛）。

        stages.name 在 GCAT 源内唯一（gcat-2026Q3 实测无重名行），故单行取数。
        """
        if not stage_name:
            return None
        row = self._conn.execute(
            'SELECT * FROM "stages" WHERE name = ? ORDER BY record_id', (stage_name,)
        ).fetchone()
        return None if row is None else self._to_record(StageRecord, row)

    def _engine_first_by_name(self, engine_name: str, warnings: list[str]) -> EngineRecord | None:
        """按名取发动机；缺失 → ``None`` + warning；同名多行 → 取首见 + warning。

        engines 表没有 variant 列（GCAT 源如此），理论上可能出现同名多行——
        gcat-2026Q3 实测无重名，但这里按任务纪律防御：取 record_id 最小者并留痕。
        """
        rows = self._conn.execute(
            'SELECT * FROM "engines" WHERE name = ? ORDER BY record_id', (engine_name,)
        ).fetchall()
        if not rows:
            warnings.append(
                f"级引用的发动机 {engine_name!r} 在 engines 表中不存在"
                "（缺失引用，如实列出，不编造）"
            )
            return None
        if len(rows) > 1:
            warnings.append(
                f"engines 表存在 {len(rows)} 行同名记录 {engine_name!r}"
                "（表无 variant 列），取 record_id 最小者"
            )
        return self._to_record(EngineRecord, rows[0])

    def _to_search_hit(self, rank: int, row: sqlite3.Row) -> VehicleSearchHit:
        """检索行 → 候选：附第三层标签（manufacturer/country）与可用性摘要。"""
        tags = self._entity_tag_map("vehicles", int(row["record_id"]))
        available = tuple(column for column in _AVAILABILITY_COLUMNS if row[column] is not None)
        max_stage_no = row["max_stage_no"]
        return VehicleSearchHit(
            record_id=int(row["record_id"]),
            name=str(row["name"]),
            family=row["family"],
            variant=row["variant"],
            manufacturer_tag=tags.get("manufacturer"),
            country_tag=tags.get("country"),
            stage_count=int(max_stage_no) if max_stage_no is not None else None,
            available_fields=available,
            match_kind=_MATCH_KIND_BY_RANK[rank],
        )
