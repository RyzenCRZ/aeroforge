"""回转体建模与产物导出（规格 §5.3 / §5.4 / §16.3）。

职责
----
把母线剖面（:mod:`aeroforge.geometry.meridian`）变为 OCCT 实体，并导出 STEP 与两级 LOD 的 GLB。

关键约束
--------
- **精确弧构边**：球冠/椭圆弧用 ``Edge.make_ellipse`` 精确构边，绝不用采样折线替代——
  否则与解析解的对照失去意义（§5.7）。
- **导出必须显式 ``unit=Unit.M``**：build123d 默认 ``Unit.MM``，漏传会使几何整体放大 1000×，
  直接触发 R-25 双通道漂移（规格 §3.2 实测结论）。
- 本模块**不持有跨调用的 OCCT 状态**；调用方（作业执行器）负责串行化（§9.1 硬规则）。
"""

from __future__ import annotations

import importlib.metadata
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import build123d as bd

from aeroforge.geometry.meridian import GEOM_TOL, ResolvedProfile, SegmentGeometry, resolve
from aeroforge.geometry.meridian import MeridianProfile as MeridianProfile

# LOD 网格参数（规格 §5.4）：LOD1 供交互预览，LOD2 供交付与精修
LOD1_DEFLECTION = 0.01
LOD1_ANGULAR = 0.5
LOD2_DEFLECTION = 0.001
LOD2_ANGULAR = 0.1

# 剖面平面：2D (u, v) → 世界 (u, 0, v)，即 r=u、z=v
PROFILE_PLANE = bd.Plane.XZ


def kernel_version() -> str:
    """几何内核版本串（参与缓存键，§16.3）。

    ⚠ 必须用**发行名** ``cadquery-ocp-novtk`` 查询：按 ``cadquery-ocp`` 查询必抛
    ``PackageNotFoundError``，而内核本身完全正常（规格 §3.2 实测结论）。
    """
    parts: list[str] = []
    for dist_name, label in (("cadquery-ocp-novtk", "occt"), ("build123d", "build123d")):
        try:
            parts.append(f"{label}={importlib.metadata.version(dist_name)}")
        except importlib.metadata.PackageNotFoundError:
            parts.append(f"{label}=unknown")
    return ";".join(parts)


# ---------------------------------------------------------------------------
# 剖面 → 实体
# ---------------------------------------------------------------------------


def _plane_at_z(z: float) -> bd.Plane:
    """把剖面平面平移至 z——弧边的圆心在轴上，故只需沿 Z 平移。"""
    return bd.Plane(origin=(0.0, 0.0, z), x_dir=(1.0, 0.0, 0.0), z_dir=(0.0, -1.0, 0.0))


def _segment_edge(geom: SegmentGeometry) -> bd.Edge:
    """由段的精确几何构出一条边（位于 XZ 剖面平面内）。"""
    if geom.kind == "line":
        r0, z0 = geom.start
        r1, z1 = geom.end
        return bd.Edge.make_line((r0, 0.0, z0), (r1, 0.0, z1))

    assert geom.center is not None
    assert geom.semi_r is not None
    assert geom.semi_z is not None
    assert geom.angle_start_deg is not None
    assert geom.angle_end_deg is not None
    return bd.Edge.make_ellipse(
        geom.semi_r,
        geom.semi_z,
        _plane_at_z(geom.center[1]),
        geom.angle_start_deg,
        geom.angle_end_deg,
    )


