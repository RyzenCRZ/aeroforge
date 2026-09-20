"""OI-39 型号检索与已知参数集（§7.7 仓储层 + API 层，gcat-2026Q3 真实数据）。

与 ``test_catalog_api.py`` 同一隔离纪律：真实快照**复制**进 pytest 临时区跑
ETL / 建库，库落在会话临时数据根（``data_root()`` 已被 conftest 重定向），绝不
回写 ``data/``。断言对着真实数据做——假数据造不出「Falcon 9 的起飞质量是
333.4 t（v1.0 公开口径）」这类结论，也造不出 GCAT 的真实断链样例
（Proton-K 的整流罩伪级 'GO'、New Glenn 引用的 'BE-4 v2' 无发动机记录）。

⚠ 实测口径说明：GCAT gcat-2026Q3 的 lv 名称/族/变体**全库无中文**（长征系列
记作 ``Chang Zheng``），故「中文检索命中」在本库上不成立——本文件把这一行为
也钉死（``长征`` → 空表），同时断言 ``Chang Zheng 5`` 能命中长征系列。
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.data.db import DB_NAME, build_database
from aeroforge.data.etl import run_etl
from aeroforge.data.repository import CatalogRepository
from aeroforge.paths import data_root

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_SNAPSHOT = REPO_ROOT / "data" / "snapshots" / "gcat-2026Q3"

#: 检索命中方式的排序档位（与仓储层 ``_MATCH_KIND_BY_RANK`` 同序，用于断言单调）。
_KIND_RANK = {"exact": 0, "prefix": 1, "substring": 2, "family_or_variant": 3}

#: 检索候选的字段集（API 契约形状，宁严勿松）。
_HIT_FIELDS = {"record_id", "name", "variant", "family", "country", "stage_count", "availability"}
_AVAILABILITY_FIELDS = {"glow", "length_m", "diameter_m", "payload_leo_kg"}


@pytest.fixture(scope="module")
def real_catalog(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """真实快照复制 → ETL → 建库到会话临时数据根（§13.8 隔离：不写 data/ 本体）。"""
    work = tmp_path_factory.mktemp("vehicle-search") / "gcat-2026Q3"
    shutil.copytree(REAL_SNAPSHOT, work)
    run_etl(work)
    build_database(work, data_root() / DB_NAME)
    try:
        yield
    finally:
        (data_root() / DB_NAME).unlink(missing_ok=True)  # 目录库每请求一连接，无句柄悬空


@pytest.fixture()
def repo(real_catalog: None) -> Iterator[CatalogRepository]:
    repository = CatalogRepository(data_root() / DB_NAME)
    yield repository
    repository.close()


@pytest.fixture()
def client(real_catalog: None) -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


# ---------------------------------------------------------------------------
# 仓储层：型号检索（OI-39 ②）
# ---------------------------------------------------------------------------


def test_search_by_designator_hits_long_march_family(repo: CatalogRepository) -> None:
    """``cz-5`` 走族命中（族 CZ5）：命中的是长征系列（GCAT 记作 Chang Zheng）。"""
    hits = repo.search_vehicles("cz-5")

    assert hits, "族 CZ5 在真实库中有 7 条 lv 记录，不应为空"
    assert any(hit.name.startswith("Chang Zheng") for hit in hits)
    assert any(hit.family == "CZ5" for hit in hits)


def test_search_falcon_finds_nine_and_heavy(repo: CatalogRepository) -> None:
    """``falcon`` 子串命中：Falcon 9 与 Falcon Heavy 都必须在候选里。"""
    names = {hit.name for hit in repo.search_vehicles("falcon")}

    assert "Falcon 9" in names
    assert "Falcon Heavy" in names


def test_search_empty_and_unknown_return_empty(repo: CatalogRepository) -> None:
    """空串（含纯空白）→ 空表（不是错误）；不存在的词 → 空表。"""
    assert repo.search_vehicles("") == []
    assert repo.search_vehicles("   ") == []
    assert repo.search_vehicles("zzzz不存在") == []


def test_search_normalization_ignores_case_hyphen_space(repo: CatalogRepository) -> None:
    """规范化口径（NFKC + casefold + 去空白连字符）：``FALCON-9 `` 与 ``Falcon 9`` 等值。"""
    hits = repo.search_vehicles("FALCON-9 ")

    assert hits, "规范化后应与 Falcon 9 完全匹配"
    assert hits[0].name == "Falcon 9"
    assert hits[0].match_kind == "exact"


def test_search_exact_match_sorts_before_substring(repo: CatalogRepository) -> None:
    """排序档位单调：完全匹配 > 前缀 > 子串 > 族/变体（OI-39 排序透明化）。"""
    kinds = [hit.match_kind for hit in repo.search_vehicles("falcon9")]

    assert kinds, "falcon9 应命中 Falcon 9 系列"
    ranks = [_KIND_RANK[kind] for kind in kinds]
    assert ranks == sorted(ranks), f"命中方式档位必须单调不回退：{kinds}"
    assert kinds[0] == "exact"


def test_search_chinese_name_misses_english_catalog(repo: CatalogRepository) -> None:
    """GCAT gcat-2026Q3 全库无中文：``长征`` 空表（行为钉死）；英文谱名能命中。"""
    assert repo.search_vehicles("长征") == []

    hits = repo.search_vehicles("Chang Zheng 5")
    assert hits
    assert hits[0].name == "Chang Zheng 5"
    assert hits[0].match_kind == "exact"


def test_search_hit_carries_availability_summary(repo: CatalogRepository) -> None:
    """可用性摘要只含非缺失概览字段：Falcon 9（v1.0）四项俱全，缺列不入摘要。"""
    hit = next(h for h in repo.search_vehicles("Falcon 9") if h.variant is None)

    assert hit.name == "Falcon 9"
    assert set(hit.available_fields) == {
        "launch_mass_kg",
        "length_m",
        "diameter_m",
        "payload_leo_kg",
    }
    assert hit.stage_count == 2


# ---------------------------------------------------------------------------
# 仓储层：已知参数集（OI-39 ③）——缺失引用不抛不编造
# ---------------------------------------------------------------------------


def test_record_broken_stage_link_stays_explicit(repo: CatalogRepository) -> None:
    """真实断链样例：Proton-K 的 stage_links 指向 'GO'（整流罩伪级），stages 表无此行。

    缺失引用是显式状态：``None`` 槽位 + warning，不抛异常、不编造替代级。
    """
    bundle = repo.get_vehicle_record("Proton-K")

    assert bundle is not None
    broken = [item for item in bundle.assemblies if item.stage is None]
    assert len(broken) == 1
    assert broken[0].link.stage_name == "GO"
    assert any("GO" in warning for warning in bundle.warnings)


def test_record_broken_engine_link_stays_explicit(repo: CatalogRepository) -> None:
    """真实断链样例：New Glenn 9x4 一级引用的发动机 'BE-4 v2' 不在 engines 表。"""
    bundle = repo.get_vehicle_record("New Glenn 9x4")

    assert bundle is not None
    first = bundle.assemblies[0]
    assert first.stage is not None
    assert first.stage.engine_name == "BE-4 v2"
    assert first.engine is None
    assert any("BE-4 v2" in warning for warning in bundle.warnings)


def test_record_unknown_vehicle_returns_none(repo: CatalogRepository) -> None:
    """查无此型号 → None（由调用方决定 404），不抛不猜。"""
    assert repo.get_vehicle_record("zzzz不存在") is None
    assert repo.get_vehicle_record("") is None


def test_record_falcon9_glow_matches_public_value(repo: CatalogRepository) -> None:
    """Falcon 9（v1.0）起飞质量 333.4 t：与公开资料一致（±5%），溯源锚点 lv#175。"""
    bundle = repo.get_vehicle_record("Falcon 9")

    assert bundle is not None
    assert bundle.vehicle.source_row == 175
    assert bundle.vehicle.launch_mass_kg == pytest.approx(333_400.0, rel=0.05)
    # 装配完整：两级 + 各一级发动机（Merlin 1C / Merlin 1C-Vac）
    assert [item.stage is not None for item in bundle.assemblies] == [True, True]
    assert [item.engine is not None for item in bundle.assemblies] == [True, True]


