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


class TangentOgiveSegment(_SegmentBase):
    """切线卵形段（§5.3 曲线族，M5）：头锥（工程常用）。

    由 ``{length, end_radius}`` **完全确定**：基底半径 R = 半径端半径、锥长 L = length，
    长径比 = L/(2R) 为派生量；卵形圆半径 ρ = (R²+L²)/(2R)，圆心在 (R−ρ, L)——基底切向
    严格竖直，故与柱段 G1 连续（这正是"切线"卵形的含义）。穹顶型放置（恰一端在轴线上）。
    ⚠ 值域：L ≥ R——过钝（L < R）的切线卵形弧会下探到负 z，破坏剖面的 z 单调性。
    """

    type: Literal["ogive"] = "ogive"


class ParabolaSegment(_SegmentBase):
    """抛物线头锥段（§5.3 曲线族，M5）：头锥（低阻）。

    母线 ``r(t) = R·(2t − K·t²)/(2−K)``，``t = z/L`` ∈ [0, 1]，K 为抛物线系数：
    K = 1 全抛物线（基底切向竖直，与柱段 G1）；K → 0 退化为锥。穹顶型放置。
    """

    type: Literal["parabola"] = "parabola"
    coefficient: float = Field(
        gt=0.0,
        le=1.0,
        description="抛物线系数 K（1 = 全抛物线，基底相切；趋 0 = 锥形）",
    )


class VonKarmanSegment(_SegmentBase):
    """冯·卡门头锥段（§5.3 曲线族，M5）：跨声速最优头锥（LDHV）。

    Haack 级 C=0 特例（标准 LDHV 近似式）::

        φ = arccos(1 − 2t),  r(t) = (R/√π)·√(φ − sin(2φ)/2)

    顶端切向水平（钝头）、基底切向竖直（与柱段 G1）。穹顶型放置。
    """

    type: Literal["von_karman"] = "von_karman"


class PowerLawSegment(_SegmentBase):
    """幂律过渡段（§5.3 曲线族，M5）：通用过渡。

    母线 ``r(t) = r₀ + (r₁ − r₀)·tⁿ``：n = 1 直线（锥/锥台）；n < 1 起始切向水平
    （钝过渡）；n > 1 起始切向竖直。非穹顶型——两端半径任意（通用过渡用途）。
    """

    type: Literal["power"] = "power"
    exponent: float = Field(gt=0.0, le=10.0, description="幂律指数 n")


class BellNozzleSegment(_SegmentBase):
    """钟形喷管段（§5.3 曲线族，M5）：Rao 抛物线近似钟形。

    参数化（Rao 型）：喉部半径 ``throat_radius``、出口半径（段另一端的半径）、
    长度比 ``length_ratio`` = L / L₁₅°（L₁₅° = (Rₑ−Rₜ)/tan15°，0.8 = 80% 钟形）。
    型面 = 喉部圆弧（半径 0.382·Rₜ，Rao 经典值）+ 二次 Bézier 抛物线（起端壁角 θₙ、
    出口壁角 θₑ 由膨胀比 ε = (Rₑ/Rₜ)² 的对数拟合工程近似给出）。

    ⚠ 两端中**恰有一端**为喉部（半径 = throat_radius），另一端为出口（必须更大）；
    ``length`` 与 ``length_ratio`` 必须自洽（意图断言：L = ratio·(Rₑ−Rₜ)/tan15°）。
    """

    type: Literal["bell"] = "bell"
    throat_radius: float = Field(gt=0.0, description="喉部半径（m）")
    length_ratio: float = Field(
        gt=0.0, le=1.5, description="钟形长度比 L/L₁₅°（0.8 = 80% 钟，Rao 常用值）"
    )


