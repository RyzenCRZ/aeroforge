"""母线层校验：G1 拦截、尖点例外、采样输出（规格 §5.7 / §16.3）。

这些检查是**纯 Python**的（无 OCCT），因此可以支撑"改参数即校验"的交互频率；
本模块同时守住它的边界：不得因追求覆盖率而把实体层检查搬进来（那需要内核）。
"""

from __future__ import annotations

import pytest

from aeroforge.geometry.meridian import (
    ArcSegment,
    LineSegment,
    MeridianError,
    MeridianProfile,
)
from aeroforge.geometry.validate import validate_meridian


def _g1_clean() -> MeridianProfile:
    """柱段 + 球冠：接头处切向严格竖直，G1 应通过。"""
    return MeridianProfile(
        name="g1-ok",
        base_radius=1.0,
        segments=(LineSegment(length=3.0, end_radius=1.0), ArcSegment(length=1.0, end_radius=0.0)),
    )


def _g1_violation() -> MeridianProfile:
    """柱段后接一个斜锥：接头处切向夹角约 26.6°，远超 0.5° 容差。"""
    return MeridianProfile(
        name="g1-bad",
        base_radius=1.0,
        segments=(LineSegment(length=3.0, end_radius=1.0), LineSegment(length=1.0, end_radius=1.5)),
    )


def _pinch() -> MeridianProfile:
    """两只穹顶在轴线上反向对接：数学合法，但 OCCT 会因退化顶点判实体无效。"""
    return MeridianProfile(
        name="pinch",
        base_radius=1.0,
        segments=(ArcSegment(length=1.0, end_radius=0.0), ArcSegment(length=1.0, end_radius=1.0)),
    )


def test_valid_profile_passes() -> None:
    report = validate_meridian(_g1_clean())

    assert report.ok
    assert all(check.severity != "fail" for check in report.checks)
    assert report.joints[0].severity == "pass"
    assert report.joints[0].angle_deg == pytest.approx(0.0, abs=1e-9)
    assert report.volume > 0.0


def test_g1_violation_is_rejected() -> None:
    report = validate_meridian(_g1_violation())

    assert not report.ok
    joint = report.joints[0]
    assert joint.severity == "fail"
    assert joint.kind == "g1"
    assert joint.angle_deg > 20.0

    g1_check = next(check for check in report.checks if check.check == "G1 切向连续")
    assert g1_check.severity == "fail"


def test_pinch_is_warning_not_failure() -> None:
    """轴处反向尖点必须被**识别并解释**，而不是当成普通 G1 违规。"""
    report = validate_meridian(_pinch())

    joint = report.joints[0]
    assert joint.kind == "pinch"
    assert joint.severity == "warn"
    assert joint.angle_deg == pytest.approx(180.0, abs=1e-6)

    g1_check = next(check for check in report.checks if check.check == "G1 切向连续")
    assert g1_check.severity == "warn"
    assert "退化顶点" in g1_check.detail


def test_returns_sampling_for_frontend() -> None:
    """validate 必须回传采样点与闭合轮廓——前端据此渲染，不做任何几何运算（ADR-011）。"""
    report = validate_meridian(_g1_clean())

    assert len(report.sample) > 3
    # 采样自下而上：z 单调不减
    zs = [z for _, z in report.sample]
    assert zs == sorted(zs)
    # 闭合轮廓首尾都必须回到轴线，才能围成回转区域
    assert report.outline[0] == (0.0, 0.0)
    assert report.outline[-1][0] == 0.0
    assert report.envelope[2] == pytest.approx(4.0, abs=1e-9)


def test_dome_must_touch_axis_at_exactly_one_end() -> None:
    """穹顶段两端都不在轴线上时必须报错，并给出"改用 line 段"的可操作指引。"""
    bad = MeridianProfile(
        name="bad-dome",
        base_radius=1.0,
        segments=(ArcSegment(length=1.0, end_radius=2.0),),
    )

    with pytest.raises(MeridianError, match="恰有一端"):
        validate_meridian(bad)


def test_empty_segment_length_is_rejected() -> None:
    """length ≤ 0 的段无法在 z 上推进，会让剖面的 r(z) 失去单值性。

    pydantic 会把模型校验器里抛出的域错误包成 ``ValidationError``（ValueError 子类），
    故这里按 ``ValueError`` 断言；API 层则由异常处理器还原为 §10.3 结构。
    """
    with pytest.raises(ValueError, match="length 必须为正"):
        MeridianProfile(
            name="zero", base_radius=1.0, segments=(LineSegment(length=0.0, end_radius=1.0),)
        )
