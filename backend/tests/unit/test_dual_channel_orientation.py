"""双通道**朝向**一致性门禁（ADR-012 / R-25 / §13.6）。

由来（M1 唯一遗留的留白 → M2 首项目视核对结案，并**查出真实缺陷**）
------------------------------------------------------------------
M1 验收时双通道只对拍了**尺寸**：`test_geometry_closed_loop` 断言 GLB 包围盒与解析包络逐轴
≤ 1%，前端 `schematicGeometry.test.ts` 断言示意网格包围盒与同一包络逐轴 ≤ 1%。两侧都在
**自己的坐标系里**自洽，于是"渲染器实际看到的世界里，回转轴落在哪根轴上"从未被断言。

M2 首项做真机目视核对时，两通道**明显不重合**（示意 = 竖直胶囊，权威 = 扁宽团块，质心偏移
约 20%，控制台零报错）：OCCT 导出器按 glTF 规范给**根节点**写了 `Rx(-90°)`（Z-up → Y-up），
而**访问器的 min/max 仍是 Z-up 局部坐标**——当初误把访问器当世界坐标，前端又转了一次 -90°，
两次叠加成 **-180°**，模型躺倒。尺寸类断言对此**完全失明**（三边长度不变，只是逐轴归属变了）。

本文件把这条链接上：两侧各从**独立数据**出发（示意侧 = 后端 `outline` 折线喂 `LatheGeometry`，
权威侧 = **真实 GLB 顶点**经场景图变换），在**世界坐标**下比对包围盒——两侧算法无关，故是
真相照而非自证。前端 `frontend/src/r3f/orientation.test.ts` 用真实 three 对象复核同一契约。

朝向契约（实测，非推测）
------------------------
柱 `r=1, L=3`：**访问器局部**为 `x/y ∈ ±1、z ∈ [0, 3]`（Z-up），**世界**为 `x/z ∈ ±1、
y ∈ [0, 3]`（Y-up，节点自带 `Rx(-90°)`）。故两个通道**都用恒等变换**：`LatheGeometry` 的轴
本来就是世界 Y，`GLTFLoader` 加载的 GLB 也已经是 Y-up。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aeroforge.geometry.meridian import ArcSegment, LineSegment, MeridianProfile

# `_node_matrix` / `_matrix_multiply` / `_transform_point` 是私有实现，但 `glb_bounding_box`
# 的正确性完全取决于其列主序语义，故直接引入并在 `test_node_matrix_helpers_*` 里钉住
# （父·子复合路径在单节点 GLB 上走不到，只能在此覆盖）。
from aeroforge.geometry.revolve import (
    LOD2_ANGULAR,
    LOD2_DEFLECTION,
    _matrix_multiply,
    _node_matrix,
    _transform_point,
    build_solid,
    export_glb,
    glb_bounding_box,
    relative_error,
)
from aeroforge.geometry.validate import validate_meridian

#: 双通道包围盒门禁（§13.6 / R-25）：两通道世界包围盒的相对误差上限。
ENVELOPE_GATE = 1e-2

#: 示意通道的变换：`LatheGeometry` 的回转轴即世界 Y，故为恒等（`SchematicMesh.tsx`）。
SCHEMATIC_IS_IDENTITY = True

#: 权威通道的变换：`AuthoritativeModel.tsx` 的 `AUTHORITATIVE_ROTATION`——**必须是恒等**
#: （GLB 场景图已自带 `Rx(-90°)`，渲染器看到的已经是 Y-up）。
AUTHORITATIVE_ROTATION_IS_IDENTITY = True

#: 夹具的区分力下限：轴向长度须明显区别于直径，否则"轴向落在世界 Y"这条断言是空的。
MIN_AXIAL_RATIO = 1.5

#: OCCT 导出器写在根节点上的旋转（实测四元数，即 `Rx(-90°)`）。
ROOT_QUATERNION = (-0.7071067811865475, 0.0, 0.0, 0.7071067811865475)

Box = tuple[float, float, float]


def _axial_cylinder() -> MeridianProfile:
    """柱：r=1, L=3（L / 2R = 1.5，足以区分轴向与径向）。"""
    return MeridianProfile(
        name="axial-cyl", base_radius=1.0, segments=(LineSegment(length=3.0, end_radius=1.0),)
    )


def _axial_capsule() -> MeridianProfile:
    """胶囊：r=1, L=5（L / 2R = 2.5）。"""
    return MeridianProfile(
        name="axial-capsule",
        base_radius=0.0,
        segments=(
            ArcSegment(length=1.0, end_radius=1.0),
            LineSegment(length=3.0, end_radius=1.0),
            ArcSegment(length=1.0, end_radius=0.0),
        ),
    )


CASES: list[tuple[str, MeridianProfile]] = [
    ("axial-cyl", _axial_cylinder()),
    ("axial-capsule", _axial_capsule()),
]

_CASE_IDS = [name for name, _ in CASES]


def _glb_json(path: Path) -> dict[str, Any]:
    """读出 GLB 的 JSON 块（仅本测试需要，用于对照"访问器局部坐标"与"世界坐标"）。"""
    raw = path.read_bytes()
    offset = 12
    while offset + 8 <= len(raw):
        chunk_length = int.from_bytes(raw[offset : offset + 4], "little")
        if raw[offset + 4 : offset + 8] == b"JSON":
            payload = raw[offset + 8 : offset + 8 + chunk_length]
            return json.loads(payload.decode("utf-8"))
        offset += 8 + chunk_length + (-chunk_length % 4)
    msg = f"GLB 缺少 JSON 块：{path}"
    raise RuntimeError(msg)


def _accessor_local_box(path: Path) -> tuple[Box, Box]:
    """GLB **访问器**里的 POSITION 包围盒——`Rx(-90°)` 之前的**局部**坐标。"""
    gltf = _glb_json(path)
    accessors = gltf["accessors"]
    lows = [float("inf")] * 3
    highs = [float("-inf")] * 3
    for mesh in gltf["meshes"]:
        for primitive in mesh["primitives"]:
            accessor = accessors[primitive["attributes"]["POSITION"]]
            for axis in range(3):
                lows[axis] = min(lows[axis], float(accessor["min"][axis]))
                highs[axis] = max(highs[axis], float(accessor["max"][axis]))
    return (lows[0], lows[1], lows[2]), (highs[0], highs[1], highs[2])


def _export(profile: MeridianProfile, tmp_path: Path, name: str) -> Path:
    target = tmp_path / f"{name}.glb"
    export_glb(build_solid(profile), target, deflection=LOD2_DEFLECTION, angular=LOD2_ANGULAR)
    return target


def _size(box: tuple[Box, Box]) -> Box:
    (min_x, min_y, min_z), (max_x, max_y, max_z) = box
    return (max_x - min_x, max_y - min_y, max_z - min_z)


def _schematic_world_box(profile: MeridianProfile) -> tuple[Box, tuple[float, float]]:
    """示意通道的世界包围盒：后端 `outline` 直接喂 `LatheGeometry`（恒等变换）。

    返回 `((size_x, size_y, size_z), (y_min, y_max))`。`LatheGeometry` 把轮廓的 `z` 作为
    世界高度 `y`、把半径铺到 XZ 平面，故轴向范围直接取自轮廓的 z 极值。
    """
    outline = validate_meridian(profile).outline
    max_radius = max(radius for radius, _ in outline)
    zs = [z for _, z in outline]
    assert SCHEMATIC_IS_IDENTITY, "示意通道一旦不再用恒等变换，本函数与前端契约同步失效"
    return (2.0 * max_radius, max(zs) - min(zs), 2.0 * max_radius), (min(zs), max(zs))


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_glb_world_box_puts_axis_on_world_y_starting_at_origin(
    tmp_path: Path, name: str, profile: MeridianProfile
) -> None:
    """权威通道的朝向契约：GLB 的**世界**包围盒轴向落在世界 **Y**，且自 `y = 0` 起。

    "世界"指渲染器实际看到的空间（`GLTFLoader` 会施加场景图节点变换），即 `glb_bounding_box`
    的现有口径。若哪天导出器改为直接把 Y-up 写进访问器、或把模型沿轴向居中
    （`y ∈ [-L/2, L/2]`），对齐关系立即变化——此测试必须先响，而不是等到视口里露馅。
    """
    target = _export(profile, tmp_path, name)
    (min_x, min_y, min_z), (max_x, max_y, max_z) = glb_bounding_box(target)

    envelope = validate_meridian(profile).envelope  # (2R, 2R, L)
    expected_diameter, expected_length = envelope[0], envelope[2]

    # 0 夹具区分力：轴向必须明显长于直径，否则"轴向落在 Y"的判断可能碰巧成立
    axial_ratio = expected_length / expected_diameter
    assert axial_ratio >= MIN_AXIAL_RATIO, (
        f"{name}：L / 2R = {axial_ratio:.2f} < {MIN_AXIAL_RATIO}，轴向与径向难以区分，"
        "本夹具无法证明朝向契约，须换一个更细长的构型"
    )

    assert AUTHORITATIVE_ROTATION_IS_IDENTITY, "前端一旦不再用恒等变换，本断言须同步修改"

    world_size = (max_x - min_x, max_y - min_y, max_z - min_z)
    error = relative_error(world_size[1], expected_length)
    assert error < ENVELOPE_GATE, (
        f"{name}：世界 Y 向尺寸 {world_size[1]:.6f} 与总长 {expected_length:.6f} 不符"
        f"（误差 {error:.3e}）——回转轴已不在世界 Y 上，双通道朝向契约被破坏"
    )
    for axis, actual in ((0, world_size[0]), (2, world_size[2])):
        error = relative_error(actual, expected_diameter)
        assert error < ENVELOPE_GATE, (
            f"{name}：世界轴 {axis} 尺寸 {actual:.6f} 与直径 {expected_diameter:.6f} 不符"
            f"（误差 {error:.3e}）——径向已不在世界 XZ 平面内"
        )

    # 起点必须落在原点：否则模型会半沉到参考网格（世界 y = 0）以下
    assert abs(min_y) < ENVELOPE_GATE * expected_length, (
        f"{name}：世界 y 起点为 {min_y:.6f} 而非 0——轴向被平移后，两通道的取景与"
        "叠加比对都会整体错位"
    )
    assert abs(max_y - expected_length) < ENVELOPE_GATE * expected_length


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_accessor_local_box_is_z_up_while_world_box_is_y_up(
    tmp_path: Path, name: str, profile: MeridianProfile
) -> None:
    """钉住"必须走场景图"这件事：访问器局部是 **Z-up**，世界是 **Y-up**，两者不可混用。

    这条断言记录 M2 首项查出的缺陷根因——当初直接读访问器 min/max，得出"轴向在文件 z"，
    前端据此又转了 -90°，与节点自带的 `Rx(-90°)` 叠加成 -180°。
    ⚠ 若某天导出器改为把 Y-up 直接写进访问器（不再写节点旋转），本断言会失败——那是
    **提示重新推导契约**，不是删除本测试的理由。
    """
    target = _export(profile, tmp_path, name)
    local = _accessor_local_box(target)
    local_size = _size(local)
    world_size = _size(glb_bounding_box(target))

    envelope = validate_meridian(profile).envelope
    expected_diameter, expected_length = envelope[0], envelope[2]

    # 局部：轴向在 z、径向在 x/y
    assert relative_error(local_size[2], expected_length) < ENVELOPE_GATE
    assert relative_error(local_size[0], expected_diameter) < ENVELOPE_GATE
    assert relative_error(local_size[1], expected_diameter) < ENVELOPE_GATE

    # 世界：轴向在 y、径向在 x/z ——即局部经 Rx(-90°) 后的结果
    assert relative_error(world_size[1], expected_length) < ENVELOPE_GATE
    assert relative_error(world_size[2], expected_diameter) < ENVELOPE_GATE

    # 局部 ≠ 世界：证明 `glb_bounding_box` 走节点变换**不是**空操作
    assert relative_error(local_size[1], world_size[1]) > ENVELOPE_GATE, (
        f"{name}：访问器局部包围盒与世界包围盒一致——节点变换未生效（或导出器已改约定），"
        "请重新推导朝向契约后再更新本测试"
    )


@pytest.mark.parametrize(("name", "profile"), CASES, ids=_CASE_IDS)
def test_dual_channel_world_boxes_agree(
    tmp_path: Path, name: str, profile: MeridianProfile
) -> None:
    """R-25 的判据：两通道在**世界坐标**下的包围盒逐轴 ≤ 1%，且轴向范围一致。

    两侧数据路径彼此独立（后端采样折线 vs GLB 顶点经场景图变换），故这不是自证。
    """
    target = _export(profile, tmp_path, name)

    schematic_size, schematic_y = _schematic_world_box(profile)
    authoritative = glb_bounding_box(target)
    authoritative_size = _size(authoritative)
    authoritative_y = (authoritative[0][1], authoritative[1][1])

    for axis in (0, 1, 2):
        error = relative_error(authoritative_size[axis], schematic_size[axis])
        assert error < ENVELOPE_GATE, (
            f"{name} 世界轴 {axis}：权威通道 {authoritative_size[axis]:.6f} vs 示意通道 "
            f"{schematic_size[axis]:.6f}，误差 {error:.3e}——两通道形态分叉（R-25）"
        )

    for edge, actual, wanted in (
        ("y_min", authoritative_y[0], schematic_y[0]),
        ("y_max", authoritative_y[1], schematic_y[1]),
    ):
        error = relative_error(actual, wanted)
        assert error < ENVELOPE_GATE, (
            f"{name} 世界 {edge}：权威 {actual:.6f} vs 示意 {wanted:.6f}，误差 {error:.3e}——"
            "两通道轴向起点/终点不一致（尺寸一致但整体错位）"
        )

    # 反向自检：若把**访问器局部**包围盒（即修复前 `glb_bounding_box` 的口径）当作世界包围盒，
    # 两通道必然分叉。这一步防止上面两条断言因夹具退化而变成恒真。
    misread_size = _size(_accessor_local_box(target))
    mismatched_axes = [
        axis
        for axis in (0, 1, 2)
        if relative_error(misread_size[axis], schematic_size[axis]) >= ENVELOPE_GATE
    ]
    assert mismatched_axes, (
        f"{name}：把访问器局部包围盒当作世界包围盒时两通道竟然也一致（轴 {mismatched_axes}）——"
        "说明本夹具无法区分朝向，请换用 L / 2R 更大的构型"
    )


def test_node_matrix_helpers_apply_rotation_and_translation() -> None:
    """矩阵辅助函数的列主序语义自检——`glb_bounding_box` 的正确性完全落在这三个函数上。

    用**实测的真实四元数**（OCCT 写在根节点上的 `Rx(-90°)`）钉住映射 `(x, y, z) → (x, z, -y)`，
    并验证平移与"父 · 子"复合：`_matrix_multiply` 的复合路径在单节点 GLB 上走不到，
    只能在这里覆盖。
    """
    rotation = _node_matrix({"rotation": list(ROOT_QUATERNION)})
    point = _transform_point(rotation, (0.5, 0.5, 3.0))
    assert point == pytest.approx((0.5, 3.0, -0.5), abs=1e-12)

    assert _node_matrix({}) == [
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]
    assert _transform_point(_node_matrix({"translation": [1.0, 2.0, 3.0]}), (0.0, 0.0, 0.0)) == (
        1.0,
        2.0,
        3.0,
    )

    # 父节点带平移、子节点带旋转：复合后先转再平移（glTF 约定 M = T · R · S）
    parent = _node_matrix({"translation": [0.0, 10.0, 0.0]})
    composed = _matrix_multiply(parent, rotation)
    assert _transform_point(composed, (0.0, 0.0, 3.0)) == pytest.approx((0.0, 13.0, 0.0), abs=1e-12)