class SplineSegment(_SegmentBase):
    """样条段（§5.3 曲线族，M5）：自定义 / 逆向。

    控制点为**内部节点**（不含两端）：曲线 = 自然三次样条插值
    [起点, *控制点, 终点]，以 z 为参数（r(z) 单值、z 严格单调由结构保证）。
    控制点的 z 必须严格递增且落在 (0, length) 内；r ≥ 0。
    """

    type: Literal["spline"] = "spline"
    control_points: tuple[tuple[float, float], ...] = Field(
        min_length=1, description="内部控制点 (r, z) 列表（z 严格递增，落在 (0, length) 内）"
    )

    @model_validator(mode="after")
    def _check_control_points(self) -> SplineSegment:
        previous = 0.0
        for index, (radius, z) in enumerate(self.control_points):
            if radius < -GEOM_TOL:
                msg = f"样条段控制点 {index} 的半径为负（{radius}）——回转体不可穿越轴线"
                raise MeridianError(msg)
            if not previous < z < self.length:
                msg = (
                    f"样条段控制点 {index} 的 z={z} 必须严格递增且落在 (0, {self.length}) 内"
                    "（z 单调是 r(z) 单值的结构前提）"
                )
                raise MeridianError(msg)
            previous = z
        return self


Segment = Annotated[
    LineSegment
    | ArcSegment
    | EllipseSegment
    | TangentOgiveSegment
    | ParabolaSegment
    | VonKarmanSegment
    | PowerLawSegment
    | BellNozzleSegment
    | SplineSegment,
    Field(discriminator="type"),
]


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


#: 段种类全集：M1 三段型（line/arc/ellipse）+ M5 曲线族六种。
SegmentKind = Literal[
    "line",
    "arc",
    "ellipse",
    "ogive",
    "parabola",
    "von_karman",
    "power",
    "bell",
    "spline",
]

#: 曲线族采样点数（含两端）：供前端渲染、GLB 样条构边与解析积分共用同一份求值器。
#: 解析侧用**精确参数式**积分（quad），内核侧用穿过这批点的 B 样条构边——两侧
# . 算法无关，仍构成 §5.7 要求的独立对照。
CURVE_SAMPLE_COUNT = 65


@dataclass(frozen=True, slots=True)
class CurveSpec:
    """曲线族段的求值规格（M5）：local 参数式 + 链内放置。

    local 坐标系：各族的标准形（ogive/parabola/von_karman：尖在 (0,0) → 基底 (R, L)；
    bell：喉部 (Rₜ, 0) → 出口 (Rₑ, L)；power/spline：起点 (r₀, 0) → 终点 (r₁, L)）。
    链内放置：``z = z0 + z_local``（正向）或 ``z = z0 + L − z_local``（reverse），
    ``r = r_local``。``params`` 为各族固定次序的参数元组（见 :func:`curve_point`）。
    """

    kind: str
    z0: float
    length: float
    reverse: bool
    params: tuple[float, ...]


def curve_point(spec: CurveSpec, t: float) -> tuple[float, float]:
    """曲线族 local 参数式的链内坐标（``t ∈ [0, 1]``，沿链推进方向）。"""
    local_t = 1.0 - t if spec.reverse else t
    r, z = _curve_local(spec.kind, spec.params, local_t)
    if spec.reverse:
        return (r, spec.z0 + spec.length - z)
    return (r, spec.z0 + z)


def _curve_local(kind: str, params: tuple[float, ...], t: float) -> tuple[float, float]:
    """曲线族 local 参数式（各族标准形，t 沿 local 正向）。"""
    if kind == "ogive":
        radius_r, length, c_r, rho, psi_tip = params
        psi = psi_tip * (1.0 - t)
        return (c_r + rho * math.cos(psi), length + rho * math.sin(psi))
    if kind == "parabola":
        radius_r, length, coefficient = params
        return (
            radius_r * (2.0 * t - coefficient * t * t) / (2.0 - coefficient),
            length * t,
        )
    if kind == "von_karman":
        radius_r, length = params
        phi = math.acos(max(-1.0, min(1.0, 1.0 - 2.0 * t)))
        shape = phi - math.sin(2.0 * phi) / 2.0
        return (radius_r / math.sqrt(math.pi) * math.sqrt(max(0.0, shape)), length * t)
    if kind == "power":
        r0, r1, length, exponent = params
        return (r0 + (r1 - r0) * t**exponent, length * t)
    if kind == "bell":
        (
            _throat,
            _exit,
            length,
            theta_n,
            _theta_e,
            arc_radius,
            q_r,
            q_z,
            p1_r,
            p1_z,
            t_q,
        ) = params
        if t <= t_q:
            phi = theta_n * (t / t_q if t_q > 0.0 else 0.0)
            return (params[0] + arc_radius * (1.0 - math.cos(phi)), arc_radius * math.sin(phi))
        span = 1.0 - t_q
        s = (t - t_q) / span if span > 0.0 else 1.0
        u = 1.0 - s
        r = u * u * q_r + 2.0 * s * u * p1_r + s * s * params[1]
        z = u * u * q_z + 2.0 * s * u * p1_z + s * s * length
        return (r, z)
    if kind == "spline":
        count = int(params[0])
        knots = params[1 : 1 + count]
        values = params[1 + count :]
        z = knots[-1] * t  # 末节点即 local 长度（控制点 z 严格落在 (0, L) 内）
        return (_spline_eval(knots, values, z), z)
    msg = f"未知曲线族 {kind!r}"
    raise MeridianError(msg)