def profile_face(resolved: ResolvedProfile) -> bd.Face:
    """把剖面闭合成平面（含两端径向段与轴线闭合边）。

    段链使用**精确弧边**（见 :func:`_segment_edge`）；仅在两端补径向段与轴线闭合边。
    """
    chain: list[bd.Edge] = []
    if not resolved.bottom_on_axis:
        # 底端径向段：(0, 0) → (base_radius, 0)
        chain.append(bd.Edge.make_line((0.0, 0.0, 0.0), (resolved.segments[0].start[0], 0.0, 0.0)))
    chain.extend(_segment_edge(geom) for geom in resolved.segments)
    if not resolved.top_on_axis:
        # 顶端径向段：(end_radius, end_z) → (0, end_z)
        chain.append(
            bd.Edge.make_line(
                (resolved.end_radius, 0.0, resolved.end_z), (0.0, 0.0, resolved.end_z)
            )
        )
    # 轴线闭合边
    chain.append(bd.Edge.make_line((0.0, 0.0, resolved.end_z), (0.0, 0.0, 0.0)))

    wires = bd.Wire.combine(chain)
    if not wires:
        msg = "剖面构边失败：边链无法合成为线框"
        raise RuntimeError(msg)
    face = bd.Face(wires[0])
    if face is None:
        msg = "剖面无法生成平面（边链可能不自洽）"
        raise RuntimeError(msg)
    return face


def build_solid(profile: MeridianProfile) -> bd.Part:
    """母线剖面 → 回转实体（绕 Z 轴 360°）。

    ⚠ 这是**权威体**：体积 / 表面积 / 质心 / STEP 一律取它（OI-33 ④）。
    GLB 走 :func:`build_segments` 的逐段版，但两者覆盖**同一区域**，故"看到的"
    与"算的"仍是同一个外形（P1 / ADR-012）。
    """
    resolved = resolve(profile)
    face = profile_face(resolved)
    part = bd.revolve(face, axis=bd.Axis.Z)
    if part is None:
        msg = "回转建模失败：OCCT 未返回实体"
        raise RuntimeError(msg)
    return part


# ---------------------------------------------------------------------------
# 分段回转（GLB 场景图的来源，OI-33 ②）
# ---------------------------------------------------------------------------

#: 分段 GLB 的根节点名。⚠ 前端只按名找节点，改名即等于关掉分级显隐。
GLB_ROOT_NAME = "vehicle"

#: 分段节点名前缀：``seg-<i>``，``<i>`` 为 ``profile.segments`` 的 0 基下标。
GLB_SEGMENT_PREFIX = "seg-"


def segment_label(index: int) -> str:
    """第 ``index`` 段的 GLB 节点名（0 基，对应 ``profile.segments[index]``）。"""
    return f"{GLB_SEGMENT_PREFIX}{index}"


def segment_face(geom: SegmentGeometry) -> bd.Face | None:
    """单段的自闭合剖面（该段曲线 + 两端径向段 + 轴线闭合边）。

    返回 ``None`` 表示**退化段**（两端半径均为 0，回转体为空）——按 OI-33 ② 不产出节点。
    """
    start_radius, start_z = geom.start
    end_radius, end_z = geom.end
    if start_radius <= GEOM_TOL and end_radius <= GEOM_TOL:
        return None

    chain: list[bd.Edge] = [_segment_edge(geom)]
    if start_radius > GEOM_TOL:
        chain.append(bd.Edge.make_line((0.0, 0.0, start_z), (start_radius, 0.0, start_z)))
    if end_radius > GEOM_TOL:
        chain.append(bd.Edge.make_line((end_radius, 0.0, end_z), (0.0, 0.0, end_z)))
    chain.append(bd.Edge.make_line((0.0, 0.0, end_z), (0.0, 0.0, start_z)))

    wires = bd.Wire.combine(chain)
    if not wires:
        msg = f"分段剖面构边失败：段（{geom.kind}，z {start_z} → {end_z}）的边链无法合成为线框"
        raise RuntimeError(msg)
    face = bd.Face(wires[0])
    if face is None:
        msg = f"分段剖面无法生成平面（边链可能不自洽）：段（{geom.kind}，z {start_z} → {end_z}）"
        raise RuntimeError(msg)
    return face


