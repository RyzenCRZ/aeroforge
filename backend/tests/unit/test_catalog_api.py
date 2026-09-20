"""目录域端点（§7.4 / §10.1）：材料库查询 + 发动机族谱。

engines 端点用**真实 GCAT 数据**（仓库 ``data/snapshots/gcat-2026Q3``）建库后
走真实 HTTP——族谱的"族清单非空、计数正确"必须对着真实数据断言，假数据造不出
这层结论。隔离方式与 §13.8 一致：把快照**复制**进 pytest 临时区再跑 ETL / 建库
（快照本体只读，绝不回写 ``data/``），库落在会话级临时数据根里，用完即删。
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
from aeroforge.paths import data_root

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_SNAPSHOT = REPO_ROOT / "data" / "snapshots" / "gcat-2026Q3"

#: §7.4 点名的结构性缺失清单（§7.9 手动补录缺口，missing_fields 恒含）。
_MISSING_FIELDS = [
    "cycle",
    "chamber_pressure_pa",
    "expansion_ratio",
    "thrust_sea_level_n",
    "isp_sea_level_s",
]

#: EngineRecordOut 的字段集——**不含任何派生字段**（推重比不派生，OI-35）。
_ENGINE_RECORD_FIELDS = {
    "record_id",
    "name",
    "manufacturer",
    "family",
    "oxidizer",
    "fuel",
    "loaded_mass_kg",
    "total_impulse_ns",
    "typical_thrust_n",
    "isp_vacuum_s",
    "burn_duration_s",
    "first_flight",
    "usage_notes",
    "quality",
}

_MATERIAL_FIELDS = {
    "id",
    "name",
    "category",
    "density_kg_m3",
    "elastic_modulus_pa",
    "yield_strength_pa",
    "service_temp_min_c",
    "service_temp_max_c",
    "typical_min_wall_thickness_m",
    "heat_treatment",
    "source",
    "quality",
    "specific_strength_m2_s2",
    "specific_stiffness_m2_s2",
}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


@pytest.fixture(scope="module")
def real_catalog(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """真实快照复制 → ETL → 建库到会话临时数据根（§13.8 隔离：不写 data/ 本体）。"""
    work = tmp_path_factory.mktemp("catalog") / "gcat-2026Q3"
    shutil.copytree(REAL_SNAPSHOT, work)
    run_etl(work)  # 幂等覆盖副本内的 records.parquet（含 first_flight / usage_notes）
    build_database(work, data_root() / DB_NAME)
    try:
        yield
    finally:
        (data_root() / DB_NAME).unlink(missing_ok=True)  # 目录库每请求一连接，无句柄悬空


# ---------------------------------------------------------------------------
# GET /api/catalog/materials
# ---------------------------------------------------------------------------


def test_materials_endpoint_returns_the_full_library(client: TestClient) -> None:
    response = client.get("/api/catalog/materials")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"materials"}
    materials = body["materials"]
    assert len(materials) == 12
    for item in materials:
        assert set(item) == _MATERIAL_FIELDS
        assert item["quality"] in ("literature", "typical")
        if item["quality"] == "literature":
            assert item["source"], "literature 条目必须携带出处（溯源红线）"


def test_materials_endpoint_derives_specific_strength_and_stiffness(
    client: TestClient,
) -> None:
    """比强度 / 比刚度是**端点派生值**（不存储，P1）：σ_y/ρ 与 E/ρ 逐条对表。"""
    body = client.get("/api/catalog/materials").json()
    by_id = {item["id"]: item for item in body["materials"]}

    al = by_id["al-2219"]
    assert al["specific_strength_m2_s2"] == pytest.approx(290e6 / 2840.0)
    assert al["specific_stiffness_m2_s2"] == pytest.approx(73e9 / 2840.0)

    # 跨类别抽查一条：Ti-6Al-4V 的比强度应显著高于铝合金（选型对比的基本盘）
    ti = by_id["ti-6al-4v"]
    assert ti["specific_strength_m2_s2"] == pytest.approx(880e6 / 4430.0)
    assert ti["specific_strength_m2_s2"] > al["specific_strength_m2_s2"]


# ---------------------------------------------------------------------------
# GET /api/catalog/engines
# ---------------------------------------------------------------------------


def test_engines_without_family_lists_real_families_with_counts(
    client: TestClient, real_catalog: None
) -> None:
    """缺省形态：族清单 + 计数（真实 GCAT 库，断言非空——空库是缺陷不是常态）。"""
    response = client.get("/api/catalog/engines")

    assert response.status_code == 200
    body = response.json()
    assert body["family"] is None
    assert body["engines"] == []
    assert body["families"], "真实 GCAT 库的族清单不应为空"
    for item in body["families"]:
        assert set(item) == {"family", "count"}
        assert isinstance(item["count"], int) and item["count"] >= 1
    # gcat-2026Q3 实测：1488 台发动机中 648 台有族（约 56% 无族，如实排除不发明桶）
    total = sum(item["count"] for item in body["families"])
    assert total == 648


def test_engines_with_family_returns_records_and_constant_missing_fields(
    client: TestClient, real_catalog: None
) -> None:
    """按族查询：记录 + missing_fields 恒在（§7.4：结构性缺口显式列出，禁止编造）。"""
    listed = client.get("/api/catalog/engines").json()["families"]
    family = listed[0]["family"]

    response = client.get("/api/catalog/engines", params={"family": family})

    assert response.status_code == 200
    body = response.json()
    assert body["family"] == family
    assert body["missing_fields"] == _MISSING_FIELDS
    assert body["snapshot"]["id"] == "gcat-2026Q3"
    assert body["engines"], "清单首族必有记录"
    for record in body["engines"]:
        assert set(record) == _ENGINE_RECORD_FIELDS
        assert record["family"] == family
        assert record["quality"] == "literature"  # §7.6：GCAT 全库 literature
    # 计数与缺省形态一致：清单里该族的 count == 该族记录数
    expected = next(item for item in listed if item["family"] == family)["count"]
    assert len(body["engines"]) == expected


def test_engines_records_carry_first_flight_and_usage_notes_as_text(
    client: TestClient, real_catalog: None
) -> None:
    """§7.4 族谱口径：Date / Usage 由 ETL 补映射，TEXT 原样（含可信度痕迹）。"""
    response = client.get("/api/catalog/engines", params={"family": "RD-170"})

    assert response.status_code == 200
    records = response.json()["engines"]
    assert records, "RD-170 族在真实库中有记录"
    rd180 = next(record for record in records if record["name"] == "RD-180")
    assert rd180["first_flight"] == "2000 May 24"  # GCAT Date 列原样（非纯年份）
    assert rd180["usage_notes"] == "Atlas 3 [1]"  # GCAT Usage 列原样（含 [1] 溯源痕迹）
    assert rd180["isp_vacuum_s"] == pytest.approx(337.8)


def test_typical_thrust_n_participates_in_no_derived_field(
    client: TestClient, real_catalog: None
) -> None:
    """OI-35：``typical_thrust_n`` 环境未声明——响应里不得存在任何由它派生的字段
    （推重比 / 海平面或真空换算 / 单位归一），它只作为原样数值下发。"""
    body = client.get("/api/catalog/engines", params={"family": "RD-170"}).json()

    for record in body["engines"]:
        assert set(record) == _ENGINE_RECORD_FIELDS
    banned = ("thrust_to_weight", "twr", "thrust_sea_level_n", "thrust_vacuum_n", "isp_sea_level_s")
    assert all(field not in record for record in body["engines"] for field in banned)


def test_engines_with_unknown_family_returns_an_empty_list(
    client: TestClient, real_catalog: None
) -> None:
    """无此族：空记录表 + 回显族名（不报错——清单就在缺省形态里，客户端可校验）。"""
    response = client.get("/api/catalog/engines", params={"family": "no-such-family"})

    assert response.status_code == 200
    body = response.json()
    assert body["family"] == "no-such-family"
    assert body["engines"] == []
    assert body["missing_fields"] == _MISSING_FIELDS