def _natural_cubic_second_derivatives(
    knots: tuple[float, ...], values: tuple[float, ...]
) -> tuple[float, ...]:
    """自然三次样条的节点二阶导数（Mᵢ，端点为 0）——Thomas 追赶法解三对角方程。

    以 z 为参数：``r(z)`` 在节点间为三次多项式，全局 C²。纯 Python 实现，
    供母线层（无内核）求得样条段的端点切向与采样。
    """
    count = len(knots)
    if count < 2:
        msg = "样条至少需要两个节点"
        raise MeridianError(msg)
    if count == 2:
        return (0.0, 0.0)
    n = count - 1
    h = [knots[i + 1] - knots[i] for i in range(n)]
    sub = [0.0] * (n + 1)  # 下对角
    diag = [2.0] * (n + 1)  # 主对角
    sup = [0.0] * (n + 1)  # 上对角
    rhs = [0.0] * (n + 1)
    for i in range(1, n):
        sub[i] = h[i - 1]
        diag[i] = 2.0 * (h[i - 1] + h[i])
        sup[i] = h[i]
        rhs[i] = 6.0 * ((values[i + 1] - values[i]) / h[i] - (values[i] - values[i - 1]) / h[i - 1])
    # Thomas 前消后回
    for i in range(1, n + 1):
        factor = sub[i] / diag[i - 1] if diag[i - 1] != 0.0 else 0.0
        diag[i] -= factor * sup[i - 1]
        rhs[i] -= factor * rhs[i - 1]
    m = [0.0] * (count)
    m[n] = rhs[n] / diag[n] if diag[n] != 0.0 else 0.0
    for i in range(n - 1, 0, -1):
        m[i] = (rhs[i] - sup[i] * m[i + 1]) / diag[i] if diag[i] != 0.0 else 0.0
    m[0] = 0.0
    return tuple(m)


def _spline_interval(knots: tuple[float, ...], z: float) -> int:
    """z 所属的节点区间下标（z 已夹到 [knots[0], knots[-1]]）。"""
    for i in range(len(knots) - 1):
        if knots[i] <= z <= knots[i + 1]:
            return i
    return len(knots) - 2


def _spline_eval(knots: tuple[float, ...], values: tuple[float, ...], z: float) -> float:
    """自然三次样条在 z 处的 r 值（z 以 local 坐标给出，落在 [0, L]）。

    区间内公式（a = (x_{i+1}−z)/h、b = (z−x_i)/h，a+b=1）::

        S(z) = a·rᵢ + b·rᵢ₊₁ − (h²/6)·a·b·[(1+a)·Mᵢ + (1+b)·Mᵢ₊₁]
    """
    z = min(max(z, knots[0]), knots[-1])
    index = _spline_interval(knots, z)
    h = knots[index + 1] - knots[index]
    m = _spline_m(knots, values)
    a = (knots[index + 1] - z) / h
    b = 1.0 - a
    return (
        values[index] * a
        + values[index + 1] * b
        - a * b * h * h / 6.0 * ((1.0 + a) * m[index] + (1.0 + b) * m[index + 1])
    )


def _spline_slope(knots: tuple[float, ...], values: tuple[float, ...], z: float) -> float:
    """自然三次样条在 z 处的 dr/dz（端点切向用）。

    ``S'(z) = (rᵢ₊₁−rᵢ)/h − (h/6)·(Mᵢ₊₁−Mᵢ) − (Mᵢ·a² − Mᵢ₊₁·b²)·h/2``。
    """
    z = min(max(z, knots[0]), knots[-1])
    index = _spline_interval(knots, z)
    h = knots[index + 1] - knots[index]
    m = _spline_m(knots, values)
    a = (knots[index + 1] - z) / h
    b = 1.0 - a
    return (
        (values[index + 1] - values[index]) / h
        - h / 6.0 * (m[index + 1] - m[index])
        - (m[index] * a * a - m[index + 1] * b * b) * h / 2.0
    )


