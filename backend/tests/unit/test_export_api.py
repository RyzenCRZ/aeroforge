"""多格式导出与整箭数据面板（§5.8 / §10.1 / §11.5，M5 第三片）。

覆盖口径（任务交付 4）：
- falcon-9 全格式导出作业：STEP 以 ``ISO-10303-21`` 开头、STL 头 ``solid``、
  GLB magic ``glTF``、IGES 可读；报告类 JSON/CSV 内容断言（GLOW 与
  ``/api/perf/evaluate`` 同源一致）；
- §5.8 规则 4：验证失败 vehicle → STEP 作业拒绝（``EXPORT_VALIDATION_FAILED``
  + diagnostics 摘要）；网格格式仍可（作业成功附警告）；参数硬约束违反 +
  非网格格式 → API 同步 422（diagnostics 在响应）；
- provenance 完整性：格式 / 容差 / 内核版本 / SPEC_VERSION / 导出时刻；
- ``POST /api/vehicle/summary``：GLOW 与 evaluate 对拍、总高与 sections
  dimensions 对拍、CZ-5 含助推器构型的最大直径含包络；
- 作业通道：export job_id → 状态查询 → 完成 → ``/api/artifacts/{key}/{file}``
  取回 200（几何键与 ``report-`` 键都走既有通道）；
- 422 路径：未知格式枚举。
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from aeroforge import SPEC_VERSION
from aeroforge.api.main import app
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Vehicle
from aeroforge.params.templates import cz5_vehicle, falcon9_vehicle

_JOB_TIMEOUT_S = 120.0

#: STEP 文件头（ISO 10303-21 物理文件首行）。
STEP_MAGIC = b"ISO-10303-21"


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _await_job(client: TestClient, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        body = cast(dict[str, Any], client.get(f"/api/jobs/{job_id}").json())
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"作业 {job_id} 在 {_JOB_TIMEOUT_S}s 内未达终态")


def _export(client: TestClient, vehicle: Vehicle, formats: list[str]) -> Any:
    """POST /api/export 的受理（vehicle 走 model_dump 契约形态）。"""
    return client.post(
        "/api/export",
        json={"vehicle": vehicle.model_dump(mode="json"), "formats": formats},
    )


def _evaluate_glow(client: TestClient, vehicle: Vehicle) -> float:
    """evaluate 点值的 GLOW（mc=false，不投 MC 作业——对拍基准）。"""
    response = client.post(
        "/api/perf/evaluate",
        json={"vehicle": vehicle.model_dump(mode="json"), "mc": False},
    )
    assert response.status_code == 200, response.text
    point = cast(dict[str, Any], response.json()["point"])
    return cast(float, point["glow_kg"])


def _sections_total_length(client: TestClient, vehicle: Vehicle) -> float:
    response = client.post(
        "/api/geometry/sections", json={"vehicle": vehicle.model_dump(mode="json")}
    )
    assert response.status_code == 200, response.text
    dimensions = cast(dict[str, Any], response.json()["dimensions"])
    return cast(float, dimensions["total_length_m"])


# ---------------------------------------------------------------------------
# falcon-9 全格式导出：头字节 + 报告内容 + provenance
# ---------------------------------------------------------------------------


def test_falcon9_all_formats_export_headers(client: TestClient) -> None:
    """全格式导出：逐格式头字节机检（STEP/IGES/STL/GLB）+ 报告类内容。"""
    vehicle = falcon9_vehicle()
    formats = ["step", "iges", "stl", "glb", "params_json", "mass_csv", "perf_json"]
    accepted = _export(client, vehicle, formats)
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["formats"] == formats
    # files 映射：格式 → 产物文件名（命名单一事实源在后端，前端按映射下载）
    assert body["files"] == {
        "step": "export.step",
        "iges": "export.iges",
        "stl": "export.stl",
        "glb": "export.glb",
        "params_json": "export.params.json",
        "mass_csv": "export.mass.csv",
        "perf_json": "export.perf.json",
    }
    job = _await_job(client, body["job_id"])
    assert job["status"] == "succeeded", job.get("error")

    metrics = cast(dict[str, Any], job["metrics"])
    assert metrics["form"] == "export"
    assert metrics["validation_passed"] is True
    files = {item["format"]: item for item in metrics["files"]}
    assert set(files) == set(formats)

    # 逐格式经 /api/artifacts/{key}/{file} 既有通道取回并机检头字节
    for fmt in ("step", "stl", "glb", "iges"):
        item = files[fmt]
        assert item["bytes"] > 0
        response = client.get(f"/api/artifacts/{item['key']}/{item['artifact']}")
        assert response.status_code == 200, response.text
        raw = response.content
        if fmt == "step":
            assert raw.startswith(STEP_MAGIC)
        elif fmt == "stl":
            assert raw.startswith(b"solid ")
        elif fmt == "glb":
            assert raw.startswith(b"glTF")
        else:  # iges
            # IGES 起始段为 72 列空白 + 段号，不硬编码首字节——按 G/S/T 段标记机检
            text = raw.decode("ascii", errors="replace")
            assert any(line[72] == "S" for line in text.splitlines() if len(line) >= 73)

    # 报告类：几何键不含报告产物、report- 键不含几何产物（子目录同键分离）
    geometry_key = cast(str, metrics["keys"]["geometry"])
    report_key = cast(str, metrics["keys"]["report"])
    assert report_key.startswith("report-")
    assert files["step"]["key"] == geometry_key
    assert files["mass_csv"]["key"] == report_key
    missing = client.get(f"/api/artifacts/{report_key}/export.step")
    assert missing.status_code == 404


def test_falcon9_reports_content_matches_evaluate(client: TestClient) -> None:
    """报告类内容对拍：CSV GLOW 与 evaluate 同源一致；params/perf JSON 结构完整。"""
    vehicle = falcon9_vehicle()
    accepted = _export(client, vehicle, ["mass_csv", "params_json", "perf_json"])
    assert accepted.status_code == 200, accepted.text
    job = _await_job(client, accepted.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    files = {item["format"]: item for item in cast(list[dict[str, Any]], job["metrics"]["files"])}

    # mass_csv：逐级账 + total 行 GLOW 与 evaluate 对拍（同一 vehicle_ledger）
    csv_response = client.get(f"/api/artifacts/{files['mass_csv']['key']}/export.mass.csv")
    assert csv_response.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_response.text)))
    assert rows[0] == ["kind", "stage_index", "propellant_kg", "dry_kg", "glow_kg"]
    by_kind = {row[0]: row for row in rows[1:]}
    assert set(by_kind) == {"stage", "payload", "total"}
    total = by_kind["total"]
    glow_csv = float(total[4])
    glow_evaluate = _evaluate_glow(client, vehicle)
    assert glow_csv == pytest.approx(glow_evaluate, rel=1e-9)
    # 推进剂 + 干重 + 载荷 = GLOW（CSV 内部自洽）
    prop_total, dry_total = float(total[2]), float(total[3])
    assert prop_total + dry_total + vehicle.payload_mass_kg == pytest.approx(glow_csv, rel=1e-9)

    # params_json：canonical Vehicle + sourced_fields（falcon-9 命中内置模板）
    params_response = client.get(f"/api/artifacts/{files['params_json']['key']}/export.params.json")
    assert params_response.status_code == 200
    params_payload = cast(dict[str, Any], json.loads(params_response.text))
    assert params_payload["spec_version"] == SPEC_VERSION
    assert params_payload["vehicle"]["name"] == "Falcon 9"
    assert params_payload["sourced_fields"], "模板命中时 sourced_fields 必须非空"

    # perf_json：evaluate 点值输出原样（payload_by_orbit 四轨道 + glow 一致）
    perf_response = client.get(f"/api/artifacts/{files['perf_json']['key']}/export.perf.json")
    assert perf_response.status_code == 200
    perf_payload = cast(dict[str, Any], json.loads(perf_response.text))
    point = cast(dict[str, Any], perf_payload["point"])
    assert set(point["payload_by_orbit"]) == {"LEO", "SSO", "GTO", "GEO"}
    assert point["glow_kg"] == pytest.approx(glow_evaluate, rel=1e-12)


def test_export_provenance_completeness(client: TestClient) -> None:
    """§5.8 规则 3：格式 / 容差 / 内核版本 / SPEC_VERSION / 导出时刻随结果下发。"""
    vehicle = falcon9_vehicle()
    accepted = _export(client, vehicle, ["step", "stl", "glb"])
    assert accepted.status_code == 200, accepted.text
    job = _await_job(client, accepted.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    metrics = cast(dict[str, Any], job["metrics"])
    provenance = cast(dict[str, str], metrics["provenance"])

    assert provenance["spec_version"] == SPEC_VERSION
    assert "occt=" in provenance["kernel_version"] and "build123d=" in provenance["kernel_version"]
    assert provenance["exported_at"]  # ISO-8601 时间戳存在
    assert "AP214" in provenance["formats.step"]
    assert "0.001" in provenance["formats.stl"]  # 线性容差显式声明
    assert "LOD2" in provenance["formats.glb"]
    assert "§5.8 规则 4" in provenance["validation"]


# ---------------------------------------------------------------------------
# §5.8 规则 4：验证门禁的三条路径
# ---------------------------------------------------------------------------


def _bulkhead_mixed_diameter_vehicle() -> Vehicle:
    """验证失败载体：共底 + 两箱异径——约束引擎无硬违反、布局可行，
    但装配校验「容积守恒」fail（共底物理上要求两箱同径，§5.5 校验 2）。"""
    vehicle = falcon9_vehicle()
    stage1 = vehicle.stages[0]
    mixed = stage1.model_copy(
        update={
            "geometry": stage1.geometry.model_copy(
                update={
                    "common_bulkhead": True,
                    "common_bulkhead_type": "insulated_sandwich",
                    "oxidizer_tank": stage1.geometry.oxidizer_tank.model_copy(
                        update={"diameter_m": 3.0, "tank_type": "common_bulkhead"}
                    ),
                    "fuel_tank": stage1.geometry.fuel_tank.model_copy(
                        update={"tank_type": "common_bulkhead"}
                    ),
                }
            )
        }
    )
    return vehicle.model_copy(update={"stages": (mixed, vehicle.stages[1])})


def test_rule4_precise_format_rejected_by_job_with_diagnostics(client: TestClient) -> None:
    """规则 4（作业层）：装配校验 fail → 精确格式作业拒绝 + diagnostics 摘要。"""
    vehicle = _bulkhead_mixed_diameter_vehicle()
    assert not has_hard(check_vehicle(vehicle)), "夹具本身必须穿过参数硬约束"

    accepted = _export(client, vehicle, ["step"])
    assert accepted.status_code == 200, accepted.text
    job = _await_job(client, accepted.json()["job_id"])
    assert job["status"] == "failed"
    error = cast(dict[str, Any], job["error"])
    assert error["code"] == "EXPORT_VALIDATION_FAILED"
    diagnostics = cast(list[dict[str, Any]], error["details"]["diagnostics"])
    assert diagnostics, "diagnostics 摘要必须在响应中"
    assert any("容积守恒" in str(item.get("check", "")) for item in diagnostics)


def test_rule4_mesh_format_allowed_with_warning(client: TestClient) -> None:
    """规则 4（降级路径）：同参数网格格式仍可导出，作业成功但附警告。"""
    vehicle = _bulkhead_mixed_diameter_vehicle()
    accepted = _export(client, vehicle, ["stl", "glb"])
    assert accepted.status_code == 200, accepted.text
    job = _await_job(client, accepted.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    metrics = cast(dict[str, Any], job["metrics"])
    assert metrics["validation_passed"] is False
    assert metrics["warnings"], "网格降级导出必须附警告（§5.8 规则 4）"
    assert any("规则 4" in warning for warning in metrics["warnings"])
    assert {item["format"] for item in metrics["files"]} == {"stl", "glb"}


def test_rule4_hard_constraint_rejected_sync_with_diagnostics(client: TestClient) -> None:
    """规则 4（API 同步层）：参数硬约束违反 + 非网格格式 → 422 + diagnostics。"""
    vehicle = falcon9_vehicle()
    overfilled = vehicle.stages[0].model_copy(update={"fill_fraction": 1.05})
    bad = vehicle.model_copy(update={"stages": (overfilled, vehicle.stages[1])})
    assert has_hard(check_vehicle(bad))

    response = _export(client, bad, ["step", "mass_csv"])
    assert response.status_code == 422, response.text
    error = cast(dict[str, Any], response.json()["error"])
    assert error["code"] == "PARAMS_CONSTRAINT_VIOLATION"
    diagnostics = cast(list[dict[str, Any]], error["details"]["diagnostics"])
    assert diagnostics, "硬约束 diagnostics 摘要必须在响应中"

    # 同参数仅网格格式 → 放行（规则 4 字面口径：只允许网格格式并附警告）
    mesh_response = _export(client, bad, ["stl"])
    assert mesh_response.status_code == 200, mesh_response.text
    job = _await_job(client, mesh_response.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    metrics = cast(dict[str, Any], job["metrics"])
    assert metrics["warnings"]


def test_export_unknown_format_is_422(client: TestClient) -> None:
    """422 路径：未知格式枚举（请求体契约违约 → REQUEST_INVALID）。"""
    response = _export(client, falcon9_vehicle(), ["obj"])
    assert response.status_code == 422, response.text
    error = cast(dict[str, Any], response.json()["error"])
    assert error["code"] == "REQUEST_INVALID"


# ---------------------------------------------------------------------------
# POST /api/vehicle/summary —— 整箭数据面板（FR-10 / §11.5）
# ---------------------------------------------------------------------------


def test_summary_matches_evaluate_and_sections(client: TestClient) -> None:
    """面板数值对拍：GLOW 与 evaluate 一致、总高与 sections dimensions 一致。"""
    vehicle = falcon9_vehicle()
    response = client.post(
        "/api/vehicle/summary", json={"vehicle": vehicle.model_dump(mode="json")}
    )
    assert response.status_code == 200, response.text
    summary = response.json()

    glow_evaluate = _evaluate_glow(client, vehicle)
    assert summary["total_mass_kg"] == pytest.approx(glow_evaluate, rel=1e-12)
    assert summary["total_mass_t"] == pytest.approx(glow_evaluate / 1000.0, rel=1e-12)
    assert summary["total_height_m"] == pytest.approx(
        _sections_total_length(client, vehicle), rel=1e-12
    )
    # 干重 + 推进剂 + 载荷 = 总质量（面板内部自洽）
    assert summary["propellant_mass_kg"] + summary["dry_mass_kg"] + vehicle.payload_mass_kg == (
        pytest.approx(summary["total_mass_kg"], rel=1e-9)
    )
    assert summary["fairing_diameter_m"] == 5.2
    assert summary["booster_envelope_diameter_m"] is None

    # 运力摘要：目标轨道（LEO）点值 = evaluate 四轨道表同源行
    evaluate_response = client.post(
        "/api/perf/evaluate",
        json={"vehicle": vehicle.model_dump(mode="json"), "mc": False},
    )
    table = cast(dict[str, Any], evaluate_response.json()["point"]["payload_by_orbit"])
    assert summary["capacity"]["target_orbit"] == "LEO"
    assert summary["capacity"]["target_payload_kg"] == pytest.approx(
        table["LEO"]["payload_kg"], rel=1e-12
    )
    assert set(summary["capacity"]["payload_by_orbit"]) == {"LEO", "SSO", "GTO", "GEO"}


def test_summary_booster_envelope_includes_boosters(client: TestClient) -> None:
    """含助推器构型（CZ-5）：最大直径取助推器包络（> 芯级直径，OI-36 ④ 公式）。"""
    vehicle = cz5_vehicle()
    response = client.post(
        "/api/vehicle/summary", json={"vehicle": vehicle.model_dump(mode="json")}
    )
    assert response.status_code == 200, response.text
    summary = response.json()

    core_diameter = max(
        stage["diameter_m"] for stage in [s.model_dump(mode="json") for s in vehicle.stages]
    )
    fairing = vehicle.fairing_diameter_m or 0.0
    envelope = summary["booster_envelope_diameter_m"]
    assert envelope is not None
    assert envelope > max(core_diameter, fairing), "助推器包络必须大于芯级 / 整流罩直径"
    assert summary["body_max_diameter_m"] == pytest.approx(envelope, rel=1e-12)

    # 质量账含助推器（0 级段）：GLOW 仍与 evaluate 同源一致
    assert summary["total_mass_kg"] == pytest.approx(_evaluate_glow(client, vehicle), rel=1e-12)


def test_summary_mission_override_and_hard_violation(client: TestClient) -> None:
    """mission 覆写只改运力上下文（质量不变）；硬约束违反走既有错误体系 422。"""
    vehicle = falcon9_vehicle()
    mission = vehicle.mission.model_copy(update={"altitude_m": 500_000.0})
    response = client.post(
        "/api/vehicle/summary",
        json={
            "vehicle": vehicle.model_dump(mode="json"),
            "mission": mission.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["total_mass_kg"] == pytest.approx(_evaluate_glow(client, vehicle), rel=1e-12), (
        "mission 覆写不得影响质量账"
    )

    overfilled = vehicle.stages[0].model_copy(update={"fill_fraction": 1.05})
    bad = vehicle.model_copy(update={"stages": (overfilled, vehicle.stages[1])})
    bad_response = client.post(
        "/api/vehicle/summary", json={"vehicle": bad.model_dump(mode="json")}
    )
    assert bad_response.status_code == 422
    error = cast(dict[str, Any], bad_response.json()["error"])
    assert error["code"] == "PARAMS_CONSTRAINT_VIOLATION"


# ---------------------------------------------------------------------------
# 交付 3：sections 响应含 2D 剖面 PNG 渲染所需的全部数据（后端零新增端点）
# ---------------------------------------------------------------------------


def test_sections_carries_all_png_render_data(client: TestClient) -> None:
    """OI-26 2D 部分：PNG 在前端渲染（SVG→canvas），sections 数据须自足——
    分区条带（段高/中文名/液面/推进剂名/色键）+ 尺寸标注（键/文本）+ 整箭量测。"""
    response = client.post(
        "/api/geometry/sections",
        json={"vehicle": falcon9_vehicle().model_dump(mode="json")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    dimensions = cast(dict[str, Any], body["dimensions"])
    assert dimensions["total_length_m"] > 0
    assert dimensions["max_diameter_m"] > 0
    assert {"key", "text"} <= set(dimensions["labels"][0])
    stage = cast(dict[str, Any], body["stages"][0])
    band = cast(dict[str, Any], stage["bands"][0])
    assert {"section", "label_zh", "length_m"} <= set(band)
    tank_band = next(item for item in stage["bands"] if item["section"] in ("ox_tank", "fuel_tank"))
    assert tank_band["liquid_level_m"] is not None
    assert tank_band["propellant_oxidizer"] or tank_band["propellant_fuel"]
    assert tank_band["color_key"]