def test_record_unit_uncertain_is_per_field(repo: CatalogRepository) -> None:
    """§7.5：单位存疑按**字段**标记——Skylark L 仅起飞质量出合理区间，其余字段不受累。"""
    bundle = repo.get_vehicle_record("Skylark L")

    assert bundle is not None
    assert bundle.vehicle.unit_uncertain == "launch_mass_kg"


# ---------------------------------------------------------------------------
# API 层：两端点形状 + 404
# ---------------------------------------------------------------------------


def test_search_endpoint_shape(client: TestClient) -> None:
    """检索端点：query 回显 + 候选投影字段集（含可用性摘要逐键 bool）。"""
    response = client.get("/api/catalog/vehicles/search", params={"name": "falcon"})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == "falcon"
    assert body["hits"], "真实库上 falcon 必有命中"
    names = {hit["name"] for hit in body["hits"]}
    assert {"Falcon 9", "Falcon Heavy"} <= names
    for hit in body["hits"]:
        assert set(hit) == _HIT_FIELDS
        assert set(hit["availability"]) == _AVAILABILITY_FIELDS
        assert all(isinstance(flag, bool) for flag in hit["availability"].values())
    falcon9 = next(
        hit for hit in body["hits"] if hit["name"] == "Falcon 9" and hit["variant"] is None
    )
    assert falcon9["availability"]["glow"] is True
    assert falcon9["stage_count"] == 2