#: 样条二阶导数缓存（冻结元组可哈希；段链复用同一样条时避免重复解三对角方程）。
_SPLINE_CACHE: dict[tuple[tuple[float, ...], tuple[float, ...]], tuple[float, ...]] = {}


def _spline_m(knots: tuple[float, ...], values: tuple[float, ...]) -> tuple[float, ...]:
    """节点二阶导数（带缓存）。"""
    cached = _SPLINE_CACHE.get((knots, values))
    if cached is None:
        cached = _natural_cubic_second_derivatives(knots, values)
        _SPLINE_CACHE[(knots, values)] = cached
    return cached


@dataclass(frozen=True, slots=True)
class SegmentGeometry:
    """一段的精确几何：端点、切向、以及弧段的圆心/半轴/起止角（度）。

    切向为单位矢量 ``(dr, dz)``，方向与剖面推进方向（自下而上）一致。
    直线段的弧参数为 ``None``。曲线族段（M5）另带：``points``（含两端的密集采样，
    供 GLB 样条构边与前端渲染）与 ``curve``（参数式求值规格，供解析积分——与内核
    的 B 样条构边构成 §5.7 的独立对照）。
    """

    kind: SegmentKind
    start: tuple[float, float]
    end: tuple[float, float]
    start_tangent: tuple[float, float]
    end_tangent: tuple[float, float]
    center: tuple[float, float] | None = None
    semi_r: float | None = None
    semi_z: float | None = None
    angle_start_deg: float | None = None
    angle_end_deg: float | None = None
    points: tuple[tuple[float, float], ...] | None = None
    curve: CurveSpec | None = None

    @property
    def is_dome(self) -> bool:
        """穹顶型段（一端在轴线上、钝头收拢）：pinch 例外判定的域。"""
        return self.kind in ("arc", "ellipse", "ogive", "parabola", "von_karman")


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
    if isinstance(seg, (ArcSegment, EllipseSegment)):
        return _resolve_dome(start_r, start_z, seg)
    return _resolve_curve(start_r, start_z, seg)


# ---------------------------------------------------------------------------
# 曲线族（M5，§5.3）：local 参数式 + 链内放置 + 精确端点切向 + 密集采样
# ---------------------------------------------------------------------------

#: Rao 抛物线近似的喉部下游圆弧半径系数（0.382·Rₜ，Rao 经典值）。
_BELL_THROAT_ARC_FACTOR = 0.382

#: 80% 钟形壁角的对数拟合（Rao 图表的工程近似，非查表精确值）：
#: θₙ = 20.5° + 3.8°·ln(ε)、θₑ = 14.2° − 1.9°·ln(ε)，ε = (Rₑ/Rₜ)²。
#: 锚点：ε=10 → θₙ≈29.3°/θₑ≈9.8°；ε=4 → 25.7°/11.6°（与公开 Rao 图表量级一致）。
_BELL_THETA_N_BASE_DEG = 20.5
_BELL_THETA_N_LOG_DEG = 3.8
_BELL_THETA_E_BASE_DEG = 14.2
_BELL_THETA_E_LOG_DEG = 1.9


def _bell_wall_angles_deg(expansion_ratio: float) -> tuple[float, float]:
    """Rao 80% 钟形的起端/出口壁角（度，对数拟合工程近似）。"""
    theta_n = _BELL_THETA_N_BASE_DEG + _BELL_THETA_N_LOG_DEG * math.log(expansion_ratio)
    theta_e = _BELL_THETA_E_BASE_DEG - _BELL_THETA_E_LOG_DEG * math.log(expansion_ratio)
    return (min(40.0, max(20.0, theta_n)), min(16.0, max(4.0, theta_e)))


def _place_tangent(tangent: tuple[float, float], reverse: bool) -> tuple[float, float]:
    """local 切向 → 链内切向。

    reverse 放置是 **z 镜像 + 参数反转**（链内 z 恒向上）：链内切向 = (−dr, +dz)——
    r 分量取负（内收/外张互换），z 分量不变（链推进方向恒为 +z）。
    """
    if reverse:
        return (-tangent[0], tangent[1])
    return tangent


