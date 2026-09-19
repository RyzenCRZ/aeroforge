"""母线剖面的解析解：体积 / 表面积 / 质心（规格 §5.7「解析对照」）。

**本模块只做数学，不引入任何几何内核依赖**——正是这一点使它成为内核结果的独立对照。
若两侧都走 OCCT（或都用同一条采样折线），误差趋零但检验失效。

闭式解
------
回转体体积（剖面在 z 上单调，故可用圆盘法）::

    V = π ∫ r(z)² dz

- **直线段**：``V = π·Δz·(r₀² + r₀r₁ + r₁²) / 3``
- **穹顶段**（球冠与椭圆弧**同式**）：以参数角 θ 表示 ``r = a·cosθ``、``z = z_c + b·sinθ``，得

  .. math:: V = \\pi a^2 b \\int_{\\theta_0}^{\\theta_1} \\cos^3\\theta \\, d\\theta
              = \\pi a^2 b \\big[ F(\\theta_1) - F(\\theta_0) \\big], \\quad
              F(\\theta) = \\sin\\theta - \\tfrac{1}{3}\\sin^3\\theta

  其中 ``(a, b)`` 为半轴：椭圆弧取 ``(semi_r, semi_z)``；球冠两者皆为 ρ。
  四分之一椭圆弧（θ: 0→90°）退化为 ``2/3·πa²b``，即半椭球体积。

表面积与质心用高精度数值积分（``scipy.integrate.quad``）：它们不是 M1 的门禁量，
但同样必须**独立于内核**才有意义。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.integrate import quad

from aeroforge.geometry.meridian import (
    GEOM_TOL,
    ResolvedProfile,
    SegmentGeometry,
    resolve,
)
from aeroforge.geometry.meridian import (
    MeridianProfile as MeridianProfile,
)

# 数值积分的相对/绝对容差（与我们要求的量级相比留出足够余量）
_QUAD_EPSABS = 1e-14
_QUAD_EPSREL = 1e-13
_QUAD_LIMIT = 200


@dataclass(frozen=True, slots=True)
class AnalyticMetrics:
    """解析求得的几何量（SI：m³ / m² / m）。"""

    volume: float
    surface_area: float
    centroid_z: float
    max_radius: float
    total_length: float

    @property
    def envelope(self) -> tuple[float, float, float]:
        """解析包络（包围盒尺寸，米）：``(2R_max, 2R_max, L)``。

        双通道一致性的一侧基准（§13.6 / §16.3）：前端示意网格与实际 GLB 的包围盒
        都必须与该包络一致。
        """
        return (2.0 * self.max_radius, 2.0 * self.max_radius, self.total_length)


def _dome_shape_integral(theta0_deg: float, theta1_deg: float) -> float:
    """``∫ cos³θ dθ`` 在给定角度区间上的解析值。"""

    def antiderivative(theta_deg: float) -> float:
        theta = math.radians(theta_deg)
        sin_theta = math.sin(theta)
        return sin_theta - sin_theta**3 / 3.0

    return antiderivative(theta1_deg) - antiderivative(theta0_deg)


def segment_volume(geom: SegmentGeometry) -> float:
    """单段的回转体积（闭式解，m³）。"""
    if geom.kind == "line":
        r0, z0 = geom.start
        r1, z1 = geom.end
        return math.pi * (z1 - z0) * (r0 * r0 + r0 * r1 + r1 * r1) / 3.0

    assert geom.semi_r is not None
    assert geom.semi_z is not None
    assert geom.angle_start_deg is not None
    assert geom.angle_end_deg is not None
    shape = _dome_shape_integral(geom.angle_start_deg, geom.angle_end_deg)
    return math.pi * geom.semi_r**2 * geom.semi_z * shape


def _segment_lateral_area(geom: SegmentGeometry) -> float:
    """单段侧面积 ``∫ 2π r ds``（m²）。"""
    if geom.kind == "line":
        r0, z0 = geom.start
        r1, z1 = geom.end
        slant = math.hypot(r1 - r0, z1 - z0)
        return math.pi * (r0 + r1) * slant

    assert geom.semi_r is not None
    assert geom.semi_z is not None
    assert geom.angle_start_deg is not None
    assert geom.angle_end_deg is not None
    a, b = geom.semi_r, geom.semi_z
    theta0 = math.radians(geom.angle_start_deg)
    theta1 = math.radians(geom.angle_end_deg)

    def integrand(theta: float) -> float:
        return (
            2.0
            * math.pi
            * a
            * math.cos(theta)
            * math.hypot(a * math.sin(theta), b * math.cos(theta))
        )

    value, _ = quad(
        integrand, theta0, theta1, epsabs=_QUAD_EPSABS, epsrel=_QUAD_EPSREL, limit=_QUAD_LIMIT
    )
    return float(value)


def _segment_first_moment_z(geom: SegmentGeometry) -> float:
    """单段的 ``∫ z·r² dz``（m⁴）——质心用。

    ⚠ 必须与 :func:`segment_volume` 同乘 π，否则 ``z̄ = 矩 / 体积`` 会整体差一个 π 因子
    （实测症状：柱段质心得 ``L/π`` 而非 ``L/2``）。
    """
    if geom.kind == "line":
        r0, z0 = geom.start
        r1, z1 = geom.end

        def integrand_line(z: float) -> float:
            t = (z - z0) / (z1 - z0)
            r = r0 + (r1 - r0) * t
            return z * r * r

        value, _ = quad(
            integrand_line, z0, z1, epsabs=_QUAD_EPSABS, epsrel=_QUAD_EPSREL, limit=_QUAD_LIMIT
        )
        return float(value)

    assert geom.center is not None
    assert geom.semi_r is not None
    assert geom.semi_z is not None
    assert geom.angle_start_deg is not None
    assert geom.angle_end_deg is not None
    a, b = geom.semi_r, geom.semi_z
    z_center = geom.center[1]
    theta0 = math.radians(geom.angle_start_deg)
    theta1 = math.radians(geom.angle_end_deg)

    def integrand_dome(theta: float) -> float:
        r = a * math.cos(theta)
        z = z_center + b * math.sin(theta)
        return z * r * r * b * math.cos(theta)

    value, _ = quad(
        integrand_dome, theta0, theta1, epsabs=_QUAD_EPSABS, epsrel=_QUAD_EPSREL, limit=_QUAD_LIMIT
    )
    return float(value)


def analyze(resolved: ResolvedProfile) -> AnalyticMetrics:
    """求整条剖面的解析几何量。"""
    volume = sum(segment_volume(geom) for geom in resolved.segments)
    lateral = sum(_segment_lateral_area(geom) for geom in resolved.segments)

    # 端盖：仅当该端不在轴线上时存在（落在轴线上则退化为点，无面积）
    caps = 0.0
    if not resolved.bottom_on_axis:
        caps += math.pi * resolved.profile.base_radius**2
    if not resolved.top_on_axis:
        caps += math.pi * resolved.end_radius**2

    # 矩与体积必须同乘 π（见 _segment_first_moment_z 的说明）
    moment = math.pi * sum(_segment_first_moment_z(geom) for geom in resolved.segments)
    if volume <= GEOM_TOL:
        msg = "剖面回转体积为零，无法求质心——请检查段参数"
        raise ValueError(msg)

    return AnalyticMetrics(
        volume=volume,
        surface_area=lateral + caps,
        centroid_z=moment / volume,
        max_radius=resolved.max_radius,
        total_length=resolved.total_length,
    )


def analyze_profile(profile: MeridianProfile) -> AnalyticMetrics:
    """便捷入口：直接由剖面模型求解析几何量。"""
    return analyze(resolve(profile))
