"""简化捆绑几何（规格 §1.7.6 OI-36 ④ / OI-33 具名节点机制的沿用）。

覆盖：``booster-<k>`` 节点名逐枚存在且持 mesh；GLB 世界包围盒随助推器**横向变宽**；
metrics 的 ``boosters`` 块与 π r² L 手算对拍（助推器体积不得静默并入核心体积）；
缓存键对助推器摘要敏感、对省略助推器逐字节不敏感（§9.2 缓存纪律）。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from aeroforge.cache.store import compute_key
from aeroforge.geometry.bundle import (
    BOOSTER_GAP_M,
    BoosterSummary,
    booster_axis_radius,
    booster_label,
    build_bundle,
)
from aeroforge.geometry.meridian import LineSegment, MeridianProfile
from aeroforge.geometry.revolve import (
    GLB_ROOT_NAME,
    LOD2_ANGULAR,
    LOD2_DEFLECTION,
    build_segments,
    export_glb,
    glb_bounding_box,
    read_glb_json,
)

#: 单段柱芯级：r=1.0、L=3.0（与既有几何测试同族的夹具）。
_CORE = MeridianProfile(
    name="core", base_radius=1.0, segments=(LineSegment(length=3.0, end_radius=1.0),)
)

#: 2 枚助推器：直径 1.0 m、长 3.0 m（轴向与芯级等长，简化口径）。
_TWO_BOOSTERS = BoosterSummary(count=2, diameter_m=1.0, length_m=3.0)


def _nodes_by_name(path: Path) -> dict[str, dict[str, Any]]:
    return {str(node.get("name", "")): node for node in read_glb_json(path).get("nodes", [])}


def _child_names(path: Path, parent: str) -> list[str]:
    gltf = read_glb_json(path)
    nodes = gltf.get("nodes", [])
    names = {index: str(node.get("name", "")) for index, node in enumerate(nodes)}
    parent_index = next(index for index, name in names.items() if name == parent)
    return [names[int(child)] for child in nodes[parent_index].get("children", [])]


def _export(profile: MeridianProfile, boosters: BoosterSummary | None, path: Path) -> Path:
    export_glb(
        build_bundle(profile, boosters),
        path,
        deflection=LOD2_DEFLECTION,
        angular=LOD2_ANGULAR,
    )
    return path


def test_booster_nodes_exist_and_carry_meshes(tmp_path: Path) -> None:
    """OI-33 机制沿用：``booster-<k>`` 逐枚具名、各持 mesh，根节点名不变。"""
    target = _export(_CORE, _TWO_BOOSTERS, tmp_path / "bundle.glb")

    expected = [booster_label(index) for index in range(_TWO_BOOSTERS.count)]
    nodes = _nodes_by_name(target)
    for label in expected:
        assert nodes[label].get("mesh") is not None, f"节点 {label} 缺 mesh（静默失效形态）"
    # 场景图：根 vehicle + 芯级逐段 seg-<i> + 逐枚 booster-<k>
    children = _child_names(target, GLB_ROOT_NAME)
    assert children == ["seg-0", *expected]


def test_no_boosters_keeps_the_existing_scene_graph(tmp_path: Path) -> None:
    """无助推器：场景图与既有逐段路径完全一致（只有 seg-<i>，§9.2 缓存纪律）。"""
    target = _export(_CORE, None, tmp_path / "core.glb")

    assert _child_names(target, GLB_ROOT_NAME) == ["seg-0"]
    assert build_bundle(_CORE, None) is not None
    # build_bundle(None) 与 build_segments 的输出同构（同一份根复合体）
    legacy = build_segments(_CORE)
    assert [child.label for child in legacy.children] == ["seg-0"]


def test_glb_bounding_box_widens_with_boosters(tmp_path: Path) -> None:
    """GLB 世界包围盒：助推器必须真正落到场景里（横向变宽），总长不变。

    ⚠ 世界坐标为 Y-up（回转轴 = 世界 Y）。2 枚助推器沿世界 X 轴一条直径布置：
    X 向变宽（芯级 ±1 → 助推器外缘 ±2.1），**垂直方向（Z）由芯级主导**
    （±0.9997 > 助推器 ±0.5）——这是周向均布的正确行为，不是"没画出来"。
    """
    core_box = glb_bounding_box(_export(_CORE, None, tmp_path / "core.glb"))
    bundle_box = glb_bounding_box(_export(_CORE, _TWO_BOOSTERS, tmp_path / "bundle.glb"))

    core_size = tuple(hi - lo for lo, hi in zip(core_box[0], core_box[1], strict=True))
    bundle_size = tuple(hi - lo for lo, hi in zip(bundle_box[0], bundle_box[1], strict=True))
    assert bundle_size[0] > core_size[0] * 1.5, "X 向未变宽——助推器没有真正落到场景里"
    assert bundle_size[2] == pytest.approx(core_size[2], rel=1e-6), (
        "2 枚沿一条直径布置时垂直方向应由芯级主导（周向均布的几何事实）"
    )
    assert bundle_size[1] == pytest.approx(core_size[1], rel=1e-3), "总长不应因助推器改变"

    # X 向半宽可手算：轴线径向位置 = 芯级最大半径 + 助推器半径 + 间隙
    axis_radius = booster_axis_radius(1.0, _TWO_BOOSTERS.diameter_m)
    expected_half_width = axis_radius + _TWO_BOOSTERS.diameter_m / 2.0
    assert bundle_size[0] / 2.0 == pytest.approx(expected_half_width, rel=1e-3)


def test_four_boosters_widen_both_transverse_axes(tmp_path: Path) -> None:
    """4 枚周向均布（90° 步距）：世界 X 与 Z **两根横向轴都**变宽。"""
    four = BoosterSummary(count=4, diameter_m=1.0, length_m=3.0)
    core_box = glb_bounding_box(_export(_CORE, None, tmp_path / "core.glb"))
    bundle_box = glb_bounding_box(_export(_CORE, four, tmp_path / "four.glb"))

    core_size = tuple(hi - lo for lo, hi in zip(core_box[0], core_box[1], strict=True))
    bundle_size = tuple(hi - lo for lo, hi in zip(bundle_box[0], bundle_box[1], strict=True))
    for axis in (0, 2):
        assert bundle_size[axis] > core_size[axis] * 1.5, (
            f"世界轴 {axis} 未变宽——4 枚均布应沿两根横向轴都撑开包络"
        )


def test_booster_axis_radius_keeps_the_gap() -> None:
    """径向位置公式：芯级最大半径 + 助推器半径 + 0.1 m 间隙（OI-36 ④）。"""
    assert booster_axis_radius(2.5, 3.35) == pytest.approx(2.5 + 3.35 / 2.0 + BOOSTER_GAP_M)


def test_four_boosters_are_evenly_distributed(tmp_path: Path) -> None:
    """周向均布：4 枚助推器的节点齐全（cz-5 构型；周向角由解析位置保证）。"""
    four = BoosterSummary(count=4, diameter_m=1.0, length_m=2.0)
    target = _export(_CORE, four, tmp_path / "four.glb")
    children = _child_names(target, GLB_ROOT_NAME)
    assert children == ["seg-0", *(booster_label(k) for k in range(4))]


def test_metrics_booster_block_matches_hand_computation() -> None:
    """metrics.boosters 块：BREP 体积 = π r² L 手算对拍，核心体积保持芯级口径。"""
    from aeroforge.geometry.bundle import booster_assembly
    from aeroforge.geometry.revolve import build_solid

    per_booster_expected = math.pi * (_TWO_BOOSTERS.diameter_m / 2.0) ** 2 * _TWO_BOOSTERS.length_m

    # 与作业执行器同一口径组装：芯级体积不入 boosters 块（单独成账，OI-36）
    core_part = build_solid(_CORE)
    _, booster_solids = booster_assembly(_CORE, _TWO_BOOSTERS, core_part)
    volumes = [solid.volume for solid in booster_solids]

    assert len(volumes) == _TWO_BOOSTERS.count
    for volume in volumes:
        assert volume == pytest.approx(per_booster_expected, rel=1e-9)
    total = sum(volumes)
    assert total == pytest.approx(_TWO_BOOSTERS.count * per_booster_expected, rel=1e-9)
    # 两本账：无助推器组装必须原样返回芯级整体体（核心体积口径不被捆绑组装改写）
    same_part, no_boosters = booster_assembly(_CORE, None, core_part)
    assert no_boosters == []
    assert same_part is core_part


def test_cache_key_is_booster_sensitive_and_byte_stable_without() -> None:
    """§9.2：键对助推器摘要敏感；省略助推器时与既有实现**逐字节同键**。"""
    plain = compute_key(_CORE)
    assert plain.key == compute_key(_CORE, boosters=None).key  # 既有输入字节不变

    boosted = compute_key(_CORE, boosters=_TWO_BOOSTERS)
    other = compute_key(_CORE, boosters=BoosterSummary(count=4, diameter_m=1.0, length_m=3.0))
    assert boosted.key != plain.key, "带助推器与不带助推器不得共享缓存键"
    assert other.key != boosted.key, "不同捆绑构型不得共享缓存键"
