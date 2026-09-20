"""M5 曲线族六种（§5.3）+ G0/G1 生成期门禁的单元测试。

覆盖口径（任务交付 1 / 交付 6）：
- 六族各一组：参数域校验（负半径 / 指数越界 / 穹顶放置 / 钟形自洽）、
  采样连续性（z 单调、端点精确、半径非负）、**解析 vs 内核对拍**（§5.7——
  解析侧参数式 quad 积分，内核侧 B 样条构边，两侧算法无关）。
- 公开量级锚：冯·卡门（LDHV）在 1.0 长径比下的容积形态介于锥与半椭球之间
  （钝头、低阻形体的定性判据）；钟形喷管 80% 钟长的 Rao 参数量级
  （L = 0.8·(Rₑ−Rₜ)/tan15°、壁角 θₙ/θₑ 落在 Rao 图表量级带内、型面外凸）。
- G0/G1：可见折痕在**生成期**报错并指段索引；合法相切通过；轴处 pinch 与
  退化段仍走报告级裁定（不拦）。
"""

from __future__ import annotations

import itertools
import math

import pytest

from aeroforge.geometry.analytic import analyze
from aeroforge.geometry.meridian import (
    BellNozzleSegment,
    LineSegment,
    MeridianError,
    MeridianProfile,
    ParabolaSegment,
    PowerLawSegment,
    SplineSegment,
    TangentOgiveSegment,
    VonKarmanSegment,
    curve_point,
    resolve,
)
from aeroforge.geometry.revolve import build_solid, measure, relative_error

#: §5.7 解析对照门禁（既有口径：< 0.1%）。
ANALYTIC_GATE = 1e-3


def _kernel_vs_analytic(profile: MeridianProfile) -> tuple[float, float]:
    kernel = measure(build_solid(profile)).volume
    analytic = analyze(resolve(profile)).volume
    return kernel, relative_error(kernel, analytic)


def _ogive_profile(length: float, radius: float) -> MeridianProfile:
    """柱 + 卵形头锥（尖在轴上：柱段在上，头锥正向收尖到顶）。"""
    return MeridianProfile(
        name="ogive-case",
        base_radius=radius,
        segments=(
            LineSegment(length=2.0, end_radius=radius),
            TangentOgiveSegment(length=length, end_radius=0.0),
        ),
    )


class TestTangentOgive:
    def test_base_tangent_vertical_g1_with_cylinder(self) -> None:
        """切线卵形的定义性性质：基底切向竖直 ⇒ 与柱段 G1（夹角 0°）。"""
        profile = _ogive_profile(3.0, 1.0)
        resolved = resolve(profile)
        ogive = resolved.segments[1]
        assert ogive.kind == "ogive"
        # 基底（链起点）切向严格竖直——"切线"卵形与柱段相切的工程定义
        assert ogive.start_tangent == pytest.approx((0.0, 1.0), abs=1e-12)
        assert ogive.curve is not None
        # 卵形圆半径 ρ = (R²+L²)/(2R)（工程公式锚，进求值规格）
        rho = (1.0**2 + 3.0**2) / (2.0 * 1.0)
        assert ogive.curve.params[3] == pytest.approx(rho, rel=1e-12)

    def test_kernel_vs_analytic(self) -> None:
        kernel, error = _kernel_vs_analytic(_ogive_profile(3.0, 1.0))
        assert kernel > 0.0
        assert error < ANALYTIC_GATE, f"解析对照超差 {error:.3e}"

    def test_blunt_ogive_rejected(self) -> None:
        """L < R 的过钝卵形会破坏 z 单调——域错误。"""
        with pytest.raises(MeridianError, match="长径比过钝"):
            resolve(_ogive_profile(0.5, 1.0))

    def test_dome_placement_required(self) -> None:
        with pytest.raises(MeridianError, match="恰有一端"):
            resolve(
                MeridianProfile(
                    name="bad-ogive",
                    base_radius=1.0,
                    segments=(TangentOgiveSegment(length=2.0, end_radius=0.8),),
                )
            )

    def test_reverse_direction(self) -> None:
        """反向放置（尖在轴上起步）：与正向同体积。"""
        forward = MeridianProfile(
            name="ogive-fwd",
            base_radius=0.0,
            segments=(TangentOgiveSegment(length=3.0, end_radius=1.0),),
        )
        reverse = MeridianProfile(
            name="ogive-rev",
            base_radius=1.0,
            segments=(TangentOgiveSegment(length=3.0, end_radius=0.0),),
        )
        assert measure(build_solid(forward)).volume == pytest.approx(
            measure(build_solid(reverse)).volume, rel=1e-12
        )