def _finish_curve(
    seg: Segment,
    kind: str,
    start: tuple[float, float],
    end: tuple[float, float],
    start_tangent_local: tuple[float, float],
    end_tangent_local: tuple[float, float],
    spec: CurveSpec,
) -> SegmentGeometry:
    """组装曲线族段的几何：链内端点/切向 + 密集采样 + 单调性自检。"""
    reverse = spec.reverse
    points = tuple(
        curve_point(spec, index / CURVE_SAMPLE_COUNT) for index in range(CURVE_SAMPLE_COUNT + 1)
    )
    geometry = SegmentGeometry(
        kind=kind,  # type: ignore[arg-type]
        start=start,
        end=end,
        start_tangent=_unit(_place_tangent(start_tangent_local, reverse)),
        end_tangent=_unit(_place_tangent(end_tangent_local, reverse)),
        points=points,
        curve=spec,
    )
    # 端点自检（G0 结构前提，公式错误应在此暴露）
    for label, expected, actual in (
        ("起点", start, points[0]),
        ("终点", end, points[-1]),
    ):
        if math.hypot(expected[0] - actual[0], expected[1] - actual[1]) > 1e-9:
            msg = (
                f"{kind} 段{label}推算不自洽：声明 {expected}、推算 {actual}。"
                "这是几何公式缺陷，请上报。"
            )
            raise MeridianError(msg)
    # z 单调 + 半径非负（r(z) 单值与回转体不自交的结构前提）
    for index in range(1, len(points)):
        _r_prev, z_prev = points[index - 1]
        r_next, z_next = points[index]
        if z_next <= z_prev - 1e-12:
            msg = f"{kind} 段采样在 z={z_next:.9f} 处非单调——母线必须是 z 的单值函数"
            raise MeridianError(msg)
        if r_next < -1e-9:
            msg = f"{kind} 段采样在 z={z_next:.9f} 处出现负半径 {r_next:.9f}——回转体不可穿越轴线"
            raise MeridianError(msg)
    return geometry


def _resolve_curve(start_r: float, start_z: float, seg: Segment) -> SegmentGeometry:
    """曲线族段的统一入口：判方向 → 建 local 参数式 → 放置到链内。"""
    end_r = seg.end_radius
    length = seg.length
    start = (start_r, start_z)
    end = (end_r, start_z + length)

    if isinstance(seg, TangentOgiveSegment | ParabolaSegment | VonKarmanSegment):
        start_on_axis = start_r <= GEOM_TOL
        end_on_axis = end_r <= GEOM_TOL
        if start_on_axis == end_on_axis:
            msg = (
                f"{seg.type} 段是头锥段（穹顶型放置），两端中**恰有一端**必须落在轴线上（r=0）；"
                f"实际起点 r={start_r}、终点 r={end_r}。"
                "若为过渡段请改用 power / spline 段。"
            )
            raise MeridianError(msg)
        radius_r = end_r if start_on_axis else start_r
        if radius_r <= GEOM_TOL:
            msg = f"{seg.type} 段的基底半径必须为正，得到 {radius_r}"
            raise MeridianError(msg)
        reverse = end_on_axis  # 链内 base→tip 时反向遍历 local（tip→base）
        spec = _build_head_spec(seg, radius_r, length, start_z, reverse)
        tip_tangent, base_tangent = _head_tangents(seg, radius_r, length)
        if reverse:
            # 链起点 = 基底、链终点 = 尖端 → 切向配对随放置互换
            tip_tangent, base_tangent = base_tangent, tip_tangent
        return _finish_curve(seg, seg.type, start, end, tip_tangent, base_tangent, spec)

    if isinstance(seg, PowerLawSegment):
        params = (start_r, end_r, length, seg.exponent)
        spec = CurveSpec(kind="power", z0=start_z, length=length, reverse=False, params=params)
        n = seg.exponent
        if n < 1.0:
            tangent_start = (1.0, 0.0)
        elif n > 1.0:
            tangent_start = (0.0, 1.0)
        else:
            tangent_start = (end_r - start_r, length)
        tangent_end = (n * (end_r - start_r), length)
        return _finish_curve(seg, "power", start, end, tangent_start, tangent_end, spec)

    if isinstance(seg, BellNozzleSegment):
        return _resolve_bell(start_r, start_z, seg)

    if isinstance(seg, SplineSegment):
        knots = (0.0, *(z for _, z in seg.control_points), length)
        values = (start_r, *(r for r, _ in seg.control_points), end_r)
        spline_params: tuple[float, ...] = (float(len(knots)), *knots, *values)
        spec = CurveSpec(
            kind="spline", z0=start_z, length=length, reverse=False, params=spline_params
        )
        slope_start = _spline_slope(knots, values, 0.0)
        slope_end = _spline_slope(knots, values, length)
        return _finish_curve(
            seg,
            "spline",
            start,
            end,
            (slope_start, 1.0),
            (slope_end, 1.0),
            spec,
        )

    msg = f"未实现的曲线族段：{type(seg).__name__}"
    raise MeridianError(msg)