def build_segments(profile: MeridianProfile) -> bd.Compound:
    """母线剖面 → **逐段具名**的复合体（GLB 场景图的唯一来源，OI-33 ②）。

    场景图：``vehicle``（根，**无 mesh**）→ ``seg-0`` / ``seg-1`` / … 各持一个 mesh。
    每段各自回转成实体，故段间新增的分界面在**未隐藏任何段**时被实体完全包住、
    从外部不可见——"看到的"与"算的"仍是同一个外形（P1 / ADR-012）。

    ⚠ 三条硬约束中本函数负责两条：**每个非退化段必须有节点名**（缺名 = 前端点了没反应，
    静默失效）、**根节点不得持 mesh**（否则整体网格与分段网格叠加渲染，同一处画两遍）。
    """
    resolved = resolve(profile)
    children: list[bd.Part] = []
    for index, geom in enumerate(resolved.segments):
        face = segment_face(geom)
        if face is None:
            continue
        solid = bd.revolve(face, axis=bd.Axis.Z)
        if solid is None:
            msg = f"分段回转建模失败：段 {index}（{geom.kind}），OCCT 未返回实体"
            raise RuntimeError(msg)
        solid.label = segment_label(index)
        children.append(solid)

    if not children:
        msg = "分段回转失败：全部段均退化（两端半径均为 0），未产出任何实体"
        raise RuntimeError(msg)

    root = bd.Compound(children=children)
    root.label = GLB_ROOT_NAME
    return root


# ---------------------------------------------------------------------------
# 内核量测与包络
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KernelMetrics:
    """内核求得/报告的量（SI）。"""

    volume: float
    surface_area: float
    centroid_z: float
    is_valid: bool
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    solid_count: int

    @property
    def bbox_size(self) -> tuple[float, float, float]:
        return (
            self.bbox_max[0] - self.bbox_min[0],
            self.bbox_max[1] - self.bbox_min[1],
            self.bbox_max[2] - self.bbox_min[2],
        )


def measure(part: bd.Part) -> KernelMetrics:
    """读取内核量测值。"""
    bbox = part.bounding_box()
    center = part.center(bd.CenterOf.MASS)
    solids = part.solids()
    return KernelMetrics(
        volume=part.volume,
        surface_area=part.area,
        centroid_z=center.Z,
        is_valid=bool(part.is_valid),
        bbox_min=(bbox.min.X, bbox.min.Y, bbox.min.Z),
        bbox_max=(bbox.max.X, bbox.max.Y, bbox.max.Z),
        solid_count=len(solids),
    )


def mesh_vertex_count(part: bd.Part, tolerance: float, angular_tolerance: float) -> int:
    """三角化后的顶点数（用于 LOD 规模核对）。

    ⚠ ``Shape.mesh()`` **不是**网格生成器（原地触发三角化并返回 ``None``）；
    取网格必须用 ``Shape.tessellate``（规格 §3.2 实测结论）。
    """
    vertices, _ = part.tessellate(tolerance, angular_tolerance)
    return len(vertices)


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def export_step(part: bd.Part, path: Path) -> None:
    """导出 STEP（⚠ 必须显式 ``unit=Unit.M``）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = bd.export_step(part, str(path), unit=bd.Unit.M)
    if not ok:
        msg = f"STEP 导出失败：{path}"
        raise RuntimeError(msg)


def export_glb(shape: bd.Compound, path: Path, *, deflection: float, angular: float) -> None:
    """导出二进制 GLB（⚠ 必须显式 ``unit=Unit.M``）。

    传入的 ``shape`` 决定 GLB 的**场景图**：传 :func:`build_segments` 得逐段具名节点
    （分级显隐的前提，OI-33 ②）；传 :func:`build_solid` 得单节点。本工程一律走分段版。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = bd.export_gltf(
        shape,
        str(path),
        unit=bd.Unit.M,
        binary=True,
        linear_deflection=deflection,
        angular_deflection=angular,
    )
    if not ok:
        msg = f"GLB 导出失败：{path}"
        raise RuntimeError(msg)