class TestParabola:
    def test_kernel_vs_analytic_and_tangent(self) -> None:
        profile = MeridianProfile(
            name="parabola-case",
            base_radius=0.0,
            segments=(ParabolaSegment(length=2.5, end_radius=1.0, coefficient=1.0),),
        )
        resolved = resolve(profile)
        # K=1：基底切向竖直（与柱段 G1 的工程性质）
        assert resolved.segments[0].end_tangent == pytest.approx((0.0, 1.0), abs=1e-12)
        kernel, error = _kernel_vs_analytic(profile)
        assert kernel > 0.0
        assert error < ANALYTIC_GATE

    def test_coefficient_domain(self) -> None:
        with pytest.raises(ValueError, match="coefficient"):
            ParabolaSegment(length=2.0, end_radius=1.0, coefficient=1.5)


class TestVonKarman:
    def test_kernel_vs_analytic(self) -> None:
        profile = MeridianProfile(
            name="vk-case",
            base_radius=0.0,
            segments=(VonKarmanSegment(length=2.0, end_radius=1.0),),
        )
        _kernel, error = _kernel_vs_analytic(profile)
        assert error < ANALYTIC_GATE

    def test_ldhv_volume_between_cone_and_ellipsoid(self) -> None:
        """公开形态锚（定性）：1.0 长径比下 LDHV 比 锥 满、比 半椭球 瘦。

        冯·卡门（LDHV）是给定长径比下的低阻钝头形体——容积必然大于同尺寸锥
        （尖头），小于半椭球（最钝）。阻力系数形态的定性对拍即锚定此序。
        """
        length, radius = 2.0, 1.0  # 长径比 = L/(2R) = 1.0
        cone = MeridianProfile(
            name="cone-ref",
            base_radius=radius,
            segments=(LineSegment(length=length, end_radius=0.0),),
        )
        von_karman = MeridianProfile(
            name="vk-ref",
            base_radius=radius,
            segments=(VonKarmanSegment(length=length, end_radius=0.0),),
        )
        v_cone = measure(build_solid(cone)).volume
        v_vk = measure(build_solid(von_karman)).volume
        # 半椭球（半轴 R × R × L）解析：(2/3)πR²L
        v_ellipsoid = 2.0 / 3.0 * math.pi * radius**2 * length
        assert v_cone < v_vk < v_ellipsoid, (
            f"LDHV 容积序破坏：锥 {v_cone:.6f} < VK {v_vk:.6f} < 半椭球 {v_ellipsoid:.6f}"
            "——钝头低阻形体的公开形态锚（定性）"
        )

    def test_tip_tangent_horizontal(self) -> None:
        profile = MeridianProfile(
            name="vk-tip",
            base_radius=0.0,
            segments=(VonKarmanSegment(length=2.0, end_radius=1.0),),
        )
        resolved = resolve(profile)
        assert resolved.segments[0].start_tangent == pytest.approx((1.0, 0.0), abs=1e-9)


class TestPowerLaw:
    def test_kernel_vs_analytic(self) -> None:
        profile = MeridianProfile(
            name="power-case",
            base_radius=1.0,
            segments=(PowerLawSegment(length=2.0, end_radius=0.4, exponent=1.6),),
        )
        _kernel, error = _kernel_vs_analytic(profile)
        assert error < ANALYTIC_GATE

    def test_exponent_domain(self) -> None:
        with pytest.raises(ValueError, match="exponent"):
            PowerLawSegment(length=2.0, end_radius=0.4, exponent=11.0)

    def test_exponent_one_is_line(self) -> None:
        """n = 1 退化为直线（锥台）：体积与 line 段解析式一致。"""
        profile = MeridianProfile(
            name="power-line",
            base_radius=1.0,
            segments=(PowerLawSegment(length=2.0, end_radius=0.4, exponent=1.0),),
        )
        r0, r1, length = 1.0, 0.4, 2.0
        expected = math.pi * length * (r0 * r0 + r0 * r1 + r1 * r1) / 3.0
        assert measure(build_solid(profile)).volume == pytest.approx(expected, rel=1e-9)