def _build_head_spec(
    seg: TangentOgiveSegment | ParabolaSegment | VonKarmanSegment,
    radius_r: float,
    length: float,
    start_z: float,
    reverse: bool,
) -> CurveSpec:
    """头锥族（ogive/parabola/von_karman）的 local 参数式规格。"""
    params: tuple[float, ...]
    if isinstance(seg, TangentOgiveSegment):
        if length < radius_r - GEOM_TOL:
            msg = (
                f"ogive 段的锥长 {length} m 小于基底半径 {radius_r} m（长径比过钝）——"
                "此时切线卵形弧会下探到负 z，破坏母线的 z 单调性"
            )
            raise MeridianError(msg)
        rho = (radius_r**2 + length**2) / (2.0 * radius_r)
        c_r = (radius_r**2 - length**2) / (2.0 * radius_r)
        psi_tip = math.atan2(-length, -c_r) if c_r != 0.0 else -math.pi / 2.0
        params = (radius_r, length, c_r, rho, psi_tip)
    elif isinstance(seg, ParabolaSegment):
        params = (radius_r, length, seg.coefficient)
    else:
        params = (radius_r, length)
    return CurveSpec(kind=seg.type, z0=start_z, length=length, reverse=reverse, params=params)


def _head_tangents(
    seg: TangentOgiveSegment | ParabolaSegment | VonKarmanSegment,
    radius_r: float,
    length: float,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """头锥族的 local 端点切向（t=0 尖端、t=1 基底）。"""
    if isinstance(seg, TangentOgiveSegment):
        rho = (radius_r**2 + length**2) / (2.0 * radius_r)
        c_r = (radius_r**2 - length**2) / (2.0 * radius_r)
        psi_tip = math.atan2(-length, -c_r) if c_r != 0.0 else -math.pi / 2.0
        # dP/dψ = (−ρ·sinψ, ρ·cosψ)；ψ_tip 处取正方向的切向（远离尖端）
        tip = (-rho * math.sin(psi_tip), rho * math.cos(psi_tip))
        if tip[1] < 0.0:
            tip = (-tip[0], -tip[1])
        return tip, (0.0, 1.0)
    if isinstance(seg, ParabolaSegment):
        coefficient = seg.coefficient
        tip = (2.0 * radius_r / (2.0 - coefficient), length)
        base = (2.0 * radius_r * (1.0 - coefficient) / (2.0 - coefficient), length)
        return tip, base
    return (1.0, 0.0), (0.0, 1.0)


def _resolve_bell(start_r: float, start_z: float, seg: BellNozzleSegment) -> SegmentGeometry:
    """钟形喷管段：判喉部方向 → Rao 近似型面 → 一致性意图断言。"""
    throat = seg.throat_radius
    length = seg.length
    start_is_throat = abs(start_r - throat) <= GEOM_TOL
    end_is_throat = abs(seg.end_radius - throat) <= GEOM_TOL
    if start_is_throat == end_is_throat:
        msg = (
            f"bell 段两端中**恰有一端**必须是喉部（r = throat_radius = {throat}）；"
            f"实际起点 r={start_r}、终点 r={seg.end_radius}。"
            "喷管段从喉部张开到出口，两端不能同为喉部或同非喉部。"
        )
        raise MeridianError(msg)
    exit_radius = seg.end_radius if start_is_throat else start_r
    if exit_radius <= throat + GEOM_TOL:
        msg = f"bell 段出口半径 {exit_radius} 必须大于喉部半径 {throat}（扩张段）"
        raise MeridianError(msg)

    # 意图断言（§5.7）：长度与长度比必须自洽——L = ratio·(Rₑ−Rₜ)/tan15°
    derived_length = seg.length_ratio * (exit_radius - throat) / math.tan(math.radians(15.0))
    if abs(length - derived_length) > 1e-6 * max(1.0, derived_length):
        msg = (
            f"bell 段长度 {length:.9f} m 与长度比 {seg.length_ratio} 不自洽："
            f"按 L = ratio·(Rₑ−Rₜ)/tan15° 应为 {derived_length:.9f} m（意图断言，§5.7）。"
            "请以长度比反算长度，或调整长度比。"
        )
        raise MeridianError(msg)

    expansion = (exit_radius / throat) ** 2
    theta_n_deg, theta_e_deg = _bell_wall_angles_deg(expansion)
    theta_n = math.radians(theta_n_deg)
    theta_e = math.radians(theta_e_deg)
    arc_radius = _BELL_THROAT_ARC_FACTOR * throat
    q = (throat + arc_radius * (1.0 - math.cos(theta_n)), arc_radius * math.sin(theta_n))
    if q[1] >= length - GEOM_TOL:
        msg = (
            f"bell 段抛物线区间为负：喉部圆弧占据 z ∈ [0, {q[1]:.6f}]，"
            f"而钟形总长仅 {length:.6f} m——请增大长度比或减小喉部半径"
        )
        raise MeridianError(msg)
    # 抛物线（二次 Bézier）：起端切向 (sinθₙ, cosθₙ)、出口切向 (sinθₑ, cosθₑ)
    direction_n = (math.sin(theta_n), math.cos(theta_n))
    direction_e = (math.sin(theta_e), math.cos(theta_e))
    # P1 = 两条切线的交点：Q + t·dₙ = E − s·dₑ（E = (Rₑ, L)，坐标序 (r, z)）
    determinant = direction_n[0] * (-direction_e[1]) - direction_n[1] * (-direction_e[0])
    if abs(determinant) <= GEOM_TOL:
        msg = "bell 段抛物线切向平行，无法构造 Bézier 控制点"
        raise MeridianError(msg)
    delta = (exit_radius - q[0], length - q[1])
    t_parameter = (delta[0] * (-direction_e[1]) - delta[1] * (-direction_e[0])) / determinant
    p1 = (q[0] + t_parameter * direction_n[0], q[1] + t_parameter * direction_n[1])

    reverse = not start_is_throat  # 链内出口→喉部时反向遍历 local（喉部→出口）
    params = (
        throat,
        exit_radius,
        length,
        theta_n,
        theta_e,
        arc_radius,
        q[0],
        q[1],
        p1[0],
        p1[1],
        q[1] / length,
    )
    spec = CurveSpec(kind="bell", z0=start_z, length=length, reverse=reverse, params=params)
    throat_tangent = (0.0, 1.0)  # 喉部：壁面与轴平行
    exit_tangent = (math.sin(theta_e), math.cos(theta_e))  # 出口壁角 θₑ
    if reverse:
        # 链起点 = 出口、链终点 = 喉部 → 切向配对随放置互换
        throat_tangent, exit_tangent = exit_tangent, throat_tangent
    return _finish_curve(
        seg,
        "bell",
        (start_r, start_z),
        (seg.end_radius, start_z + length),
        throat_tangent,
        exit_tangent,
        spec,
    )


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
        """剖面最大半径（m）。

        端点半径与曲线族**采样内点**一并取最大——样条段可在内段鼓出超过两端
        （头锥/钟形族母线 r 单调，内点不会超过端点，扫描它们无副作用）。
        """
        candidates: list[float] = [self.profile.base_radius]
        for segment in self.segments:
            candidates.append(max(segment.start[0], segment.end[0]))
            if segment.points is not None:
                candidates.append(max(r for r, _ in segment.points))
        return max(candidates)

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
    故此处精度不影响 §5.7 的检验有效性。曲线族段（M5）返回构建期已生成的密集采样
    （:data:`CURVE_SAMPLE_COUNT` 点，含两端）——与 GLB 样条构边共用同一份点列。
    """
    if geom.points is not None:
        return list(geom.points)
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
