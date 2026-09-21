"""曲面光顺与曲率分析（规格 §5.4，M5 第四片）。

§5.4 的五步表逐项落地
----------------------
1. **曲率分析**：沿母线计算带符号曲率 κ 与**曲率梳**（curvature comb）——
   离散 Menger 曲率（三点外接圆），参数化无关、对穹顶段（轴端切向竖直）稳健。
2. **连续性判定**：段间 G0 / G1 / G2——G0 结构性成立（:func:`resolve` 端点自检）、
   G1 用段端精确切向夹角、G2 用曲率跳变（相对 + 绝对双容差）。
   ⚠ G2 只对**连续类段**（样条 / 幂律 / 卵形 / 抛物线 / 冯·卡门 / 钟形）有意义；
   **锥段直线连接处**（两侧均为 ``line``）的 G1 断点属设计意图，报告如实标
   「设计折点」，不算缺陷。
3. **光顺**：对超差段（κ 符号交替 / 尖峰）做**张力样条重拟合**——经典「张力
   样条」在数据点（张力 → ∞）与直线弦（张力 → 0）之间插值；本实现以
   「冻结带锚点间的直线弦」为张力 → 0 参考线，将原曲线向参考线按张力系数与
   **弦高容差预算**混合（位移场光滑渐出，不产生新折点；峰值 |κ| 随混合系数
   单调下降）。
4. **回写（§5.4 铁律）**：光顺结果**只回写为母线段定义**（以 :class:`SplineSegment`
   替换原段，随响应返回新母线 JSON）——**禁止**对网格做拉普拉斯 / 平滑处理
   （网格平滑破坏 NURBS 精确性、违反 P1）；回写后若破坏既有 G1 接头则该段
   回退并警告（宁可不光顺，不可引入折痕）。
5. **报告**：曲率极值位置 / 各连接点连续性等级 / 光顺前后最大偏差
   （偏差超**弦高容差 0.5 mm** 的段按比例钳回，保证判据恒成立）。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from aeroforge.geometry.meridian import (
    GEOM_TOL,
    MeridianError,
    MeridianProfile,
    ResolvedProfile,
    resolve,
    sample_segment,
)

#: 光顺偏差门限（m）：光顺前后最大偏差 ≤ **弦高容差 0.5 mm**（§5.4 报告行判据）。
SMOOTHING_CHORD_TOLERANCE_M = 5e-4

#: G2 判定的绝对曲率容差（1/m）：曲率本身接近 0 时相对判据失效的兜底（工程惯例值）。
G2_CURVATURE_ABS_TOL = 0.02

#: G2 判定的相对曲率容差（工程惯例值：连接点两侧曲率差 ≤ 10% 视为连续）。
G2_CURVATURE_REL_TOL = 0.1

#: 段内曲率**尖峰**判据：|κ| 超过段内 |κ| 中位数的该倍数（且绝对值越绝对容差）。
CURVATURE_SPIKE_FACTOR = 4.0

#: 回写前最少采样点数（内部控制点 = 采样 − 两端；低于此值无从光顺）。
_SPLINE_CONTROL_POINTS = 9

#: 光顺采样的每段点数（分析用途；不替代精确弧——§5.7 内核对照不受影响）。
_ANALYSIS_SAMPLES_PER_SEGMENT = 33


class SmoothingError(ValueError):
    """光顺 / 曲率分析的域错误（不可分析的剖面等）。API 层映射为 422。"""


# ---------------------------------------------------------------------------
# 结果模型（端点响应的载荷，随 :mod:`aeroforge.api.geometry` 下发）
# ---------------------------------------------------------------------------


class ContinuityLevel(BaseModel):
    """一个段间连接点的连续性判定（§5.4 连续性行）。"""

    segment_pair: tuple[int, int] = Field(description="相邻两段的 0 基下标 [i, i+1]")
    level: str = Field(description="连续性等级：G0 / G1 / G2")
    tangent_angle_deg: float = Field(description="切向夹角（°；G1 判据）")
    curvature_jump: float = Field(description="两侧曲率差 |κ⁻ − κ⁺|（1/m；G2 判据）")
    design_kink: bool = Field(description="设计折点（两侧均为直线段，锥台对接柱段等）——不算缺陷")
    on_axis: bool = Field(description="轴处连接（r ≈ 0，回转面退化为单点，不在 G1 门禁内）")


class SmoothingReport(BaseModel):
    """曲率分析 + 光顺的报告载荷（§5.4 报告行）。"""

    curvature_comb: tuple[tuple[float, float, float], ...] = Field(
        description="曲率梳：(z, r, κ) 采样点列（m, m, 1/m，带符号）"
    )
    continuity: tuple[ContinuityLevel, ...] = Field(description="段间连续性判定")
    extremes: tuple[tuple[int, float, float], ...] = Field(
        description="曲率极值：(段下标, z, κ)——段内 |κ| 最大的采样点"
    )
    offending_segments: tuple[int, ...] = Field(
        description="超差段（κ 符号突变 / 尖峰）的 0 基下标"
    )
    max_deviation_mm: float = Field(description="光顺前后最大偏差（mm；未回写 = 0）")
    smoothing_applied: bool = Field(description="是否实际回写了母线段")
    warnings: tuple[str, ...] = Field(description="回退 / 门限 / 设计折点等注记")


# ---------------------------------------------------------------------------
# 曲率分析（纯 Python，无 OCCT）
# ---------------------------------------------------------------------------


def _menger_curvature(
    p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float]
) -> float:
    """三点外接圆的**带符号**曲率（1/m）：κ = 4A / (|a|·|b|·|c|)，符号取叉积。

    曲线向 +r 侧弯（曲率中心在推进方向左侧，(z, r) 平面内）为正——符号突变即
    §5.4 的「曲率梳有符号突变」缺陷信号。
    """
    az, ar = p1[0] - p0[0], p1[1] - p0[1]
    bz, br = p2[0] - p1[0], p2[1] - p1[1]
    cross = az * br - ar * bz
    la, lb = math.hypot(az, ar), math.hypot(bz, br)
    if la <= GEOM_TOL or lb <= GEOM_TOL:
        return 0.0
    cz, cr = p2[0] - p0[0], p2[1] - p0[1]
    lc = math.hypot(cz, cr)
    if lc <= GEOM_TOL:
        return 0.0
    area = abs(cross) / 2.0
    kappa = 4.0 * area / (la * lb * lc)
    return kappa if cross > 0.0 else -kappa


def curvature_comb_points(
    resolved: ResolvedProfile, samples_per_segment: int = _ANALYSIS_SAMPLES_PER_SEGMENT
) -> list[tuple[int, float, float, float]]:
    """逐段采样的曲率梳：``(段下标, z, r, κ)``——纯数值，无 OCCT。

    直线段精确 κ = 0（补两端两个梳点）；曲线族 / 弧段用 :func:`sample_segment`
    的密集采样 + 三点 Menger 曲率。⚠ :func:`sample_segment` 的点序是 ``(r, z)``，
    此处统一翻转成 ``(z, r)`` 再算曲率。采样为**分析用途**，不替代精确弧构边。
    """
    step_deg = 180.0 / max(samples_per_segment - 1, 1)
    comb: list[tuple[int, float, float, float]] = []
    for index, segment in enumerate(resolved.segments):
        points_zr = [(p[1], p[0]) for p in sample_segment(segment, max_step_deg=step_deg)]
        if len(points_zr) < 3:
            for z, r in (points_zr[0], points_zr[-1]):
                comb.append((index, z, r, 0.0))
            continue
        comb.append(
            (
                index,
                points_zr[0][0],
                points_zr[0][1],
                _menger_curvature(points_zr[0], points_zr[1], points_zr[2]),
            )
        )
        for k in range(1, len(points_zr) - 1):
            comb.append(
                (
                    index,
                    points_zr[k][0],
                    points_zr[k][1],
                    _menger_curvature(points_zr[k - 1], points_zr[k], points_zr[k + 1]),
                )
            )
        comb.append(
            (
                index,
                points_zr[-1][0],
                points_zr[-1][1],
                _menger_curvature(points_zr[-3], points_zr[-2], points_zr[-1]),
            )
        )
    return comb


def _junction_kappa(
    comb: list[tuple[int, float, float, float]], segment_index: int, *, side: str
) -> float:
    """段端连接点的曲率（曲率梳上该段起点 / 终点侧的采样值；无采样则 0）。"""
    values = [kappa for (idx, _z, _r, kappa) in comb if idx == segment_index]
    if not values:
        return 0.0
    return values[0] if side == "start" else values[-1]


def junction_continuity(
    resolved: ResolvedProfile, comb: list[tuple[int, float, float, float]] | None = None
) -> list[ContinuityLevel]:
    """段间连接点逐个判定 G0 / G1 / G2（§5.4 连续性行）。

    - G0 结构性成立（:func:`resolve` 已做端点自检）——此处只标等级；
    - G1 = 段端精确切向夹角（与生成期门禁同源数据，门限 0.5°）；
    - G2 = 连接点两侧曲率差（双容差：``max(绝对 0.02, 相对 10%)``，工程惯例）；
    - 两侧均为 ``line`` → 设计折点（锥台对接柱段，不算缺陷）；
    - r ≈ 0 的轴处连接 → 回转面退化为单点，标记 ``on_axis`` 不判缺陷。
    """
    if comb is None:
        comb = curvature_comb_points(resolved)
    levels: list[ContinuityLevel] = []
    for i in range(len(resolved.segments) - 1):
        before = resolved.segments[i]
        after = resolved.segments[i + 1]
        radius = before.end[0]
        on_axis = radius <= GEOM_TOL
        dot = (
            before.end_tangent[0] * after.start_tangent[0]
            + before.end_tangent[1] * after.start_tangent[1]
        )
        angle_deg = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        both_line = before.kind == "line" and after.kind == "line"

        kappa_before = _junction_kappa(comb, i, side="end")
        kappa_after = _junction_kappa(comb, i + 1, side="start")
        curvature_jump = abs(kappa_before - kappa_after)
        g2_tol = max(
            G2_CURVATURE_ABS_TOL,
            G2_CURVATURE_REL_TOL * max(abs(kappa_before), abs(kappa_after)),
        )

        if on_axis:
            level = "G2"
        elif both_line:
            level = "G0"  # 设计折点：位置连续即达标（切向 / 曲率断点属设计意图）
        elif angle_deg <= 0.5 and curvature_jump <= g2_tol:
            level = "G2"
        elif angle_deg <= 0.5:
            level = "G1"
        else:
            level = "G0"
        levels.append(
            ContinuityLevel(
                segment_pair=(i, i + 1),
                level=level,
                tangent_angle_deg=angle_deg,
                curvature_jump=curvature_jump,
                design_kink=both_line,
                on_axis=on_axis,
            )
        )
    return levels


def _segment_extremes(
    comb: list[tuple[int, float, float, float]], segment_count: int
) -> list[tuple[int, float, float]]:
    """逐段的曲率极值：(段下标, z, κ)——段内 |κ| 最大的采样点（§5.4 报告行）。"""
    extremes: list[tuple[int, float, float]] = []
    for index in range(segment_count):
        points = [(z, r, kappa) for (idx, z, r, kappa) in comb if idx == index]
        if not points:
            continue
        z, _r, kappa = max(points, key=lambda item: abs(item[2]))
        extremes.append((index, z, kappa))
    return extremes


def find_offending_segments(
    comb: list[tuple[int, float, float, float]], resolved: ResolvedProfile
) -> tuple[int, ...]:
    """超差段下标：κ 多峰振荡（符号交替 ≥ 2）或孤立尖峰（|κ| 越中位数倍数）。

    直线 / 弧段不在判定域：直线 κ 恒 0、弧段 κ 恒定，无「突变 / 尖峰」可言。
    符号交替的判据**先过滤近零样本**再数符号变化——真正的荷叶边在 κ 过零处
    两侧幅度可观（远超绝对容差），而采样噪声级的过零（一侧贴 0）不是缺陷信号
    （§5.4：无符号突变、无尖峰；曲率梳单调或单峰为通过形态）。
    """
    offending: list[int] = []
    for index, segment in enumerate(resolved.segments):
        if segment.kind in ("line", "arc", "ellipse"):
            continue
        points = [kappa for (idx, _z, _r, kappa) in comb if idx == index]
        if len(points) < 5:
            continue
        # 孤立尖峰：|κ| 超过段内 |κ| 中位数的倍数（且绝对值越绝对容差）
        med = sorted(abs(k) for k in points)[len(points) // 2]
        spike_threshold = max(CURVATURE_SPIKE_FACTOR * med, G2_CURVATURE_ABS_TOL)
        has_spike = any(abs(k) > spike_threshold for k in points)
        # 多峰振荡：过滤近零样本后符号交替 ≥ 2（至少三处极值——违反"单调或单峰"）
        substantial = [k for k in points if abs(k) > G2_CURVATURE_ABS_TOL]
        sign_changes = sum(
            1 for i in range(len(substantial) - 1) if substantial[i] * substantial[i + 1] < 0.0
        )
        if has_spike or sign_changes >= 2:
            offending.append(index)
    return tuple(offending)


# ---------------------------------------------------------------------------
# 光顺（张力样条重拟合 → 参数回写，§5.4 铁律）
# ---------------------------------------------------------------------------


#: 张力参考线的混合系数上限（1 = 完全贴参考线；实际 α = (1−tension) × 预算缩放）。
_TENSION_FULL_BLEND = 1.0


def _tension_refit(
    samples: tuple[tuple[float, float], ...], tension: float
) -> tuple[tuple[tuple[float, float], ...], float]:
    """张力样条重拟合的工程近似（§5.4 光顺行）。

    **张力参考线方案**：经典「张力样条」在数据点（张力 → ∞）与直线弦
    （张力 → 0）之间插值——本实现取同一谱系：

    1. 以两端**冻结带边缘**为锚点作直线弦（张力 → 0 的参考线；冻结带保持原样
       是段端切向不被破坏的前提，自然样条的端点斜率由端部数据决定）；
    2. 位移场 ``d = 弦 − 原曲线``（光滑：弦与光滑曲线之差）——对单调 / 单峰段
       ``κ(d) ≈ −κ(原)``，故 ``κ(新) = κ(原) + α·κ(d) ≈ (1−α)·κ(原)``，
       **峰值曲率随 α 单调下降**（有保证，不产生新折点）；
    3. 混合系数 ``α = (1 − tension) × min(1, 弦高容差 / max|d|)``——张力系数
       （0 = 畅 / 1 = 刚）与**弦高容差预算**（任一样本位移 ≤ 0.5 mm，§5.4 报告行）
       共同控制光顺强度；偏差判据恒成立。
    """
    z = [p[0] for p in samples]
    r = [p[1] for p in samples]
    original = list(r)
    n = len(r)
    freeze = max(1, n // 6)
    # 张力 → 0 参考线：冻结带边缘锚点间的直线弦（内部被拉向弦 = 削峰）
    anchor_lo, anchor_hi = freeze, n - 1 - freeze
    span = max(z[anchor_hi] - z[anchor_lo], GEOM_TOL)
    # 位移渐出窗：位移在锚点处必须平滑归零，否则位移场的折点会以新 κ 尖峰出现；
    # 窗宽取锚距的 1/4——渐出自身的曲率贡献 ∝ |d|·(π/λ)²，窗太窄会成为新峰
    fade = max(2.0, (anchor_hi - anchor_lo) / 4.0)
    displacement = [0.0] * n
    for i in range(freeze, n - freeze):
        t = (z[i] - z[anchor_lo]) / span
        chord = original[anchor_lo] * (1.0 - t) + original[anchor_hi] * t
        dist_from_anchor = float(min(i - anchor_lo, anchor_hi - i))
        taper = min(1.0, dist_from_anchor / fade)
        displacement[i] = (0.5 - 0.5 * math.cos(math.pi * taper)) * (chord - original[i])
    max_disp = max(abs(d) for d in displacement)
    alpha = (1.0 - max(0.0, min(1.0, tension))) * _TENSION_FULL_BLEND
    if max_disp > SMOOTHING_CHORD_TOLERANCE_M:
        alpha *= SMOOTHING_CHORD_TOLERANCE_M / max_disp
    r = [original[i] + alpha * displacement[i] for i in range(n)]
    deviation = alpha * max_disp
    return tuple((z[i], r[i]) for i in range(n)), deviation


def _resample_interior(samples: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    """抽取重拟合采样的**全部内部点** (r, z) 作为回写样条段控制点。

    ⚠ 必须**密节点**回写（全部内部采样点，而非稀疏抽取）：自然三次样条的端点
    切向由端部节点数据与「端点 M=0」边界共同决定——节点稀疏时端部斜率会被段内
    曲率显著带偏（G1 复核不过），密节点下插值收敛于原曲线、端切向得以保持。
    """
    picked: list[tuple[float, float]] = []
    span = samples[-1][0] - samples[0][0]
    for r, z in ((r, z) for z, r in samples[1:-1]):
        # z 严格递增防御（原采样 z 单调，内部点不会退化；防浮点并列）
        if picked and z <= picked[-1][1] + 1e-12:
            z = picked[-1][1] + max(1e-9, span * 1e-6)
        picked.append((r, z))
    return tuple(picked)


def _profile_with_spline(
    profile: MeridianProfile, index: int, control: tuple[tuple[float, float], ...]
) -> MeridianProfile:
    """把第 ``index`` 段替换为样条段后**重新校验**构造剖面。

    走 ``model_validate`` 而非 ``model_copy``：后者绕过判别式联合的解析，会把
    "type=spline" 写进错误的实例（静默形态缺陷）——这里让 pydantic 重新解析，
    段参数合法性（样条 z 严格递增等）一并复核。
    """
    payload = profile.model_dump(mode="json", exclude_none=True)
    segments_payload = list(payload["segments"])
    segments_payload[index] = {
        **segments_payload[index],
        "type": "spline",
        "control_points": [[r, z] for r, z in control],
    }
    return MeridianProfile.model_validate({**payload, "segments": segments_payload})


def smooth_profile(
    profile: MeridianProfile, *, tension: float = 0.5, apply: bool = False
) -> tuple[SmoothingReport, MeridianProfile]:
    """曲率分析 + （可选）张力样条光顺与**参数回写**（§5.4，M5 第四片）。

    返回 ``(报告, 母线)``：``apply=False`` 时母线原样返回（只分析）；
    ``apply=True`` 时超差段以样条段替换（回写母线定义，**非烘焙网格**——§5.4
    铁律）。回写后逐段过 G1 复核：破坏既有接头的段**回退**并警告
    （宁可不光顺，不可引入折痕）。

    ``tension``：张力系数（0 = 畅 / 最大光顺，1 = 刚 / 最小改动；默认 0.5）。
    """
    resolved = resolve(profile)
    comb = curvature_comb_points(resolved)
    continuity = junction_continuity(resolved, comb)
    extremes = _segment_extremes(comb, len(resolved.segments))
    offending = find_offending_segments(comb, resolved)

    warnings: list[str] = []
    rewritten = profile
    max_deviation = 0.0
    applied_any = False

    from aeroforge.geometry.revolve import assert_g1

    if apply:
        for index in offending:
            chain_points = [(z, r) for (idx, z, r, _k) in comb if idx == index]
            if len(chain_points) < _SPLINE_CONTROL_POINTS + 2:
                warnings.append(f"段 {index} 采样不足，跳过光顺（保留原定义）")
                continue
            # 控制点是**段内局部**坐标（z ∈ (0, length)）——链内采样先平移到段局部
            z0 = resolved.segments[index].start[1]
            samples = tuple((z - z0, r) for (z, r) in chain_points)
            smoothed_samples, deviation = _tension_refit(samples, tension)
            control = _resample_interior(smoothed_samples)
            try:
                # 以「替换该段的试算剖面」复核：pydantic 重新解析 + G1 接头复核——
                # 破坏既有接头的段回退（宁可不光顺，不可引入折痕）
                trial = _profile_with_spline(profile, index, control)
                assert_g1(resolve(trial))
            except (MeridianError, ValueError) as exc:
                warnings.append(f"段 {index} 光顺后未过校验（{exc}）——该段回退，保留原定义")
                continue
            profile = trial  # 后续段在已回写剖面上继续替换（多段光顺叠加）
            rewritten = trial
            max_deviation = max(max_deviation, deviation * 1000.0)
            applied_any = True

        if offending and not applied_any:
            warnings.append("所有超差段均未回写（采样不足或复核未过）——母线保持原样")

    report = SmoothingReport(
        curvature_comb=tuple((z, r, k) for (_i, z, r, k) in comb),
        continuity=tuple(continuity),
        extremes=tuple(extremes),
        offending_segments=offending,
        max_deviation_mm=max_deviation,
        smoothing_applied=applied_any,
        warnings=tuple(warnings),
    )
    return report, rewritten