def _node_matrix(node: dict[str, Any]) -> list[float]:
    """节点的局部变换矩阵，**列主序 16 元素**（glTF 的 `matrix` 或 TRS 二选一）。

    glTF 约定 ``M = T · R · S``：先缩放、再旋转、最后平移。
    """
    matrix = node.get("matrix")
    if matrix is not None:
        return [float(value) for value in matrix]

    translation = [float(value) for value in node.get("translation", (0.0, 0.0, 0.0))]
    quaternion = [float(value) for value in node.get("rotation", (0.0, 0.0, 0.0, 1.0))]
    scale = [float(value) for value in node.get("scale", (1.0, 1.0, 1.0))]

    x, y, z, w = quaternion
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    # 行主序的旋转矩阵（第 row 行第 col 列 → row_major[row * 3 + col]）
    row_major = [
        1.0 - 2.0 * (yy + zz),
        2.0 * (xy - wz),
        2.0 * (xz + wy),
        2.0 * (xy + wz),
        1.0 - 2.0 * (xx + zz),
        2.0 * (yz - wx),
        2.0 * (xz - wy),
        2.0 * (yz + wx),
        1.0 - 2.0 * (xx + yy),
    ]
    # `R · S`：缩放是对角阵，等价于把 R 的第 j 列整体乘 s_j；再按列主序摊平
    return [
        row_major[0] * scale[0],
        row_major[3] * scale[0],
        row_major[6] * scale[0],
        0.0,
        row_major[1] * scale[1],
        row_major[4] * scale[1],
        row_major[7] * scale[1],
        0.0,
        row_major[2] * scale[2],
        row_major[5] * scale[2],
        row_major[8] * scale[2],
        0.0,
        translation[0],
        translation[1],
        translation[2],
        1.0,
    ]


def _matrix_multiply(parent: list[float], local: list[float]) -> list[float]:
    """列主序 4×4 相乘：``parent · local``。"""
    result = [0.0] * 16
    for column in range(4):
        for row in range(4):
            result[column * 4 + row] = sum(
                parent[k * 4 + row] * local[column * 4 + k] for k in range(4)
            )
    return result


def _transform_point(
    matrix: list[float], point: tuple[float, float, float]
) -> tuple[float, float, float]:
    """列主序矩阵左乘点（齐次坐标 w = 1），返回世界坐标。"""
    coordinates = [
        sum(matrix[column * 4 + row] * point[column] for column in range(3)) + matrix[12 + row]
        for row in range(3)
    ]
    return (coordinates[0], coordinates[1], coordinates[2])


def read_glb_json(path: Path) -> dict[str, Any]:
    """读取 GLB 的 JSON 块（场景图、网格、访问器都在此）。

    GLB = 12 字节头 + 若干块（每块 8 字节头 + 载荷，载荷按 4 字节对齐）。
    单独抽出本函数是为了让"场景图契约"（OI-33 ②：节点名 / 根节点不持 mesh）与
    包围盒核对**读同一份解析结果**，而不是各写一份二进制解析。
    """
    raw = path.read_bytes()
    if len(raw) < 12 or raw[:4] != b"glTF":
        msg = f"不是合法的 GLB 文件：{path}"
        raise RuntimeError(msg)

    offset = 12
    while offset + 8 <= len(raw):
        chunk_length = int.from_bytes(raw[offset : offset + 4], "little")
        chunk_type = raw[offset + 4 : offset + 8]
        if chunk_type == b"JSON":
            payload = raw[offset + 8 : offset + 8 + chunk_length]
            gltf: dict[str, Any] = json.loads(payload.decode("utf-8"))
            return gltf
        offset += 8 + chunk_length + (-chunk_length % 4)

    msg = f"GLB 缺少 JSON 块：{path}"
    raise RuntimeError(msg)


