"""GLB 场景图契约门禁：分级显隐的**唯一前提**（OI-33 ② / §13.6）。

由来（OI-19 的硬前提不成立，实测查出）
------------------------------------
规格要求"按级 / 按部件显示或隐藏"，而落地前实测发现权威产物 ``model_lod2.glb`` 是
**1 个节点 / 1 个网格 / 3 个 primitive 且无节点名**——几何里**没有可分级的对象**。
前端按名找不到节点就等于**不隐藏**，即"点了没反应"且**全程零报错**。

故 OI-33 裁决：几何层改为 ``vehicle``（根，不持 mesh）+ 逐段具名 ``seg-<i>``，
本文件把该契约的每条硬约束都变成会响的断言（缺一条都是一种静默失效）：

1. **节点名必须存在且与 ``profile.segments`` 逐一对应**——缺名 = 找不到 = 不隐藏；
2. **根节点不得持 mesh**——否则整体网格与分段网格叠加渲染，同一处被画两遍；
3. **分段不改变外形**——未隐藏任何段时，分段 GLB 的世界包围盒必须与整体体一致，
   且分段体积之和 = 整体体积（否则"看到的"与"算的"分叉，P1 / ADR-012）；
4. **退化段不产出节点**，且**后续段的下标不得因此前移**——前移会让前端隐藏错段。

⚠ 本文件的比对全部在 **世界坐标** 下进行（``glb_bounding_box`` 会施加场景图节点变换），
理由见 ``test_dual_channel_orientation`` 的模块说明。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aeroforge.geometry.analytic import analyze
from aeroforge.geometry.meridian import (
    ArcSegment,
    EllipseSegment,
    LineSegment,
    MeridianProfile,
    resolve,
)
from aeroforge.geometry.revolve import (
    GLB_ROOT_NAME,
    LOD2_ANGULAR,
    LOD2_DEFLECTION,
    build_segments,
    build_solid,
    export_glb,
    glb_bounding_box,
    measure,
    read_glb_json,
    relative_error,
    segment_label,
)
from aeroforge.geometry.validate import validate_meridian

#: 分段不得改变外形：分段 GLB 与整体体导出的世界包围盒相对误差上限（§13.6）。
ENVELOPE_GATE = 1e-2

#: 分段体积之和 = 整体体积的容差：段间分界面是**纯粹的内部划分**，不产生额外布尔运算，
#: 故这里只该有浮点噪声（比包围盒的 1% 严格得多）。
VOLUME_SUM_TOL = 1e-9

Box = tuple[float, float, float]


def _segmented_capsule() -> MeridianProfile:
    """3 段"胶囊"：椭球底 + 柱 + 椭球顶。

    用 ``ellipse`` 而不用 ``arc``：椭圆段的半径端**就是**其最大半径（切向严格竖直），
    故与柱段 G1 连续；而球冠段的球半径 ρ=(R²+H²)/2H 在 H≠R 时会鼓出到 ρ > R，
    使"最大半径"落在弧的中段而非端点（本文件的世界包络断言会被它咬到）。
    """
    return MeridianProfile(
        name="segmented-capsule",
        base_radius=0.0,
        segments=(
            EllipseSegment(length=1.5, end_radius=1.0),
            LineSegment(length=4.0, end_radius=1.0),
            EllipseSegment(length=2.0, end_radius=0.0),
        ),
    )


def _segmented_cylinder() -> MeridianProfile:
    """单段柱：分段退化的**边界情形**——场景图应为 `vehicle` + 恰好一个 `seg-0`。"""
    return MeridianProfile(
        name="segmented-cylinder",
        base_radius=1.0,
        segments=(LineSegment(length=3.0, end_radius=1.0),),
    )


def _degenerate_middle() -> MeridianProfile:
    """含**退化段**的剖面：穹顶 → 轴向段（两端半径均为 0）→ 反向穹顶 → 柱。

    轴向段与穹顶段在轴处呈 90° 折角，**母线校验按 G1 拒绝**（故归 ILLEGAL）。
    这里用它只为验证几何层的"退化段不产出节点"分支——该分支不能靠正常输入走到，
    但一旦漏判，分段节点名就会与 ``profile.segments`` 的下标错位（前端隐藏错段）。
    """
    return MeridianProfile(
        name="degenerate-middle",
        base_radius=1.0,
        segments=(
            ArcSegment(length=1.0, end_radius=0.0),
            LineSegment(length=1.0, end_radius=0.0),
            ArcSegment(length=1.0, end_radius=1.0),
            LineSegment(length=2.0, end_radius=1.0),
        ),
    )


#: 分段场景图契约的参数化用例
CASES: list[tuple[str, MeridianProfile]] = [
    ("capsule", _segmented_capsule()),
    ("cylinder", _segmented_cylinder()),
]

_CASE_IDS = [name for name, _ in CASES]


def _export_segments(profile: MeridianProfile, tmp_path: Path, name: str) -> Path:
    target = tmp_path / f"{name}-segments.glb"
    export_glb(build_segments(profile), target, deflection=LOD2_DEFLECTION, angular=LOD2_ANGULAR)
    return target


def _nodes_by_name(path: Path) -> dict[str, dict[str, Any]]:
    gltf = read_glb_json(path)
    return {str(node.get("name", "")): node for node in gltf.get("nodes", [])}


def _scene_roots(path: Path) -> list[str]:
    gltf = read_glb_json(path)
    nodes = gltf.get("nodes", [])
    scenes = gltf.get("scenes", [])
    scene_index = gltf.get("scene")
    if isinstance(scene_index, int) and 0 <= scene_index < len(scenes):
        indices = [int(index) for index in scenes[scene_index].get("nodes", [])]
    else:
        indices = [int(index) for index in scenes[0].get("nodes", [])] if scenes else []
    return [str(nodes[index].get("name", "")) for index in indices]


def _child_names(path: Path, parent: str) -> list[str]:
    gltf = read_glb_json(path)
    nodes = gltf.get("nodes", [])
    names = {index: str(node.get("name", "")) for index, node in enumerate(nodes)}
    parent_index = next(index for index, name in names.items() if name == parent)
    return [names[int(child)] for child in nodes[parent_index].get("children", [])]


def _size(box: tuple[Box, Box]) -> Box:
    (min_x, min_y, min_z), (max_x, max_y, max_z) = box
    return (max_x - min_x, max_y - min_y, max_z - min_z)


def _segment_solids(compound: Any) -> list[tuple[str, Any]]:
    """取出分段实体及其节点名（顺序即 GLB 节点顺序）。"""
    return [(str(child.label), child) for child in compound.children]


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_root_node_is_named_and_carries_no_mesh(
    tmp_path: Path, name: str, profile: MeridianProfile
) -> None:
    """硬约束 ②：根节点必须是**唯一**的场景根、名为 ``vehicle``、且**不持 mesh**。

    根节点一旦自己带 mesh，就会与各段网格**叠加渲染**——同一处被画两遍（几何被画厚
    一层），肉眼不易察觉，而显隐某段时"少了一层的那个残影"才会露馅。
    """
    target = _export_segments(profile, tmp_path, name)
    nodes = _nodes_by_name(target)

    assert GLB_ROOT_NAME in nodes, (
        f"{name}：GLB 内没有名为 {GLB_ROOT_NAME!r} 的根节点（实际节点名：{sorted(nodes)}）——"
        "前端按名取场景根，改名或改名缺失都会让显隐静默失效"
    )
    assert _scene_roots(target) == [GLB_ROOT_NAME], (
        f"{name}：场景根不是唯一的 {GLB_ROOT_NAME!r}（实际 {_scene_roots(target)}）——"
        "根节点承载 glTF 的 Z-up → Y-up 旋转，多根会丢掉该变换"
    )
    assert nodes[GLB_ROOT_NAME].get("mesh") is None, (
        f"{name}：根节点自带 mesh（mesh={nodes[GLB_ROOT_NAME].get('mesh')}）——"
        "整体网格会与分段网格叠加渲染，同一处被画两遍"
    )


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_every_segment_has_a_named_node_in_order(
    tmp_path: Path, name: str, profile: MeridianProfile
) -> None:
    """硬约束 ①：``seg-<i>`` 与 ``profile.segments`` **逐一对应**，缺一即失败。

    `<i>` 是段链的 **0 基下标**（与参数层 `stages[i]` 无关）。这条断言故意逐段列出
    期望名，而不是只比数量——数量相等但错位一位时，前端会隐藏**相邻的另一段**，
    而报告里看不出任何异常。
    """
    target = _export_segments(profile, tmp_path, name)
    expected = [segment_label(index) for index in range(len(profile.segments))]

    assert _child_names(target, GLB_ROOT_NAME) == expected, (
        f"{name}：根节点的子节点为 {_child_names(target, GLB_ROOT_NAME)}，"
        f"期望 {expected}——分段节点名与段序不一致（前端会隐藏错段）"
    )
    for label in expected:
        assert _nodes_by_name(target)[label].get("mesh") is not None, (
            f"{name}：节点 {label} 没有 mesh——隐藏它不会有任何可见变化（静默失效）"
        )


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_segment_nodes_share_the_same_world_space_as_the_whole_solid(
    tmp_path: Path, name: str, profile: MeridianProfile
) -> None:
    """硬约束 ③：分段 GLB 的世界包围盒 = 整体体导出的世界包围盒（≤ 1%）。

    两者出自**同一份几何**的两条导出路径，故这不是自证；它拦的是"分段体各自被
    单独施加了一次根旋转/平移"这类错误——那种错误会让每段都整体错位。
    """
    segmented = _export_segments(profile, tmp_path, name)
    whole = tmp_path / f"{name}-whole.glb"
    export_glb(build_solid(profile), whole, deflection=LOD2_DEFLECTION, angular=LOD2_ANGULAR)

    segmented_box = glb_bounding_box(segmented)
    whole_box = glb_bounding_box(whole)

    # 0 夹具区分力：分段必须真的存在，否则"两盒一致"是恒真的
    assert len(profile.segments) >= 1
    for axis in (0, 1, 2):
        size_error = relative_error(_size(segmented_box)[axis], _size(whole_box)[axis])
        assert size_error < ENVELOPE_GATE, (
            f"{name} 世界轴 {axis}：分段 {_size(segmented_box)[axis]:.6f} vs "
            f"整体 {_size(whole_box)[axis]:.6f}，误差 {size_error:.3e}——分段改变了外形"
        )
        for edge in (0, 1):
            error = relative_error(segmented_box[edge][axis], whole_box[edge][axis])
            assert error < ENVELOPE_GATE, (
                f"{name} 世界轴 {axis} 的 {('min', 'max')[edge]}：分段 "
                f"{segmented_box[edge][axis]:.6f} vs 整体 {whole_box[edge][axis]:.6f}，"
                f"误差 {error:.3e}——分段整体错位（尺寸一致但位置不对）"
            )


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_segment_world_envelope_matches_analytic(
    tmp_path: Path, name: str, profile: MeridianProfile
) -> None:
    """分段 GLB 的世界包络 vs 解析包络 ≤ 1%（§16.3 的双通道判据，权威侧仍须成立）。

    ⚠ **不可逐轴 zip**：解析包络是内核口径的 Z-up ``(2R, 2R, L)``，而 GLB 世界坐标已是
    glTF 的 Y-up ``(2R, L, 2R)``（导出器给根节点写了 ``Rx(-90°)``）。
    """
    target = _export_segments(profile, tmp_path, name)
    (min_x, min_y, min_z), (max_x, max_y, max_z) = glb_bounding_box(target)
    world_size = (max_x - min_x, max_y - min_y, max_z - min_z)

    envelope = analyze(resolve(profile)).envelope  # (2R, 2R, L)
    diameter, length = envelope[0], envelope[2]

    assert relative_error(world_size[1], length) < ENVELOPE_GATE, (
        f"{name}：世界 Y 向 {world_size[1]:.6f} 与总长 {length:.6f} 不符——"
        "分段后回转轴已不在世界 Y 上"
    )
    for axis in (0, 2):
        assert relative_error(world_size[axis], diameter) < ENVELOPE_GATE, (
            f"{name}：世界轴 {axis} 尺寸 {world_size[axis]:.6f} 与直径 {diameter:.6f} 不符"
        )
    assert abs(min_y) < ENVELOPE_GATE * length


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_segment_volumes_sum_to_the_whole_solid(name: str, profile: MeridianProfile) -> None:
    """硬约束 ③（体积口径）：各分段体体积之和 = 整体体体积。

    段间新增的分界面是**纯内部划分**，不产生任何布尔运算，故容差取浮点噪声量级。
    这条断言是"分段不改变几何"在**实体内**（而非包围盒）的版本——包围盒对
    "某段被拉伸但整体包络不变"这类错误是失明的。
    """
    whole = measure(build_solid(profile)).volume
    parts = _segment_solids(build_segments(profile))
    total = sum(measure(part).volume for _, part in parts)

    assert len(parts) == len(profile.segments), (
        f"{name}：分段体数 {len(parts)} ≠ 段数 {len(profile.segments)}"
    )
    assert total > 0.0, f"{name}：分段体体积之和为 {total}，夹具已退化"
    assert relative_error(total, whole) < VOLUME_SUM_TOL, (
        f"{name}：分段体积之和 {total:.12f} vs 整体 {whole:.12f}，"
        f"误差 {relative_error(total, whole):.3e}——分段引入了重叠或空隙"
    )


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_segment_outline_is_aligned_with_segments(
    tmp_path: Path, name: str, profile: MeridianProfile
) -> None:
    """示意通道的同粒度前提（OI-33 ③）：``segment_outline`` 与段链**等长且同序**。

    示意通道逐段建网格，段号取自该数组的下标——数组一旦比段链短（例如把退化段
    直接跳过），前端按 `seg-<i>` 隐藏时就会隐藏**相邻的另一段**。
    """
    report = validate_meridian(profile)
    assert len(report.segment_outline) == len(profile.segments), (
        f"{name}：segment_outline 有 {len(report.segment_outline)} 组，"
        f"段链有 {len(profile.segments)} 段——下标错位会让前端隐藏错段"
    )
    for index, chunk in enumerate(report.segment_outline):
        assert len(chunk) >= 2, f"{name}：第 {index} 段的轮廓点数 {len(chunk)} < 2，无法建模"
        # 每段轮廓必须自轴线起、回轴线止，才能各自回转为**封闭**曲面
        assert abs(chunk[0][0]) <= 1e-9, f"{name}：第 {index} 段轮廓未自轴线起"
        assert abs(chunk[-1][0]) <= 1e-9, f"{name}：第 {index} 段轮廓未回到轴线"


def test_degenerate_segment_yields_no_node_and_keeps_indices_aligned(tmp_path: Path) -> None:
    """硬约束 ④：退化段不产出节点，**且后续段的下标不得前移**。

    夹具的段 1 是两端半径均为 0 的轴向段（回转体为空）。期望场景图为
    ``[seg-0, seg-2, seg-3]``——**保留空洞**。若实现改成"跳过退化段后重编号"，
    节点名就会与 ``profile.segments`` 错位一位，前端隐藏 `seg-2` 时动的却是第 3 段。
    """
    profile = _degenerate_middle()
    target = _export_segments(profile, tmp_path, "degenerate")

    expected = [
        segment_label(index)
        for index, seg in enumerate(profile.segments)
        if not (seg.end_radius <= 1e-9 and _start_radius(profile, index) <= 1e-9)
    ]
    assert _child_names(target, GLB_ROOT_NAME) == expected, (
        f"分段节点名 {_child_names(target, GLB_ROOT_NAME)} 与期望 {expected} 不符——"
        "退化段被跳过时下标发生了重编号"
    )
    assert segment_label(1) not in _nodes_by_name(target)

    # 退化段在示意通道侧同样必须**占位**（空列表），否则两侧的段号不再是同一个
    outline = validate_meridian(profile).segment_outline
    assert len(outline) == len(profile.segments)
    assert outline[1] == [], f"退化段应返回空轮廓以保下标对齐，实际 {outline[1]}"
    assert all(chunk for index, chunk in enumerate(outline) if index != 1)


def _start_radius(profile: MeridianProfile, index: int) -> float:
    """第 ``index`` 段的起点半径（段链首尾相接，故等于前一段的终点半径）。"""
    if index == 0:
        return profile.base_radius
    return profile.segments[index - 1].end_radius


def test_all_degenerate_profile_fails_loudly() -> None:
    """全退化剖面必须**明确报错**，而不是导出一个没有任何 `seg-` 节点的 GLB。

    ⚠ 该剖面**刻意不做成夹具工厂**：``validate_meridian`` 对它直接抛 ``ValueError``
    （``analyze()`` 拒绝零体积剖面求质心），拿不到报告，因此无法归入
    ``test_fixture_profiles`` 的任何一类。而 ``POST /api/geometry/build`` 并**不**
    先过母线校验，故这个输入真能走到几何层——正因如此几何层必须自己拦：
    静默产出一个无节点的 GLB，会让前端的每一次显隐操作都"点了没反应"，
    且**没有任何报错**（"没报错 ≠ 正确"的又一形态）。
    """
    profile = MeridianProfile(
        name="degenerate-only",
        base_radius=0.0,
        segments=(LineSegment(length=1.0, end_radius=0.0),),
    )

    with pytest.raises(RuntimeError, match="退化"):
        build_segments(profile)
