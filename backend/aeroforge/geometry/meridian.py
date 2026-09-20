"""母线剖面：段模型、精确几何推算、G0/G1 校验、采样与 canonical JSON。

规格依据：§5.2（2D→3D 入口）、§5.3（曲线族）、§5.7（五重校验）、§16.3（M1 裁剪）。

设计要点（M1）
--------------
- 段类型三选一：``line``（柱/锥/锥台）、``arc``（球冠/球底）、``ellipse``（椭球底/共底）。
  三类共用 ``{length, end_radius}``：段与段**首尾相接**，故 G0 由结构保证，只需校 G1 切向。
- ``arc`` / ``ellipse`` 均为**穹顶段**：一端必落在轴线上（r=0），另一端为半径端。
  - ``ellipse``：半轴 = (半径端半径 R, 轴向跨度 H)，椭圆心在轴线上，恰为四分之一椭圆弧。
  - ``arc``：球冠，球半径**由 R、H 唯一确定** ρ = (R² + H²) / 2H，无需额外自由度。
- 所有段在 z 上单调，故回转体的体积/面积有闭式解（见 :mod:`aeroforge.geometry.analytic`），
  构成内核结果的**独立对照**（§5.7）。
- ⚠ 不得用采样折线代替精确弧：内核以精确弧构边、解析侧以闭式解求值，两者才构成真对照；
  若两侧都用同一条折线，误差趋零但**检验失效**（自证）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 几何量容差（SI，单位米）。半径判零、端点重合等一律用它，避免浮点噪声造成"伪尖点/伪退化"。
GEOM_TOL = 1e-9

# canonical JSON 的浮点位数：超出部分视为同一模型的浮点噪声（缓存键因此稳定）。
CANONICAL_DECIMALS = 9

# G1 判据：接头处两侧切向夹角上限（度）。穹顶与柱段相接时切向本就严格竖直，余量给数值噪声。
G1_MAX_ANGLE_DEG = 0.5


class MeridianError(ValueError):
    """母线剖面的域错误（几何不自洽、参数越界等）。API 层映射为 422。"""


# ---------------------------------------------------------------------------
# 段模型
# ---------------------------------------------------------------------------


class _SegmentBase(BaseModel):
    """段基类：``{length, end_radius}`` 三元组中的两元，起点由前一段决定。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    length: float = Field(description="轴向跨度（m），正 = 沿 +Z 推进")
    end_radius: float = Field(ge=0.0, description="本段结束处的半径（m）")


class LineSegment(_SegmentBase):
    """直线段：柱段（end_radius == 起点半径）或锥段/锥台。"""

    type: Literal["line"] = "line"


class ArcSegment(_SegmentBase):
    """球冠段（圆弧）：半径端半径 R 与轴向跨度 H 唯一确定球半径 ρ=(R²+H²)/2H。"""

    type: Literal["arc"] = "arc"


class EllipseSegment(_SegmentBase):
    """椭圆弧段：半轴 = (半径端半径 R, 轴向跨度 H)，椭圆心在轴线上。"""

    type: Literal["ellipse"] = "ellipse"


Segment = Annotated[LineSegment | ArcSegment | EllipseSegment, Field(discriminator="type")]


