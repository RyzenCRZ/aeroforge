"""GCAT 目录库门禁（§7.7 / §7.2 / ADR-007）：建库、标签索引、仓储隔离。

全部离线：快照由 ``fetch_snapshot`` + 假下载器在临时目录现造，库文件落在
``tmp_path``，绝不触碰开发机 ``data/aeroforge.db``。

三条"禁止"各有对应机检：

1. 禁止业务拼 SQL（§7.7）→ ``test_sql_confined_to_storage_modules``
2. 禁止全表扫描（§7.2）→ 查询只提供标签组合与主键两种形态（结构保证）
3. 标签缺失不静默（§7.2）→ ``tag_incomplete`` 逐维度断言
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import fields as dataclass_fields
from pathlib import Path

import pytest

from aeroforge.data.db import DB_NAME, EXPECTED_TAG_KINDS, build_database
from aeroforge.data.etl import TABLE_FIELDS, run_etl
from aeroforge.data.models import EngineRecord, StageRecord, VehicleRecord
from aeroforge.data.repository import CatalogRepository
from aeroforge.data.snapshot import fetch_snapshot

# ── 夹具：三张业务表（engines 多一行缺推进剂的固体机）+ 三张关联表 ──────────

FAKE_LV = (
    "#LV_Name\tLV_Family\tLV_Variant\tLV_Manufacturer\tLV_Min_Stage\tLV_Max_Stage\tLength"
    "\tDiameter\tLaunch_Mass\tLEO_Capacity\tGTO_Capacity\tTO_Thrust\tClass\tLFlag\tMFlag\tDFlag\n"
    "# Updated 2026 Sep 18\n"
    "N-1 11A52\tN-1\t-\tOKB1\t1\t2\t105.3\t14.00\t2,788.0\t70000\t-\t45300\tO\t-\t-\t-\n"
    "BadRocket\tX\t-\tY\t1\t1\t99999\t2.0\t12ab\t5\t5\t5\tO\t1\t-\t-\n"
)

FAKE_STAGES = (
    "#Stage_Name\tStage_Family\tStage_Manufacturer\tLength\tDiameter\tLaunch_Mass"
    "\tDry_Mass\tThrust\tThrustSL\tDuration\tEngine\tNEng\tLength_Flag\tDiameter_Flag"
    "\tLaunch_Mass_Flag\tDry_Mass_Flag\tThrust_Flag\tThrustSL_Flag\n"
    "# Updated 2026 Sep 18\n"
    "N-1 B1\tN-1\tOKB1\t22.0\t17.0\t1300.0\t65000\t42000\t38000\t130\tNK-15\t30\t-\t-\t-\t-\t-\t-\n"
    "S-IC\tSaturn V\tBoeing\t42.0\t10.0\t2300.0\t-\t?-kN\t?\t168\tF-1\t5\t-\t-\t-\t-\t-\t-\n"
)

FAKE_ENGINES = (
    "#Name\tManufacturer\tFamily\tAlt_Name\tOxidizer\tFuel\tMass\tMFlag\tImpulse"
    "\tImpFlag\tThrust\tTFlag\tIsp\tIspFlag\tDuration\tDurFlag\tChambers\tDate\tUsage\tGroup\n"
    "# Updated 2026 Sep 18\n"
    "NK-15\tOKB6\tNK\t-\tLOX\tKerosene\t1200\t-\t-\t-\t1400\t-\t327\t-\t120\t-\t1\t1969\tN-1B1\t-\n"
    "WeirdMotor\tX\tY\t-\tLOX\tCH4\t500\t-\t100\t-\t50\t-\t132800\t-\t60\t-\t1\t2020\tZ (1)\t-\n"
    # 第三行：固体机不填氧化剂/燃料 → tag_incomplete 必须带 propellant（§7.2）
    "SolidBooster\tX\tY\t-\t-\t-\t900\t-\t5000\t-\t-\t-\t-\t-\t80\t-\t1\t2020\t-\t-\n"
)

FAKE_LVS = (
    "#LV_Name\tLV_Variant\tStage_No\tStage_Name\tQualifier\n"
    "# Updated 2026 Sep 18\n"
    "N-1\t11A52\t1\tN-1 B1\t-\n"
    "N-1\t11A52\t2\tN-1 B2\t-\n"
)

FAKE_ORGS = (
    "#Code\tUCode\tStateCode\tType\tClass\tTStart\tTStop\tShortName\tName\tLocation"
    "\tLongitude\tLatitude\tError\tParent\tShortEName\tEName\tUName\n"
    "OKB1\t-\tUSSR\tC\t-\t1955\t-\tOKB-1\tOKB-1\tSoviet Union\t-\t-\t-\t-\t-\tOKB-1\tOKB-1\n"
)

FAKE_FAMILY = "#Family\nN-1\nSaturn V\n"


def per_file_payload() -> dict[str, bytes]:
    return {
        "lv.tsv": FAKE_LV.encode("utf-8"),
        "stages.tsv": FAKE_STAGES.encode("utf-8"),
        "engines.tsv": FAKE_ENGINES.encode("utf-8"),
        "family.tsv": FAKE_FAMILY.encode("utf-8"),
        "orgs.tsv": FAKE_ORGS.encode("utf-8"),
        "lvs.tsv": FAKE_LVS.encode("utf-8"),
    }


def downloader(payloads: dict[str, bytes]) -> Callable[[str], bytes]:
    def download(url: str) -> bytes:
        for filename, payload in payloads.items():
            if url.endswith(f"/{filename}"):
                return payload
        raise AssertionError(f"未知下载地址：{url}")

    return download


@pytest.fixture()
def snapshot_dir(tmp_path: Path) -> Path:
    """现造快照并跑完 ETL——建库消费 records.parquet。"""
    out = tmp_path / "gcat-2026Q3"
    fetch_snapshot(out, release="1.8.7", downloader=downloader(per_file_payload()))
    run_etl(out)
    return out


@pytest.fixture()
def db_path(snapshot_dir: Path, tmp_path: Path) -> Path:
    path = tmp_path / "catalog" / DB_NAME
    build_database(snapshot_dir, path)
    return path


@pytest.fixture()
def repo(db_path: Path) -> Iterator[CatalogRepository]:
    repository = CatalogRepository(db_path)
    yield repository
    repository.close()


def _names(records: list[VehicleRecord] | list[StageRecord] | list[EngineRecord]) -> list[str]:
    return [record.name for record in records]


# ── 建库 ─────────────────────────────────────────────────────────────────────


def test_build_writes_counts_and_snapshot_meta(db_path: Path) -> None:
    with CatalogRepository(db_path) as repository:
        meta = repository.snapshot()
        assert meta.id == "gcat-2026Q3"
        assert meta.release == "1.8.7"
        # manifest 登记的总行数 = 2 lv + 2 stages + 3 engines + 2 lvs + 1 orgs + 2 family
        assert meta.total_records == 12

        assert _names(repository.query_vehicles()) == ["N-1 11A52", "BadRocket"]
        assert _names(repository.query_stages()) == ["N-1 B1", "S-IC"]
        assert _names(repository.query_engines()) == ["NK-15", "WeirdMotor", "SolidBooster"]


def test_build_requires_etl_output(tmp_path: Path) -> None:
    out = tmp_path / "gcat-2026Q3"
    fetch_snapshot(out, release="1.8.7", downloader=downloader(per_file_payload()))
    with pytest.raises(FileNotFoundError, match="gcat_etl"):
        build_database(out, tmp_path / DB_NAME)


def test_rebuild_replaces_atomically_and_idempotent(snapshot_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "catalog" / DB_NAME
    first = build_database(snapshot_dir, db)
    second = build_database(snapshot_dir, db)

    assert first.rows == second.rows
    assert first.tags == second.tags
    assert first.associations == second.associations
    assert not db.with_name(DB_NAME + ".building").exists()  # 临时文件已原子替换走

    with CatalogRepository(db) as repository:
        assert len(repository.query_engines()) == 3


# ── 标签索引（§7.2） ─────────────────────────────────────────────────────────


def test_tag_query_finds_engines_by_propellant_and_manufacturer(repo: CatalogRepository) -> None:
    """核心场景的最小复现：LOX/CH4 + 厂商 X → 定位到单一发动机。"""
    hits = repo.query_engines(tags={"propellant": "LOX/CH4", "manufacturer": "X"})
    assert _names(hits) == ["WeirdMotor"]

    kerosene = repo.query_engines(tags={"propellant": "LOX/KEROSENE"})
    assert _names(kerosene) == ["NK-15"]  # 规范化为大写枚举，源值 "Kerosene" 命中

    assert repo.query_engines(tags={"propellant": "LOX/CH4", "stages": "2"}) == []


def test_tag_incomplete_lists_only_source_missing_dimensions(repo: CatalogRepository) -> None:
    """§7.2"标签缺失"：只标**源字段存在却缺失**的维度，绝不发明维度。"""
    solid = next(engine for engine in repo.query_engines() if engine.name == "SolidBooster")
    assert solid.tag_incomplete == "propellant"  # 固体机无氧化剂/燃料列值
    assert solid.manufacturer == "X"  # 厂商在，不算缺失

    # 排除 tag_incomplete 后只剩两台完整发动机
    complete = repo.query_engines(include_incomplete=False)
    assert _names(complete) == ["NK-15", "WeirdMotor"]
    assert all(engine.tag_incomplete == "" for engine in complete)

    # 防回归门禁：parquet 缺失值读回可能是 NaN，str() 会产出 "None" 假串——
    # 曾给固体机生成幽灵标签 propellant NONE/NONE，全库不得出现任何 NONE 值
    assert all("NONE" not in tag.value for tag in repo.tags())


def test_unit_uncertain_is_not_tag_incomplete(repo: CatalogRepository) -> None:
    """两条质量线各管各的：单位疑点（§7.5）≠ 标签缺失（§7.2）。"""
    bad = next(vehicle for vehicle in repo.query_vehicles() if vehicle.name == "BadRocket")
    assert "length_m" in bad.unit_uncertain
    assert bad.tag_incomplete == ""  # 厂商/级数/类别三维度都派生成功


def test_vehicle_query_by_class_and_stage_count(repo: CatalogRepository) -> None:
    """级数维度用 max_stage_no（末级号）：N-1 夹具 Min 1 / Max 2 → stages:2。"""
    n1 = repo.query_vehicles(tags={"class": "O", "stages": "2"})
    assert _names(n1) == ["N-1 11A52"]

    one_stage = repo.query_vehicles(tags={"stages": "1"})
    assert _names(one_stage) == ["BadRocket"]

    assert repo.query_vehicles(tags={"class": "O", "stages": "3"}) == []


def test_tags_enumeration_is_levelled(repo: CatalogRepository) -> None:
    """枚举随快照版本管理（§7.2）：同值只入一份，且带层级可过滤。"""
    manufacturers = repo.tags(level=3, kind="manufacturer")
    assert [tag.value for tag in manufacturers] == ["Boeing", "OKB-1", "OKB6", "X", "Y"]

    entities = repo.tags(kind="entity")
    assert [(tag.level, tag.value) for tag in entities] == [
        (1, "engine"),
        (1, "stage"),
        (1, "vehicle"),
    ]


def test_manufacturer_resolves_via_orgs_and_carries_country(repo: CatalogRepository) -> None:
    """第三层（§7.2）：厂商代码经 orgs.tsv 解析为机构名并带出国家码。

    GCAT 厂商列存的是机构代码（真实库如 ``SPX``）——不解析则"美国 + LOX/CH4
    → Raptor"这一核心场景永远查不出来。解析失败回退原码（``Y``），且**不给**
    国家标签（不猜）。
    """
    soviet = repo.query_vehicles(tags={"manufacturer": "OKB-1", "country": "USSR"})
    assert _names(soviet) == ["N-1 11A52"]  # OKB1 → OKB-1（orgs.tsv 解析）+ StateCode USSR

    unresolved = repo.query_vehicles(tags={"manufacturer": "Y"})
    assert _names(unresolved) == ["BadRocket"]  # 代码不在 orgs 表 → 原码入枚举

    assert [tag.value for tag in repo.tags(kind="country")] == ["USSR"]


# ── 行级往返与装配关系 ───────────────────────────────────────────────────────


def test_record_roundtrip_preserves_si_values_and_flags(repo: CatalogRepository) -> None:
    n1 = next(vehicle for vehicle in repo.query_vehicles() if vehicle.name == "N-1 11A52")
    assert n1.min_stage_no == pytest.approx(1.0)  # 级号范围：起始级号（非级数，§7.3 更正）
    assert n1.max_stage_no == pytest.approx(2.0)  # 末级号 ≈ 核心级数
    assert n1.launch_mass_kg == pytest.approx(2_788_000.0)  # t → kg（ETL 已换算）
    assert n1.liftoff_thrust_n == pytest.approx(45_300_000.0)  # kN → N，海平面侧（OI-35）
    assert n1.source_row == 1
    assert n1.quality == "literature"

    engine = next(item for item in repo.query_engines() if item.name == "NK-15")
    assert engine.isp_vacuum_s == pytest.approx(327.0)
    assert engine.loaded_mass_kg == pytest.approx(1200.0)

    fetched = repo.get_vehicle(n1.record_id)
    assert fetched == n1  # 主键取数与列表查询一致

    with pytest.raises(KeyError, match="record_id=999"):
        repo.get_vehicle(999)


def test_stage_links_lookup(repo: CatalogRepository) -> None:
    """lvs.tsv → 装配关系：完整火箭 → 各级（数值序）。"""
    links = repo.stage_links("N-1", "11A52")
    assert [(link.stage_no, link.stage_name) for link in links] == [
        ("1", "N-1 B1"),
        ("2", "N-1 B2"),
    ]
    assert repo.stage_links("Vanguard", "S01") == []


# ── 结构门禁 ─────────────────────────────────────────────────────────────────


def test_dataclass_fields_track_fieldmaps() -> None:
    """models 的字段名单必须与 ETL FieldMap 声明一致——两处名单漂移即测试失败。"""
    meta = {
        "record_id",
        "snapshot",
        "source_row",
        "quality",
        "unit_uncertain",
        "source_flags",
        "tag_incomplete",
    }
    pairs = {
        "lv": VehicleRecord,
        "stages": StageRecord,
        "engines": EngineRecord,
    }
    for etl_key, record_cls in pairs.items():
        declared = {field.internal for field in TABLE_FIELDS[etl_key] if not field.keep_raw}
        got = {f.name for f in dataclass_fields(record_cls)} - meta
        assert got == declared, f"{etl_key}: models 与 FieldMap 名单不一致"


def test_expected_tag_kinds_cover_all_entities() -> None:
    """哨兵：新增实体表而漏配期望维度时必须响（否则 tag_incomplete 永远为空）。"""
    from aeroforge.data.db import ENTITY_TABLES

    assert set(EXPECTED_TAG_KINDS) == set(ENTITY_TABLES.values())


def test_sql_confined_to_storage_modules() -> None:
    """§7.7"禁止业务代码拼 SQL"的机检：SQL 与 sqlite3 只许出现在 db/repository。"""
    import re

    allowed = {"db.py", "repository.py"}
    source_root = Path(__file__).resolve().parents[2] / "aeroforge"
    offenders: list[str] = []
    for source_file in source_root.rglob("*.py"):
        if source_file.name in allowed and source_file.parent.name == "data":
            continue
        text = source_file.read_text(encoding="utf-8")
        if re.search(r"\b(?:import|from) sqlite3\b", text) or re.search(r"\bSELECT\b", text):
            offenders.append(str(source_file.relative_to(source_root)))
    assert offenders == [], f"以下模块私藏了 SQL / sqlite3：{offenders}"
