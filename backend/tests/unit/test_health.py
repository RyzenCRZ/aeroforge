"""M0 冒烟：``/api/health`` 必须可用并返回版本号（规格 §16 M0 验收项）。"""

from fastapi.testclient import TestClient

from aeroforge import __version__
from aeroforge.api.main import app

client = TestClient(app)


def test_health_returns_version() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["name"] == "aeroforge"
    assert body["version"] == __version__


def test_health_matches_openapi_schema() -> None:
    """响应字段须在 OpenAPI schema 中声明，前端类型由此生成（规格 §18.2）。"""
    schema = app.openapi()
    response_schema = schema["paths"]["/api/health"]["get"]["responses"]["200"]

    assert response_schema["content"]["application/json"]["schema"]["$ref"].endswith(
        "/HealthResponse"
    )
