"""API 集成：母线段带回环、同步校验、异步构建、缓存命中、产物取用（规格 §10 / §16.3）。

用真实 ``TestClient``（含 lifespan）而非直接调函数：本层要验的正是
"路由装配 + 错误契约 + 作业线程与事件循环的配合"，这些在函数级测试里全部看不到。
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app

#: 单次构建作业的等待上限（OCCT 回转一个柱段远快于此；超时即视为挂死）。
_JOB_TIMEOUT_S = 60.0

_CYLINDER = {
    "name": "cyl",
    "base_radius": 1.0,
    "segments": [{"type": "line", "length": 3.0, "end_radius": 1.0}],
}

#: 下球底 + 柱段：自轴线起、到半径端止（穹顶段两端中恰有一端在轴线上）。
_CAPSULE = {
    "name": "capsule",
    "base_radius": 0.0,
    "segments": [
        {"type": "arc", "length": 1.0, "end_radius": 1.0},
        {"type": "line", "length": 2.0, "end_radius": 1.0},
    ],
}


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """进入 lifespan 的客户端：绑定事件循环（WS 推送依赖它），退出时收线程。"""
    with TestClient(app) as instance:
        yield instance


def _await_job(client: TestClient, job_id: str) -> dict[str, object]:
    """轮询作业直到终态（轮询即 §10.2 的降级路径，顺带把它测到）。"""
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"作业 {job_id} 在 {_JOB_TIMEOUT_S}s 内未达终态")


def test_validate_returns_samples_without_kernel(client: TestClient) -> None:
    response = client.post("/api/geometry/validate", json=_CAPSULE)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert len(body["sample"]) > 3
    assert body["envelope"] == [2.0, 2.0, 3.0]


def test_validate_rejects_degenerate_dome(client: TestClient) -> None:
    """几何不自洽必须得到 §10.3 结构（含 suggestion），而不是默认的 detail 列表。"""
    bad = {
        "name": "bad",
        "base_radius": 1.0,
        "segments": [{"type": "arc", "length": 1.0, "end_radius": 2.0}],
    }
    response = client.post("/api/geometry/validate", json=bad)

    assert response.status_code == 422
    error = response.json()["error"]
    assert set(error) >= {"code", "stage", "message", "details", "suggestion"}
    assert "恰有一端" in error["message"]
    assert error["suggestion"]


def test_contour_roundtrip_is_byte_identical(client: TestClient) -> None:
    """母线保存/重载往返一致（§16.3 验收项 2）。"""
    saved = client.post("/api/geometry/contour", json={"id": "capsule-v1", "profile": _CAPSULE})
    assert saved.status_code == 200
    canonical = saved.json()["canonical"]

    loaded = client.get("/api/geometry/contour/capsule-v1")
    assert loaded.status_code == 200
    assert loaded.json()["canonical"] == canonical
    assert loaded.json()["profile"] == saved.json()["profile"]


def test_contour_rejects_path_traversal_id(client: TestClient) -> None:
    """标识直接参与文件名拼接，故非法字符必须被拒（防目录穿越）。"""
    response = client.post("/api/geometry/contour", json={"id": "../escape", "profile": _CYLINDER})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "GEOMETRY_INVALID"


def test_missing_contour_returns_404(client: TestClient) -> None:
    response = client.get("/api/geometry/contour/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONTOUR_NOT_FOUND"


def test_build_then_cache_hit(client: TestClient) -> None:
    """未命中建作业 → 作业成功 → 再构建命中缓存（§9.3）。"""
    first = client.post("/api/geometry/build", json=_CYLINDER)
    assert first.status_code == 200
    body = first.json()
    assert body["cache_hit"] is False
    assert body["job_id"]
    key = body["key"]

    job = _await_job(client, body["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    assert job["result_key"] == key

    second = client.post("/api/geometry/build", json=_CYLINDER)
    assert second.json()["cache_hit"] is True
    assert second.json()["key"] == key
    assert second.json()["metrics"]["volume"] == pytest.approx(3.0 * 3.141592653589793, rel=1e-6)


def test_artifacts_are_served_from_whitelist(client: TestClient) -> None:
    build = client.post("/api/geometry/build", json=_CAPSULE).json()
    job = _await_job(client, build["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    key = build["key"]

    glb = client.get(f"/api/artifacts/{key}/model_lod1.glb")
    assert glb.status_code == 200
    assert glb.headers["content-type"] == "model/gltf-binary"
    assert glb.content[:4] == b"glTF"

    step = client.get(f"/api/artifacts/{key}/model.step")
    assert step.status_code == 200
    assert b"ISO-10303" in step.content

    provenance = client.get(f"/api/provenance/{key}")
    assert provenance.status_code == 200
    assert provenance.json()["key"] == key

    assert client.get(f"/api/artifacts/{key}/../secret").status_code in (404, 422)


def test_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/api/jobs/deadbeef")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_websocket_streams_until_terminal(client: TestClient) -> None:
    """WS 必须把作业推到终态后主动收线（§10.2）。"""
    profile = dict(_CAPSULE, name="ws-capsule")
    build = client.post("/api/geometry/build", json=profile).json()

    with client.websocket_connect(f"/ws/jobs/{build['job_id']}") as socket:
        while True:
            message = socket.receive_json()
            if message["status"] in ("succeeded", "failed", "cancelled"):
                break

    assert message["status"] == "succeeded", message.get("error")
    assert message["result_key"] == build["key"]
    assert message["progress"] == 1.0


def test_websocket_rejects_unknown_job(client: TestClient) -> None:
    with client.websocket_connect("/ws/jobs/not-a-job") as socket:
        message = socket.receive_json()

    assert message["code"] == "JOB_NOT_FOUND"
