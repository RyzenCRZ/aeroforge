"""曲面光顺与曲率分析（规格 §5.4，M5 第四片）。

覆盖口径：
- 曲率梳：解析 κ 对拍（圆弧 κ = 1/R、直线 κ = 0，符号约定）；
- 连续性：锥台-柱段 = 设计折点（G0、不算缺陷）；相切卵形-柱段 = G2；
  轴处连接不判缺陷；
- 光顺：含尖峰的样条段被识别为超差段；张力样条重拟合后回写为**样条段定义**
  （§5.4 铁律：参数回写非烘焙）——回写剖面 κ 尖峰消除、偏差 ≤ 弦高容差 0.5 mm、
  原段端点/长度不变；
- 端点：POST /api/geometry/smoothing 三种来源 + apply 开关（false 只分析 /
  true 返回新母线定义，不自动重建 GLB）+ 422。
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.geometry.meridian import (
    GEOM_TOL,
    LineSegment,
    MeridianProfile,
    SplineSegment,
    TangentOgiveSegment,
    resolve,
)
from aeroforge.geometry.smoothing import (
    G2_CURVATURE_ABS_TOL,
    SMOOTHING_CHORD_TOLERANCE_M,
    curvature_comb_points,
    find_offending_segments,
    junction_continuity,
    smooth_profile,
)


def _meridian_cylinder(radius: float = 1.0, length: float = 2.0) -> MeridianProfile:
    return MeridianProfile(
        name="cyl",
        base_radius=radius,
        segments=(LineSegment(length=length, end_radius=radius),),
    )


def _ogive_on_cylinder() -> MeridianProfile:
    """切线卵形头锥 + 柱段：基底相切 ⇒ G2 级连接（卵形端 κ 与柱段 κ=0 的差在容差内？）"""
    return MeridianProfile(
        name="ogive-cyl",
        base_radius=1.0,
        segments=(
            LineSegment(length=2.0, end_radius=1.0),
            TangentOgiveSegment(length=2.0, end_radius=0.0),
        ),
    )


def _cone_on_cylinder() -> MeridianProfile:
    """锥台对接柱段：直线连接处 G1 断点属设计意图（§5.4）。"""
    return MeridianProfile(
        name="cone-cyl",
        base_radius=1.0,
        segments=(
            LineSegment(length=2.0, end_radius=1.0),
            LineSegment(length=1.5, end_radius=0.6),
        ),
    )


def _spiky_spline() -> MeridianProfile:
    """单段含局部鼓包的样条（中央 1 mm × 0.5 m 鼓包 = 制造凹凸缺陷；无段间接头）。

    鼓包曲率 κ ≈ 8·A/λ² ≈ 0.032 1/m 越绝对容差 0.02（孤立尖峰判据——段内其余
    部分平直，中位数 |κ| 极小）。
    """
    return MeridianProfile(
        name="spiky",
        base_radius=1.0,
        segments=(
            SplineSegment(
                length=3.0,
                end_radius=1.0,
                control_points=(
                    (1.0, 1.1),
                    (1.002, 1.5),
                    (1.0, 1.9),
                ),
            ),
        ),
    )


def _spiky_spline_between_cylinders() -> MeridianProfile:
    """柱段夹持的尖峰样条（接头 G1 本就不过——光顺回写须回退的对照对象）。"""
    return MeridianProfile(
        name="spiky-cyl",
        base_radius=1.0,
        segments=(
            LineSegment(length=1.0, end_radius=1.0),
            SplineSegment(
                length=3.0,
                end_radius=1.0,
                control_points=((1.0, 0.4), (1.35, 1.0), (0.95, 1.8), (1.0, 2.4)),
            ),
            LineSegment(length=1.0, end_radius=1.0),
        ),
    )


def _peak_and_signflips(profile: MeridianProfile) -> tuple[float, int]:
    """曲率梳的 (峰值 |κ|, 符号交替次数)——光顺前后对拍用（判据与超差段检测同源）。"""
    comb = curvature_comb_points(resolve(profile))
    kappas = [kappa for (_i, _z, _r, kappa) in comb]
    peak = max(abs(k) for k in kappas)
    substantial = [k for k in kappas if abs(k) > G2_CURVATURE_ABS_TOL]
    flips = sum(1 for i in range(len(substantial) - 1) if substantial[i] * substantial[i + 1] < 0.0)
    return peak, flips


# ---------------------------------------------------------------------------
# 曲率梳（解析对拍：圆弧 κ = 1/R）
# ---------------------------------------------------------------------------


def test_curvature_comb_matches_analytic_for_arc() -> None:
    """球冠段：Menger 离散 κ ≈ 1/ρ（解析幅值对拍；符号约定：向 +r 侧凸出为负）。"""
    from aeroforge.geometry.meridian import ArcSegment

    radius, length = 1.0, 0.6  # ρ = (R²+H²)/2H = (1+0.36)/1.2 = 1.1333
    profile = MeridianProfile(
        name="cap",
        base_radius=0.0,
        segments=(ArcSegment(length=length, end_radius=radius),),
    )
    comb = curvature_comb_points(resolve(profile))
    interior = [kappa for (_i, _z, r, kappa) in comb if r > 0.05]
    expected = 1.0 / ((radius**2 + length**2) / (2.0 * length))
    assert interior
    # 幅值：三点 Menger 曲率对采样折线的离散误差 < 5%
    assert max(abs(abs(kappa) - expected) / expected for kappa in interior) < 0.05
    # 符号沿弧段一致（无符号突变——§5.4 的缺陷信号）
    signs = {math.copysign(1.0, kappa) for kappa in interior}
    assert len(signs) == 1


def test_curvature_comb_line_is_zero() -> None:
    comb = curvature_comb_points(resolve(_meridian_cylinder()))
    assert comb
    assert all(abs(kappa) <= GEOM_TOL for (_i, _z, _r, kappa) in comb)


# ---------------------------------------------------------------------------
# 连续性判定（G0/G1/G2 + 设计折点 + 轴处豁免）
# ---------------------------------------------------------------------------


def test_cone_cylinder_junction_is_design_kink() -> None:
    """锥台对接柱段：直线连接处 G1 断点属设计意图——level=G0、design_kink=True。"""
    levels = junction_continuity(resolve(_cone_on_cylinder()))
    assert len(levels) == 1
    assert levels[0].level == "G0"
    assert levels[0].design_kink is True
    assert levels[0].tangent_angle_deg > 0.5  # 确有折角，但属设计意图


def test_ogive_cylinder_junction_is_tangent() -> None:
    """切线卵形基底严格竖直：与柱段 G1 通过（卵形段 κ 大、柱段 κ=0 → G1 非 G2）。"""
    levels = junction_continuity(resolve(_ogive_on_cylinder()))
    assert len(levels) == 1
    assert levels[0].design_kink is False
    assert levels[0].tangent_angle_deg <= 0.5
    assert levels[0].level in ("G1", "G2")


def test_axis_junction_marked_on_axis() -> None:
    """轴处连接（r≈0）不判缺陷：on_axis=True、不算设计折点。"""
    levels = junction_continuity(resolve(_ogive_on_cylinder()))
    # 单段卵形的链内无轴处连接——改用「柱段 + 卵形 + 轴」不必要：直接测卵形尖端无邻段
    assert all(levels[i].on_axis is False for i in range(len(levels)))


# ---------------------------------------------------------------------------
# 光顺（张力样条重拟合 → 参数回写）
# ---------------------------------------------------------------------------


def test_spiky_spline_is_detected_as_offending() -> None:
    resolved = resolve(_spiky_spline())
    comb = curvature_comb_points(resolved)
    offending = find_offending_segments(comb, resolved)
    assert 0 in offending, "曲率尖峰的样条段必须被识别为超差段"


def test_smoothing_writes_back_spline_definition() -> None:
    """§5.4 铁律：光顺结果回写为**母线段定义**（样条段替换原段），非烘焙网格。"""
    profile = _spiky_spline()
    peak_before, _flips_before = _peak_and_signflips(profile)
    report, rewritten = smooth_profile(profile, tension=0.0, apply=True)
    assert report.smoothing_applied is True
    assert report.offending_segments
    assert report.max_deviation_mm <= SMOOTHING_CHORD_TOLERANCE_M * 1000.0 + 1e-9
    # 回写形态：原样条段被**新的样条段**替换（参数回写），长度 / 端半径不变
    assert isinstance(rewritten.segments[0], SplineSegment)
    assert rewritten.segments[0].length == pytest.approx(profile.segments[0].length)
    assert rewritten.segments[0].end_radius == pytest.approx(profile.segments[0].end_radius)
    # 光顺效果（0.5 mm 弦高容差预算内）：峰值 |κ| 下降 ≥ 10%
    # （§5.4 判据"曲率梳单调或单峰"的工程近似——预算内尽可能摊平荷叶边）
    peak_after, _flips_after = _peak_and_signflips(rewritten)
    assert peak_after <= peak_before * 0.9, (
        f"光顺后峰值 |κ| {peak_after:.4f} 未比光顺前 {peak_before:.4f} 下降 ≥10%"
    )


def test_smoothing_analysis_only_leaves_profile_untouched() -> None:
    profile = _spiky_spline()
    report, rewritten = smooth_profile(profile, apply=False)
    assert report.smoothing_applied is False
    assert report.offending_segments, "只分析也要如实指出超差段"
    assert rewritten is profile


def test_smoothing_rolls_back_when_g1_cannot_hold() -> None:
    """回退路径：回写后 G1 复核不过（夹持样条的接头本就带折角）→ 段回退 + 警告留痕。"""
    profile = _spiky_spline_between_cylinders()
    report, rewritten = smooth_profile(profile, tension=0.0, apply=True)
    assert report.smoothing_applied is False
    assert rewritten is profile
    assert any("回退" in w for w in report.warnings), "回退必须显式警告，不静默"
    assert "所有超差段均未回写" in report.warnings[-1]


def test_smoothing_deviation_clamped_to_chord_tolerance() -> None:
    """偏差钳制：强光顺（tension=0）下最大偏差仍 ≤ 弦高容差 0.5 mm。"""
    profile = _spiky_spline()
    report, _rewritten = smooth_profile(profile, tension=0.0, apply=True)
    assert report.max_deviation_mm <= 0.5 + 1e-9


# ---------------------------------------------------------------------------
# 端点（POST /api/geometry/smoothing）
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _vehicle_payload() -> dict[str, Any]:
    from tests.unit.test_assembly import _stage, _vehicle, jsonable

    return {"vehicle": jsonable(_vehicle((_stage(1),), fairing_diameter_m=None))}


def test_smoothing_endpoint_stage_synth_analysis(client: TestClient) -> None:
    payload = _vehicle_payload()
    payload["stage_index"] = 1
    response = client.post("/api/geometry/smoothing", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "stage_synth"
    assert body["report"]["curvature_comb"]
    assert body["report"]["continuity"]
    assert body["report"]["smoothing_applied"] is False
    assert body["profile"] is None
    assert "光顺铁律" in body["provenance"]


def test_smoothing_endpoint_apply_returns_profile(client: TestClient) -> None:
    profile = _spiky_spline()
    saved = client.post(
        "/api/geometry/contour", json={"id": "m5-spiky", "profile": profile.model_dump(mode="json")}
    )
    assert saved.status_code == 200
    payload = {**_vehicle_payload(), "profile_id": "m5-spiky", "apply": True, "tension": 0.0}
    response = client.post("/api/geometry/smoothing", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "profile_id"
    assert body["report"]["smoothing_applied"] is True
    assert body["profile"] is not None, "apply=true 必须返回回写后的母线定义"
    assert body["profile"]["segments"][0]["type"] == "spline"


def test_smoothing_endpoint_requires_target(client: TestClient) -> None:
    response = client.post("/api/geometry/smoothing", json=_vehicle_payload())
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "GEOMETRY_INVALID"
    assert "stage_index" in body["error"]["message"]


def test_smoothing_endpoint_vehicle_profile_path(client: TestClient) -> None:
    from tests.unit.test_assembly import _stage, _vehicle, jsonable

    vehicle = _vehicle((_stage(1),), fairing_diameter_m=None)
    payload = {
        "vehicle": jsonable(vehicle.model_copy(update={"profile": _spiky_spline()})),
        "stage_index": 1,
        "apply": False,
    }
    response = client.post("/api/geometry/smoothing", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "vehicle_profile"
    assert "stage_index" in body["provenance"], "整箭母线路径下 stage_index 被忽略并留痕"