class MeridianProfile(BaseModel):
    """母线剖面：底部半径 + 自下而上的段链（规格 §5.2）。

    剖面在 (r, z) 平面内描述，回转轴为 Z 轴，单位米。
    实体 = 区域 ``{(r, z)}`` 绕 Z 轴回转 360°，其中区域由
    ``(0,0) → (base_radius,0) → [段链] → (r_end,z_end) → (0,z_end) → (0,0)`` 围成。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(default="", description="剖面名称（仅用于展示，不参与几何）")
    base_radius: float = Field(ge=0.0, description="z=0 处的半径（m）")
    segments: tuple[Segment, ...] = Field(min_length=1, description="自下而上的段链")

    @model_validator(mode="after")
    def _check_total(self) -> MeridianProfile:
        for index, seg in enumerate(self.segments):
            if seg.length <= GEOM_TOL:
                msg = f"段 {index}（{seg.type}）的 length 必须为正，得到 {seg.length}"
                raise MeridianError(msg)
        return self


# ---------------------------------------------------------------------------
# 段的精确几何推算
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SegmentGeometry:
    """一段的精确几何：端点、切向、以及弧段的圆心/半轴/起止角（度）。

    切向为单位矢量 ``(dr, dz)``，方向与剖面推进方向（自下而上）一致。
    直线段的弧参数为 ``None``。
    """

    kind: Literal["line", "arc", "ellipse"]
    start: tuple[float, float]
    end: tuple[float, float]
    start_tangent: tuple[float, float]
    end_tangent: tuple[float, float]
    center: tuple[float, float] | None = None
    semi_r: float | None = None
    semi_z: float | None = None
    angle_start_deg: float | None = None
    angle_end_deg: float | None = None

    @property
    def is_dome(self) -> bool:
        return self.kind in ("arc", "ellipse")


def _unit(vector: tuple[float, float]) -> tuple[float, float]:
    norm = math.hypot(*vector)
    if norm <= GEOM_TOL:
        msg = f"退化切向 {vector}"
        raise MeridianError(msg)
    return (vector[0] / norm, vector[1] / norm)


def _resolve_line(start_r: float, start_z: float, seg: LineSegment) -> SegmentGeometry:
    end_r = seg.end_radius
    end_z = start_z + seg.length
    if abs(end_r - start_r) <= GEOM_TOL:
        # 柱段：切向严格沿 +Z
        tangent = (0.0, 1.0)
        return SegmentGeometry(
            kind="line",
            start=(start_r, start_z),
            end=(end_r, end_z),
            start_tangent=tangent,
            end_tangent=tangent,
        )
    tangent = _unit((end_r - start_r, seg.length))
    return SegmentGeometry(
        kind="line",
        start=(start_r, start_z),
        end=(end_r, end_z),
        start_tangent=tangent,
        end_tangent=tangent,
    )


def _resolve_dome(
    start_r: float,
    start_z: float,
    seg: ArcSegment | EllipseSegment,
) -> SegmentGeometry:
    """推算穹顶段：一端必须落在轴线上（r=0），另一端为半径端。"""
    end_r = seg.end_radius
    length = seg.length

    start_on_axis = start_r <= GEOM_TOL
    end_on_axis = end_r <= GEOM_TOL
    if start_on_axis == end_on_axis:
        msg = (
            f"{seg.type} 段是穹顶段，两端中**恰有一端**必须落在轴线上（r=0）；"
            f"实际起点 r={start_r}、终点 r={end_r}。"
            "若两端均不在轴线上，请改用 line 段（锥台）或在该段前后补足穹顶。"
        )
        raise MeridianError(msg)

    radius_r = end_r if start_on_axis else start_r  # 半径端的半径 R
    if radius_r <= GEOM_TOL:
        msg = f"{seg.type} 段的半径端半径必须为正，得到 {radius_r}"
        raise MeridianError(msg)

    if start_on_axis:
        # 反向穹顶：自轴线向外张开（共底内底、倒置底）
        angle_start, angle_end = -90.0, 0.0
        if seg.type == "ellipse":
            semi_r, semi_z = radius_r, length
            center = (0.0, start_z + length)
        else:
            rho = (radius_r**2 + length**2) / (2.0 * length)
            semi_r = semi_z = rho
            center = (0.0, start_z + rho)
            angle_end = math.degrees(math.atan2(length - rho, radius_r))
    else:
        # 正向穹顶：自半径端收拢到轴线（球底、椭球底）
        angle_start, angle_end = 0.0, 90.0
        if seg.type == "ellipse":
            semi_r, semi_z = radius_r, length
            center = (0.0, start_z)
        else:
            rho = (radius_r**2 + length**2) / (2.0 * length)
            semi_r = semi_z = rho
            center = (0.0, start_z + length - rho)
            angle_start = math.degrees(math.atan2(rho - length, radius_r))

    def point_at(angle_deg: float) -> tuple[float, float]:
        theta = math.radians(angle_deg)
        return (center[0] + semi_r * math.cos(theta), center[1] + semi_z * math.sin(theta))

    def tangent_at(angle_deg: float) -> tuple[float, float]:
        theta = math.radians(angle_deg)
        return _unit((-semi_r * math.sin(theta), semi_z * math.cos(theta)))

    geom = SegmentGeometry(
        kind=seg.type,
        start=point_at(angle_start),
        end=point_at(angle_end),
        start_tangent=tangent_at(angle_start),
        end_tangent=tangent_at(angle_end),
        center=center,
        semi_r=semi_r,
        semi_z=semi_z,
        angle_start_deg=angle_start,
        angle_end_deg=angle_end,
    )

    # 自检：推算出的端点必须与声明一致（否则是公式错误，不是输入问题）
    for label, expected, actual in (
        ("起点", (start_r, start_z), geom.start),
        ("终点", (end_r, start_z + length), geom.end),
    ):
        if math.hypot(expected[0] - actual[0], expected[1] - actual[1]) > 1e-6:
            msg = (
                f"{seg.type} 段{label}推算不自洽：声明 {expected}、推算 {actual}。"
                "这是几何公式缺陷，请上报。"
            )
            raise MeridianError(msg)
    return geom


def resolve_segment(start_r: float, start_z: float, seg: Segment) -> SegmentGeometry:
    """把一段（连同其起点）推算为精确几何。"""
    if isinstance(seg, LineSegment):
        return _resolve_line(start_r, start_z, seg)
    return _resolve_dome(start_r, start_z, seg)


# ---------------------------------------------------------------------------
# 剖面求值
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """已推算的剖面：段几何 + 首尾状态 + 供前端使用的闭合轮廓。"""

    profile: MeridianProfile
    segments: tuple[SegmentGeometry, ...]
    end_radius: float
    end_z: float
    bottom_on_axis: bool
    top_on_axis: bool

    @property
    def total_length(self) -> float:
        return self.end_z

    @property
    def max_radius(self) -> float:
        return max([self.profile.base_radius, *(max(s.start[0], s.end[0]) for s in self.segments)])

    def closed_outline(self) -> list[tuple[float, float]]:
        """闭合轮廓（含轴线段与两端径向段），供 2D 剖面绘制与前端车削网格使用。

        顺序：自轴线底部出发 → 半径端 → 段链 → 顶部 → 回轴线。
        前端可直接把它交给 ``LatheGeometry``（其端点落在轴上即得闭合曲面），
        从而**不做任何几何计算**（ADR-011）。
        """
        points: list[tuple[float, float]] = []
        if not self.bottom_on_axis:
            points.append((0.0, 0.0))
        points.append(self.segments[0].start)
        for seg in self.segments:
            points.append(seg.end)
        if not self.top_on_axis:
            points.append((0.0, self.end_z))
        if points[0] != (0.0, 0.0):
            points.insert(0, (0.0, 0.0))
        return points

    def segment_outlines(self, max_step_deg: float = 5.0) -> list[list[tuple[float, float]]]:
        """**逐段**闭合轮廓（OI-33 ③），与 :meth:`closed_outline` 同粒度拆分。

        每段一组点，自轴线起、回轴线止（两端半径为正时各补一条径向段），
        故前端可对每组点各建一个 ``LatheGeometry``，使**示意通道与权威通道同粒度**：
        隐藏某段时两侧都少画同一段。数值全部来自后端，前端仍是零几何计算（ADR-011）。

        ⚠ **下标必须与 ``segments`` 一一对应**：退化段（两端半径均为 0，回转体为空）
        返回**空列表**而非跳过——跳过会让后续下标整体前移，前端按 `seg-<i>` 隐藏时
        就会隐藏**相邻的另一段**（静默错位）。
        """
        outlines: list[list[tuple[float, float]]] = []
        for geom in self.segments:
            start_radius, start_z = geom.start
            end_radius, end_z = geom.end
            if start_radius <= GEOM_TOL and end_radius <= GEOM_TOL:
                outlines.append([])
                continue
            points: list[tuple[float, float]] = []
            if start_radius > GEOM_TOL:
                points.append((0.0, start_z))
            points.extend(sample_segment(geom, max_step_deg))
            if end_radius > GEOM_TOL:
                points.append((0.0, end_z))
            outlines.append(points)
        return outlines


def resolve(profile: MeridianProfile) -> ResolvedProfile:
    """推算整条剖面：逐段链接并校验端点连续性（G0 应结构性成立）。"""
    kernels: list[SegmentGeometry] = []
    current_r = profile.base_radius
    current_z = 0.0
    for seg in profile.segments:
        geom = resolve_segment(current_r, current_z, seg)
        kernels.append(geom)
        current_r, current_z = geom.end

    # G0 结构性自检：段的声明终态与推算终点必须一致
    for index, geom in enumerate(kernels):
        declared = (
            profile.segments[index].end_radius,
            kernels[index].start[1] + profile.segments[index].length,
        )
        if math.hypot(declared[0] - geom.end[0], declared[1] - geom.end[1]) > 1e-6:
            msg = f"段 {index} 端点不连续：声明 {declared}、推算 {geom.end}"
            raise MeridianError(msg)

    return ResolvedProfile(
        profile=profile,
        segments=tuple(kernels),
        end_radius=current_r,
        end_z=current_z,
        bottom_on_axis=profile.base_radius <= GEOM_TOL,
        top_on_axis=current_r <= GEOM_TOL,
    )


# ---------------------------------------------------------------------------
# 采样（供前端渲染；权威几何仍由精确弧决定）
# ---------------------------------------------------------------------------


def sample_segment(geom: SegmentGeometry, max_step_deg: float = 5.0) -> list[tuple[float, float]]:
    """按角度步长采样一段，返回含起止点的折线（米）。

    采样的用途仅为**前端渲染**与 2D 剖面绘制；解析对照与内核建模均使用精确弧，
    故此处精度不影响 §5.7 的检验有效性。
    """
    if not geom.is_dome:
        return [geom.start, geom.end]
    assert geom.center is not None
    assert geom.semi_r is not None
    assert geom.semi_z is not None
    assert geom.angle_start_deg is not None
    assert geom.angle_end_deg is not None
    span = geom.angle_end_deg - geom.angle_start_deg
    steps = max(2, math.ceil(abs(span) / max_step_deg))
    points: list[tuple[float, float]] = []
    for index in range(steps + 1):
        angle = math.radians(geom.angle_start_deg + span * index / steps)
        points.append(
            (
                geom.center[0] + geom.semi_r * math.cos(angle),
                geom.center[1] + geom.semi_z * math.sin(angle),
            )
        )
    return points


def sample_profile(
    resolved: ResolvedProfile, max_step_deg: float = 5.0
) -> list[tuple[float, float]]:
    """采样整条剖面（自下而上，不含轴线闭合段）。"""
    points: list[tuple[float, float]] = []
    for index, geom in enumerate(resolved.segments):
        chunk = sample_segment(geom, max_step_deg)
        points.extend(chunk if index == 0 else chunk[1:])
    return points


# ---------------------------------------------------------------------------
# canonical JSON（缓存键与往返一致判据，规格 §16.3）
# ---------------------------------------------------------------------------


def round_floats(value: object) -> object:
    """把模型序列化结果里的浮点统一量化（供 canonical JSON 使用）。

    本工程有两处 canonical JSON（剖面 :func:`canonical_json` 与参数层
    :func:`aeroforge.params.schema.canonical_json`），共用这一个量化器——
    两份字节形态若各自漂移，缓存键与往返判据就会分叉（P1 单一真相源）。
    """
    if isinstance(value, float):
        rounded = round(value, CANONICAL_DECIMALS)
        # 规整 -0.0 → 0.0，否则同一模型会因符号位得到两个缓存键
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, dict):
        return {key: round_floats(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [round_floats(item) for item in value]
    return value


def canonical_json(profile: MeridianProfile) -> str:
    """剖面的 canonical JSON：键序固定、浮点定量、无多余空白。

    同一几何模型必须得到**逐字节相同**的字符串——它同时是：
    - 母线保存/重载的往返一致判据（§16.3 验收项 2）
    - 缓存键的输入（§16.3）

    ⚠ 序列化覆盖**全部字段（含 display-only 的 ``name``）**：这是刻意的取舍——
    只保留一种规范化形态，避免"几何键"与"存档形态"两份 canonical 各自漂移。
    代价是重命名剖面会使缓存失效（多一次重建），远小于两份形态不一致的风险。
    """
    payload = round_floats(profile.model_dump(mode="json", exclude_none=True))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_profile(raw: str) -> MeridianProfile:
    """自 canonical JSON 还原剖面；无法还原即抛 :class:`MeridianError`。"""
    try:
        return MeridianProfile.model_validate_json(raw)
    except Exception as exc:
        msg = f"母线剖面解析失败：{exc}"
        raise MeridianError(msg) from exc