class TestBellNozzle:
    def _bell_length(self, throat: float, exit_r: float, ratio: float) -> float:
        return ratio * (exit_r - throat) / math.tan(math.radians(15.0))

    def test_eighty_percent_rao_magnitudes(self) -> None:
        """80% 钟形长度 Rao 参数量级（任务锚）：

        - 长度 = ratio·(Rₑ−Rₜ)/tan15°（80% 钟形定义式）；
        - 壁角 θₙ / θₑ 落在 Rao 图表量级带（ε≈11 → θₙ≈29–30°、θₑ≈9–10°）；
        - 型面外凸（半径沿流向单调增）、喉部壁面与轴平行。
        """
        throat, exit_r, ratio = 0.3, 1.0, 0.8
        profile = MeridianProfile(
            name="bell-80",
            base_radius=exit_r,
            segments=(
                BellNozzleSegment(
                    length=self._bell_length(throat, exit_r, ratio),
                    end_radius=throat,
                    throat_radius=throat,
                    length_ratio=ratio,
                ),
                LineSegment(length=1.0, end_radius=throat),
            ),
        )
        resolved = resolve(profile)
        bell = resolved.segments[0]
        assert bell.kind == "bell"
        # 喉部壁面与轴平行（链内倒放：喉部是段终点，切向仍竖直）
        assert bell.end_tangent == pytest.approx((0.0, 1.0), abs=1e-9)
        # 壁角量级带（对数拟合 vs Rao 图表）
        expansion = (exit_r / throat) ** 2
        from aeroforge.geometry.meridian import _bell_wall_angles_deg

        theta_n, theta_e = _bell_wall_angles_deg(expansion)
        assert 25.0 <= theta_n <= 35.0, f"θₙ={theta_n:.2f}° 超出 Rao 量级带"
        assert 7.0 <= theta_e <= 13.0, f"θₑ={theta_e:.2f}° 超出 Rao 量级带"
        # 型面单调外凸：半径沿链推进（喉部方向）递减即钟形张开
        points = [r for r, _ in bell.points or ()]
        assert all(a >= b - 1e-12 for a, b in itertools.pairwise(points))
        _kernel, error = _kernel_vs_analytic(profile)
        assert error < ANALYTIC_GATE

    def test_length_ratio_consistency_enforced(self) -> None:
        with pytest.raises(MeridianError, match="不自洽"):
            resolve(
                MeridianProfile(
                    name="bell-bad-ratio",
                    base_radius=1.0,
                    segments=(
                        BellNozzleSegment(
                            length=1.2,
                            end_radius=0.3,
                            throat_radius=0.3,
                            length_ratio=0.8,
                        ),
                    ),
                )
            )

    def test_throat_must_be_one_end(self) -> None:
        with pytest.raises(MeridianError, match="喉部"):
            resolve(
                MeridianProfile(
                    name="bell-no-throat",
                    base_radius=0.9,
                    segments=(
                        BellNozzleSegment(
                            length=self._bell_length(0.3, 1.0, 0.8),
                            end_radius=0.7,
                            throat_radius=0.3,
                            length_ratio=0.8,
                        ),
                    ),
                )
            )

    def test_exit_must_exceed_throat(self) -> None:
        """出口 ≤ 喉部（收缩"喷管"）：域错误（长度给正但出口更小）。"""
        with pytest.raises(MeridianError, match="出口半径"):
            resolve(
                MeridianProfile(
                    name="bell-inverted",
                    base_radius=0.25,
                    segments=(
                        BellNozzleSegment(
                            length=1.0,
                            end_radius=0.3,
                            throat_radius=0.3,
                            length_ratio=0.8,
                        ),
                    ),
                )
            )

    def test_length_ratio_domain(self) -> None:
        with pytest.raises(ValueError, match="length_ratio"):
            BellNozzleSegment(length=1.0, end_radius=0.3, throat_radius=0.3, length_ratio=1.6)