def test_search_endpoint_empty_name_returns_empty_hits(client: TestClient) -> None:
    """空检索词 → 200 + 空表（「还没打完」不是错误）。"""
    response = client.get("/api/catalog/vehicles/search", params={"name": ""})

    assert response.status_code == 200
    body = response.json()
    assert body["query"] == ""
    assert body["hits"] == []


def test_record_endpoint_carries_sources_missing_and_notes(client: TestClient) -> None:
    """参数集端点：逐字段出处锚点、缺失清单与 null 字段机械一致、发动机口径注记。"""
    response = client.get("/api/catalog/vehicles/record", params={"name": "Falcon 9"})

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Falcon 9"
    assert body["variant"] is None
    assert body["quality"] == "literature"
    assert body["snapshot"]["id"] == "gcat-2026Q3"

    # 出处锚点：lv 行 / st 行 / en 行各按表前缀 + 源行号
    vehicle = body["vehicle"]
    assert all("lv#" in entry["source"] for entry in vehicle.values())
    assert vehicle["launch_mass_kg"]["value"] == pytest.approx(333_400.0)
    assert vehicle["launch_mass_kg"]["source"].endswith("lv#175")
    assert len(body["stages"]) == 2
    for assembly in body["stages"]:
        assert assembly["record"] is not None
        assert all("st#" in entry["source"] for entry in assembly["record"].values())
    assert body["engines"], "两级各引用一台发动机，engines 不应为空"
    for engine in body["engines"]:
        assert all("en#" in entry["source"] for entry in engine.values())
        assert "真空口径（OI-35）" in engine["isp_vacuum_s"]["source"]
        assert "环境未声明" in engine["typical_thrust_n"]["source"]

    # 缺失一致性：值为 null 的字段必在 missing 里（不编造的机械保证）。
    # engine 字段在 missing 里以 stages[i].engine.<field> 登记（engines[] 是去重列表，
    # 下标与装配序不对应），故按字段名跨装配核对。
    null_paths = [f"vehicle.{field}" for field, entry in vehicle.items() if entry["value"] is None]
    for index, assembly in enumerate(body["stages"]):
        record = assembly["record"]
        assert record is not None
        null_paths += [
            f"stages[{index}].{field}" for field, entry in record.items() if entry["value"] is None
        ]
    for engine in body["engines"]:
        for field, entry in engine.items():
            if entry["value"] is None:
                assert any(path.endswith(f".engine.{field}") for path in body["missing"]), (
                    f"engine 字段 {field} 为 null，但 missing 里没有对应的 stages[i].engine.{field}"
                )
    assert null_paths, "Falcon 9 v1.0 在 GCAT 里有真实缺口（如变体），missing 不应为空"
    assert set(null_paths) <= set(body["missing"])
    assert body["reference_only"] is False


def test_record_endpoint_reports_reference_only_for_sparse_records(client: TestClient) -> None:
    """GCAT 以 0 填未知的「?」记录：可用字段不足一半 → reference_only=true。"""
    response = client.get("/api/catalog/vehicles/record", params={"name": "?"})

    assert response.status_code == 200
    body = response.json()
    assert body["reference_only"] is True
    assert body["missing"]


def test_record_endpoint_passes_through_missing_reference(client: TestClient) -> None:
    """断链透传：Proton-K 的 'GO' 缺失引用同时出现在 missing 与 warnings（不吞不掉）。"""
    response = client.get("/api/catalog/vehicles/record", params={"name": "Proton-K"})

    assert response.status_code == 200
    body = response.json()
    broken = [item for item in body["stages"] if item["missing_reference"]]
    assert len(broken) == 1
    assert broken[0]["record"] is None
    assert any("GO" in entry for entry in body["missing"])
    assert any("GO" in warning for warning in body["warnings"])


def test_record_endpoint_unit_uncertain_is_per_field(client: TestClient) -> None:
    """单位存疑逐字段下发：Skylark L 只有 launch_mass_kg 带标记。"""
    body = client.get("/api/catalog/vehicles/record", params={"name": "Skylark L"}).json()

    assert body["vehicle"]["launch_mass_kg"]["unit_uncertain"] is True
    assert body["vehicle"]["length_m"]["unit_uncertain"] is False


def test_record_endpoint_unknown_vehicle_returns_404(client: TestClient) -> None:
    """查无此型号 → 404 + CATALOG_NOT_FOUND + 可操作建议（§10.3）。"""
    response = client.get("/api/catalog/vehicles/record", params={"name": "zzzz不存在"})

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "CATALOG_NOT_FOUND"
    assert error["suggestion"]