def glb_bounding_box(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """GLB 在**世界坐标**下的包围盒（已施加场景图的节点变换）。

    这是双通道一致性检验中**权威通道**的一侧（§13.6 / §16.3）：比对的是**渲染器实际
    看到的空间**，而非访问器里的原始数据——导出的单位错误（如漏传 ``unit=Unit.M``）
    只有在此处才暴露。

    ⚠ **必须走节点变换**：OCCT 导出器按 glTF 规范把 Z-up 转成 Y-up，方式是给根节点写一个
    ``Rx(-90°)`` 的四元数旋转，而**访问器里的 min/max 仍是 Z-up 的局部坐标**（实测本工程
    柱 r=1 L=3 的访问器为 ``x/y ∈ ±1、z ∈ [0, 3]``，节点 rotation 为 ``[-0.7071, 0, 0, 0.7071]``）。
    只读访问器会得到"轴向在 z"的错误结论，进而让前端多做一次 -90°，把模型转倒——
    这正是 M2 首项目视核对查出的缺陷，故此处按场景图求世界坐标。
    """
    gltf = read_glb_json(path)

    nodes: list[dict[str, Any]] = gltf.get("nodes", [])
    if not nodes:
        msg = f"GLB 内无节点，无法确定世界坐标：{path}"
        raise RuntimeError(msg)

    scenes: list[dict[str, Any]] = gltf.get("scenes", [])
    scene_index = gltf.get("scene")
    if isinstance(scene_index, int) and 0 <= scene_index < len(scenes):
        roots = [int(index) for index in scenes[scene_index].get("nodes", [])]
    elif scenes:
        roots = [int(index) for index in scenes[0].get("nodes", [])]
    else:
        roots = list(range(len(nodes)))

    accessors = gltf.get("accessors", [])
    identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
    lows = [math.inf, math.inf, math.inf]
    highs = [-math.inf, -math.inf, -math.inf]
    visited: set[tuple[int, int]] = set()
    positioned = False

    def visit(node_index: int, parent: list[float]) -> None:
        nonlocal positioned
        if not 0 <= node_index < len(nodes):
            return
        node = nodes[node_index]
        world_matrix = _matrix_multiply(parent, _node_matrix(node))

        mesh_index = node.get("mesh")
        if mesh_index is not None:
            for primitive in gltf.get("meshes", [])[int(mesh_index)].get("primitives", []):
                accessor_index = primitive.get("attributes", {}).get("POSITION")
                if accessor_index is None:
                    continue
                if (node_index, int(accessor_index)) in visited:
                    continue
                visited.add((node_index, int(accessor_index)))
                accessor = accessors[int(accessor_index)]
                low = accessor.get("min")
                high = accessor.get("max")
                if low is None or high is None:
                    msg = f"GLB 的 POSITION 访问器缺少 min/max，无法核对包络：{path}"
                    raise RuntimeError(msg)
                # 局部包围盒的 8 个角点各自变换后再取包围盒（旋转后不能只取 min/max 两端）
                for corner_x in (float(low[0]), float(high[0])):
                    for corner_y in (float(low[1]), float(high[1])):
                        for corner_z in (float(low[2]), float(high[2])):
                            point = _transform_point(world_matrix, (corner_x, corner_y, corner_z))
                            for axis in range(3):
                                lows[axis] = min(lows[axis], point[axis])
                                highs[axis] = max(highs[axis], point[axis])
                positioned = True

        for child in node.get("children", []):
            visit(int(child), world_matrix)

    for root in roots:
        visit(root, identity)

    if not positioned:
        msg = f"GLB 内无 POSITION 访问器：{path}"
        raise RuntimeError(msg)

    return (lows[0], lows[1], lows[2]), (highs[0], highs[1], highs[2])


def relative_error(actual: float, expected: float) -> float:
    """相对误差；期望值为零时退化为绝对误差（避免除零）。"""
    if abs(expected) <= GEOM_TOL:
        return abs(actual - expected)
    return abs(actual - expected) / abs(expected)