class TestSpline:
    def test_kernel_vs_analytic_and_control_points(self) -> None:
        control = ((0.95, 0.6), (0.8, 1.2), (0.62, 1.6))
        length = 2.0
        profile = MeridianProfile(
            name="spline-case",
            base_radius=1.0,
            segments=(SplineSegment(length=length, end_radius=0.5, control_points=control),),
        )
        resolved = resolve(profile)
        spline = resolved.segments[0]
        assert spline.kind == "spline"
        # 插值样条的定义性质：求值器在控制点 z 处精确返回控制点 r
        assert spline.curve is not None
        for radius, z in control:
            evaluated_r, evaluated_z = curve_point(spline.curve, z / length)
            assert evaluated_r == pytest.approx(radius, abs=1e-9), (
                f"样条在 z={z} 处得 r={evaluated_r:.9f}，应为控制点 {radius}"
            )
            assert evaluated_z == pytest.approx(z, abs=1e-12)
        # 采样 z 单调、端点精确
        points = spline.points
        assert points is not None
        assert points[0] == pytest.approx((1.0, 0.0), abs=1e-12)
        assert points[-1] == pytest.approx((0.5, length), abs=1e-12)
        _kernel, error = _kernel_vs_analytic(profile)
        assert error < ANALYTIC_GATE

    def test_control_point_domain(self) -> None:
        with pytest.raises(ValueError, match="严格递增"):
            SplineSegment(
                length=2.0,
                end_radius=0.5,
                control_points=((0.9, 1.2), (0.8, 0.6)),
            )
        with pytest.raises(ValueError, match="半径为负"):
            SplineSegment(
                length=2.0,
                end_radius=0.5,
                control_points=((-0.1, 0.6),),
            )


class TestG1Gate:
    """§5.3：违反 G1 的连接点在生成阶段即报错并指出段索引。"""

    def _crease(self) -> MeridianProfile:
        """柱段后接斜锥：接头 r=1.0 处切向夹角约 26.6°——可见折痕。"""
        return MeridianProfile(
            name="crease",
            base_radius=1.0,
            segments=(
                LineSegment(length=3.0, end_radius=1.0),
                LineSegment(length=1.0, end_radius=1.5),
            ),
        )

    def test_crease_raises_at_build_with_segment_index(self) -> None:
        with pytest.raises(MeridianError, match=r"段 0.*段 1") as excinfo:
            build_solid(self._crease())
        assert "26" in str(excinfo.value) or "夹角" in str(excinfo.value)

    def test_tangent_case_passes(self) -> None:
        """合法相切（柱 + K=1 抛物线头锥的基底接口）通过生成。"""
        profile = MeridianProfile(
            name="tangent-ok",
            base_radius=1.0,
            segments=(
                LineSegment(length=1.0, end_radius=1.0),
                ParabolaSegment(length=2.0, end_radius=0.0, coefficient=1.0),
            ),
        )
        assert measure(build_solid(profile)).volume > 0.0

    def test_axis_joints_are_report_level_not_gated(self) -> None:
        """轴处（r=0）的折角不进生成期门禁——报告级留痕（既有口径）。

        VK 基底切向竖直（与柱段 G1 ✓）；段 1→2、2→3 在轴处呈 90° 折角——
        回转面上退化为单点，不是"可见折痕"，生成不拦（退化段照样不产出节点）。
        """
        profile = MeridianProfile(
            name="axis-joints",
            base_radius=1.0,
            segments=(
                LineSegment(length=1.0, end_radius=1.0),
                VonKarmanSegment(length=1.5, end_radius=0.0),
                LineSegment(length=1.0, end_radius=0.0),
                VonKarmanSegment(length=1.5, end_radius=1.0),
                LineSegment(length=1.0, end_radius=1.0),
            ),
        )
        assert measure(build_solid(profile)).volume > 0.0

    def test_curve_crese_raises(self) -> None:
        """曲线族折痕同样拦截：幂律 n<1 起步水平，接柱段即 90° 折痕。"""
        profile = MeridianProfile(
            name="curve-crease",
            base_radius=1.0,
            segments=(
                LineSegment(length=1.0, end_radius=1.0),
                PowerLawSegment(length=2.0, end_radius=0.4, exponent=0.7),
            ),
        )
        with pytest.raises(MeridianError, match="段 0"):
            build_solid(profile)
