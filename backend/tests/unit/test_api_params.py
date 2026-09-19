"""API 集成：``POST /api/params/diagnose``（规格 §6.3 / §6.5 / §10.1 / §10.3）。

用真实 ``TestClient``（含 lifespan）而不是直接调函数：本层要验的正是
"路由装配 + 契约形状 + 错误处理分支"，这些在函数级测试里全看不到。特别是
**结构错那条通路**——FastAPI 抛的是 ``RequestValidationError``（不是 pydantic 的
``ValidationError``），两条通路各自翻译一次，很容易只对了一条。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.params.diagnostics import CODE_DV_ALLOCATION
from aeroforge.params.schema import Vehicle

#: §6.5 规则条数（7 条表内规则 + §6.3 举例的喷管出口直径）。
_RULE_COUNT = 8


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """进入 lifespan 的客户端（与几何 API 的用法一致）。"""
    with TestClient(app) as instance:
        yield instance


def _payload(
    vehicle: Vehicle, mutate: Callable[[dict[str, Any]], None] | None = None
) -> dict[str, Any]:
    payload = vehicle.model_dump(mode="json")
    if mutate is not None:
        mutate(payload)
    return payload


def test_diagnose_returns_constraints_and_rules(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    response = client.post(
        "/api/params/diagnose",
        json=_payload(single_stage_vehicle, lambda p: p.update({"fairing_diameter_m": 2.5})),
    )

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"constraints", "diagnostics", "rules"}

    # 进入 200 的约束裁定必为非硬：硬违反一律走 422（§6.3 的「处理」列）
    assert body["constraints"], "用例本身没造出警告"
    assert {item["level"] for item in body["constraints"]} == {"warning"}

    assert len(body["rules"]) == _RULE_COUNT
    for rule in body["rules"]:
        assert set(rule) >= {"code", "title", "level", "threshold", "source"}
        assert rule["threshold"].strip() and rule["source"].strip()


def test_diagnose_reports_deferred_rules_in_the_response(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """未判定的规则必须随响应下发——否则前端会把"没判"显示成"没问题"。"""
    response = client.post("/api/params/diagnose", json=_payload(single_stage_vehicle))

    assert response.status_code == 200
    deferred = {rule["code"]: rule["deferred_reason"] for rule in response.json()["rules"]}

    assert CODE_DV_ALLOCATION in deferred
    assert deferred[CODE_DV_ALLOCATION], "未判定的规则没写明原因"
    assert deferred["TWR_TOO_LOW"], "缺 M4 的推进剂质量时推重比不得判定"


def test_diagnose_rejects_a_hard_violation(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """硬约束违反 → 422 + §10.3 结构，且 ``details.diagnostics`` 是**六字段**清单。"""
    response = client.post(
        "/api/params/diagnose",
        json=_payload(
            single_stage_vehicle, lambda p: p["stages"][0].update({"fill_fraction": 1.5})
        ),
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "PARAMS_CONSTRAINT_VIOLATION"
    assert error["stage"] == "params"
    assert error["suggestion"]

    diagnostics = error["details"]["diagnostics"]
    assert [item["code"] for item in diagnostics] == ["HARD_FILL_OVERFILL"]
    assert diagnostics[0]["level"] == "hard"
    assert diagnostics[0]["field_path"] == "stages[0].fill_fraction"
    assert set(diagnostics[0]) == {"level", "code", "field_path", "message", "suggestion"}


def test_diagnose_rejects_a_structural_error(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """结构错走另一条通路（``RequestValidationError``），形状必须与上面完全一致。"""
    response = client.post(
        "/api/params/diagnose",
        json=_payload(
            single_stage_vehicle,
            lambda p: p["stages"][0]["engine"].update({"mixture_ratio": -1.0}),
        ),
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "REQUEST_INVALID"
    assert error["stage"] == "params"

    diagnostics = error["details"]["diagnostics"]
    assert len(diagnostics) == 1, f"嵌套字段错不得连带出容器级错误：{diagnostics}"
    assert diagnostics[0]["field_path"] == "stages[0].engine.mixture_ratio"
    assert "> 0.0" in diagnostics[0]["suggestion"]


def test_diagnose_empty_stages_yields_one_clear_error(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """真正的空级列表必须报出来——且只报这一条（与上面那条互为对照）。"""
    response = client.post(
        "/api/params/diagnose",
        json=_payload(single_stage_vehicle, lambda p: p.update({"stages": []})),
    )

    assert response.status_code == 422
    diagnostics = response.json()["error"]["details"]["diagnostics"]

    assert [item["code"] for item in diagnostics] == ["PARAMS_TOO_SHORT"]
    assert diagnostics[0]["field_path"] == "stages"


def test_diagnose_scales_with_the_stage_count(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """两级构型下规则集条数不变（规则是**整箭**一次），逐级判定体现在裁定里。"""
    response = client.post("/api/params/diagnose", json=_payload(two_stage_vehicle))

    assert response.status_code == 200
    body = response.json()
    assert len(body["rules"]) == _RULE_COUNT

    length_rule = next(
        rule for rule in body["rules"] if rule["code"] == "LENGTH_TO_DIAMETER_OUT_OF_RANGE"
    )
    assert [item["field_path"] for item in length_rule["diagnostics"]] == ["stages[1].length_m"]


def test_endpoint_is_documented_in_the_openapi_contract(client: TestClient) -> None:
    """端点与六字段裁定必须出现在契约里：前端类型由 ``npm run gen:api`` 生成。"""
    spec = client.get("/openapi.json").json()

    assert "/api/params/diagnose" in spec["paths"]
    diagnostic = spec["components"]["schemas"]["Diagnostic"]
    assert set(diagnostic["required"]) == {
        "level",
        "code",
        "field_path",
        "message",
        "suggestion",
    }
    assert "impact" not in diagnostic["properties"]
