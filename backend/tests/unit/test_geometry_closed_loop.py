"""几何闭环：解析解 ↔ 内核 ↔ GLB 三方对拍（规格 §5.7 / §16.3 验收项 1、3）。

三层验证缺一不可：

1. **解析解 vs 教科书闭式解** —— 若解析模块本身写错，内核也就无从对照。
2. **内核 vs 解析解**（门禁 < 0.1%）—— 这是 M1 的首要验收项。
3. **GLB 世界包围盒 vs 解析包络**（门禁 ≤ 1%）—— 权威通道的**唯一**判据：
   STEP 内部单位正确不代表 GLB 导出单位正确，漏传 ``unit=Unit.M`` 只有在这一步才暴露。
   ⚠ 比对的是 GLB 的**世界**坐标（含场景图节点变换，即 glTF 的 Y-up），不是访问器的局部
   min/max——两者轴向不同（见 ``glb_bounding_box`` 与 ``test_dual_channel_orientation``）。

⚠ 第 2 项之所以是真对照，是因为两侧算法**完全独立**：内核走 OCCT 精确弧，解析走闭式积分解。
若任一侧改用采样折线逼近，误差会趋零但检验失效（自证），见 meridian.py 的模块说明。
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from aeroforge.geometry.analytic import analyze
from aeroforge.geometry.meridian import (
    ArcSegment,
    EllipseSegment,
    LineSegment,
    MeridianProfile,
    canonical_json,
    parse_profile,
    resolve,
)
from aeroforge.geometry.revolve import (
    LOD2_ANGULAR,
    LOD2_DEFLECTION,
    build_solid,
    export_glb,
    glb_bounding_box,
    measure,
    relative_error,
)

#: M1 验收门禁（§16.3）：解析体积 vs 内核体积的相对误差上限。
VOLUME_GATE = 1e-3

#: 双通道包围盒门禁（§13.6 / §16.3）：GLB 包络 vs 解析包络的相对误差上限。
ENVELOPE_GATE = 1e-2

#: 解析解对教科书闭式解的容差：纯解析数学，只该有浮点噪声。
TEXTBOOK_TOL = 1e-9


def _cylinder() -> MeridianProfile:
    """柱段：r=1, h=3。"""
    return MeridianProfile(
        name="cyl", base_radius=1.0, segments=(LineSegment(length=3.0, end_radius=1.0),)
    )


def _cone() -> MeridianProfile:
    """锥段：r=1 → 0, h=2（母线为直线，非穹顶）。"""
    return MeridianProfile(
        name="cone", base_radius=1.0, segments=(LineSegment(length=2.0, end_radius=0.0),)
    )


def _sphere_cap() -> MeridianProfile:
    """球冠：R=H=1 时 ρ=1，恰为半球。"""
    return MeridianProfile(
        name="cap", base_radius=1.0, segments=(ArcSegment(length=1.0, end_radius=0.0),)
    )


def _ellipsoid_cap() -> MeridianProfile:
    """椭球底：半轴 (1, 2)，即纵向拉长的半椭球。"""
    return MeridianProfile(
        name="ellip", base_radius=1.0, segments=(EllipseSegment(length=2.0, end_radius=0.0),)
    )


def _common_bulkhead() -> MeridianProfile:
    """共底包络：底穹顶 + 柱段 + 顶穹顶（无轴处尖点）。

    共底的**共同隔板是内部结构**，不改变推进剂包络外形，故包络就是"两只穹顶夹一段柱"。
    """
    return MeridianProfile(
        name="bulkhead",
        base_radius=0.0,
        segments=(
            ArcSegment(length=1.0, end_radius=1.0),
            LineSegment(length=3.0, end_radius=1.0),
            ArcSegment(length=1.0, end_radius=0.0),
        ),
    )


#: 五个参考构型 → (构造器, 教科书闭式体积)
#: 闭式解：柱 πr²h；锥 πr²h/3；半球 2πρ³/3；半椭球 2πa²b/3；共底 2·(2π/3) + π·1²·3
REFERENCE_CASES: list[tuple[str, MeridianProfile, float]] = [
    ("柱", _cylinder(), math.pi * 1.0**2 * 3.0),
    ("锥", _cone(), math.pi * 1.0**2 * 2.0 / 3.0),
    ("球冠", _sphere_cap(), 2.0 * math.pi * 1.0**3 / 3.0),
    ("椭球", _ellipsoid_cap(), 2.0 * math.pi * 1.0**2 * 2.0 / 3.0),
    ("共底", _common_bulkhead(), 2.0 * (2.0 * math.pi / 3.0) + math.pi * 1.0**2 * 3.0),
]

_CASE_IDS = [name for name, _, _ in REFERENCE_CASES]


@pytest.mark.parametrize(("name", "profile", "expected"), REFERENCE_CASES, ids=_CASE_IDS)
def test_analytic_matches_textbook(name: str, profile: MeridianProfile, expected: float) -> None:
    """解析解必须与教科书闭式解一致——否则内核对照无意义。"""
    metrics = analyze(resolve(profile))

    assert relative_error(metrics.volume, expected) < TEXTBOOK_TOL


@pytest.mark.parametrize(("name", "profile", "expected"), REFERENCE_CASES, ids=_CASE_IDS)
def test_kernel_matches_analytic(name: str, profile: MeridianProfile, expected: float) -> None:
    """M1 首要门禁：内核体积 vs 解析体积 < 0.1%。"""
    analytic = analyze(resolve(profile))
    kernel = measure(build_solid(profile))

    error = relative_error(kernel.volume, analytic.volume)
    assert error < VOLUME_GATE, (
        f"{name}：内核 {kernel.volume} vs 解析 {analytic.volume}，误差 {error:.3e}"
    )
    assert kernel.is_valid
    assert kernel.solid_count == 1


@pytest.mark.parametrize(("name", "profile", "expected"), REFERENCE_CASES, ids=_CASE_IDS)
def test_glb_envelope_matches_analytic(
    tmp_path: Path, name: str, profile: MeridianProfile, expected: float
) -> None:
    """双通道一致性的权威侧：GLB **世界**包围盒 vs 解析包络 ≤ 1%。

    用 LOD2（细网格）：圆周长被离散化为内接多边形，半径方向恒**略小**，
    取细网格才能让该项门禁的余量反映真实精度而非离散化步长。

    ⚠ **不可逐轴 zip**：解析包络是内核的 Z-up `(2R, 2R, L)`，而 GLB 的世界坐标已是 glTF 的
    Y-up `(2R, L, 2R)`（导出器给根节点写了 `Rx(-90°)`）。按轴序硬对齐会在"轴向落在哪根轴"
    上失真——M2 首项查出的缺陷正是这一类（详见 ``test_dual_channel_orientation``）。
    """
    part = build_solid(profile)
    target = tmp_path / "model.glb"
    export_glb(part, target, deflection=LOD2_DEFLECTION, angular=LOD2_ANGULAR)

    (min_x, min_y, min_z), (max_x, max_y, max_z) = glb_bounding_box(target)
    envelope = analyze(resolve(profile)).envelope  # 内核口径：(2R, 2R, L)
    expected_diameter, expected_length = envelope[0], envelope[2]

    for label, actual, wanted in (
        ("世界 X（径向）", max_x - min_x, expected_diameter),
        ("世界 Y（轴向）", max_y - min_y, expected_length),
        ("世界 Z（径向）", max_z - min_z, expected_diameter),
    ):
        error = relative_error(actual, wanted)
        assert error < ENVELOPE_GATE, (
            f"{name} {label}：GLB {actual} vs 解析 {wanted}，误差 {error:.3e}"
        )


def test_canonical_json_roundtrip_is_byte_identical() -> None:
    """母线保存/重载往返一致（§16.3 验收项 2）：逐字节相同。"""
    profile = _common_bulkhead()

    first = canonical_json(profile)
    second = canonical_json(parse_profile(first))

    assert first == second


def test_canonical_json_ignores_float_noise() -> None:
    """浮点噪声不得改变缓存键：1e-12 级差异必须在规范化后被吸收。"""
    base = _cylinder()
    jittered = base.model_copy(
        update={"segments": (LineSegment(length=3.0 + 1e-12, end_radius=1.0),)}
    )

    assert canonical_json(base) == canonical_json(jittered)
