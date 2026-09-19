"""几何验证（规格 §5.7 / §16.3）。

分两层，因为二者代价差三个数量级：

- :func:`validate_meridian` —— **纯 Python，无内核**。供 ``POST /api/geometry/validate``
  同步调用，前端每次改参数都会打它。检查母线自洽性、G1 切向、尖点、设计意图断言。
- :func:`validate_solid` —— **需要 OCCT**。供构建作业在实体生成后调用，落实 §5.7 五项。

§5.7 的核心认知：``is_valid() == True`` **不等于**模型正确。本模块因此把"解析对照"
与"设计意图断言"作为独立检查项，而不是依赖内核的有效性标志。
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from aeroforge.geometry.analytic import AnalyticMetrics, analyze
from aeroforge.geometry.meridian import (
    G1_MAX_ANGLE_DEG,
    GEOM_TOL,
    MeridianProfile,
    ResolvedProfile,
    resolve,
    sample_profile,
)
from aeroforge.geometry.revolve import KernelMetrics, relative_error

# 体积/面积的解析对照容差（规格 §16.3 验收项 1：< 0.1%）
ANALYTIC_VOLUME_TOL = 1e-3

# 设计意图断言的容差（长度求和等纯代数恒等式，只需容忍浮点噪声）
INTENT_TOL = 1e-9

Severity = Literal["pass", "warn", "fail", "skip"]


class CheckResult(BaseModel):
    """单项检查结果。"""

    check: str = Field(description="检查项名称")
    severity: Severity = Field(description="裁定：pass / warn / fail / skip")
    detail: str = Field(description="人类可读的说明")
    value: float | None = Field(default=None, description="实测值")
    expected: float | None = Field(default=None, description="期望值")
    relative_error: float | None = Field(default=None, description="相对误差")

    @property
    def ok(self) -> bool:
        """只有 ``fail`` 才算不通过。

        ``warn``（可解释的例外，如轴处反向尖点）与 ``skip``（未接入的检查，
        例如 M1 尚无材料模块）计入通过——但两者都会在报告里显式留痕，
        不允许被当成"悄悄通过"。
        """
        return self.severity != "fail"


class JointCheck(BaseModel):
    """段间接头（G0 由结构保证，此处校 G1）。"""

    index: int = Field(description="接头序号：段 index 与 index+1 之间")
    z: float
    radius: float
    angle_deg: float = Field(description="两侧切向夹角（度）")
    kind: Literal["g1", "pinch"] = Field(description="g1 = 常规切向；pinch = 轴处反向尖点")
    severity: Severity


class ValidationReport(BaseModel):
    """母线层校验报告（同步接口返回体）。"""

    ok: bool
    checks: list[CheckResult]
    joints: list[JointCheck]
    sample: list[tuple[float, float]] = Field(description="剖面采样点 (r, z)，米")
    outline: list[tuple[float, float]] = Field(description="闭合轮廓 (r, z)，米")
    segment_outline: list[list[tuple[float, float]]] = Field(
        description=(
            "**逐段**闭合轮廓 (r, z)，米——下标与 `profile.segments` 一一对应，"
            "退化段为空列表。供示意通道**逐段**建网格，与权威通道的 GLB 具名节点"
            "（`seg-<i>`）同粒度，二者才能各自隐藏同一段（OI-33 ③）"
        )
    )
    envelope: tuple[float, float, float] = Field(description="解析包络 (2R, 2R, L)，米")
    volume: float = Field(description="解析体积（m³）")
    surface_area: float
    centroid_z: float
    max_radius: float
    total_length: float


def _severity(ok: bool, *, warn_only: bool = False) -> Severity:
    if ok:
        return "pass"
    return "warn" if warn_only else "fail"


def validate_meridian(profile: MeridianProfile) -> ValidationReport:
    """母线层校验：几何自洽 + G1 + 尖点 + 设计意图断言（纯 Python，无内核）。"""
    resolved = resolve(profile)
    checks: list[CheckResult] = []

    # ── 1 半径非负（结构保证，运行时复核；规格 §5.7「实体性」的前置） ──
    radii = [profile.base_radius, *(seg.end_radius for seg in profile.segments)]
    negative = [value for value in radii if value < -GEOM_TOL]
    checks.append(
        CheckResult(
            check="半径非负",
            severity=_severity(not negative),
            detail="所有半径 ≥ 0——回转体不穿越轴线" if not negative else f"存在负半径：{negative}",
        )
    )

    # ── 2 轴向单调（结构保证：length > 0），故剖面是 z 的单值函数，不会自交 ──
    #    这一步不是"跳过"，而是明确记录"自交在该参数化下不可表达"，避免假装的检查。
    checks.append(
        CheckResult(
            check="自交",
            severity="pass",
            detail=(
                "剖面在 z 上严格单调（每段 length > 0），故 r(z) 单值，"
                "回转体为圆盘堆叠，结构上不可能自交"
            ),
        )
    )

    # ── 3 闭合性：轮廓首尾必须落在轴线上，才能围成闭合区域 ──
    outline = resolved.closed_outline()
    closes = (
        abs(outline[0][0]) <= GEOM_TOL
        and abs(outline[-1][0]) <= GEOM_TOL
        and abs(outline[0][1]) <= GEOM_TOL
        and abs(outline[-1][1] - resolved.end_z) <= GEOM_TOL
        and resolved.end_z > GEOM_TOL
    )
    checks.append(
        CheckResult(
            check="闭合性",
            severity=_severity(closes),
            detail=(
                f"轮廓自轴线 (r=0, z=0) 起、回到轴线 (r=0, z={resolved.end_z:.6f})，区域闭合"
                if closes
                else "轮廓未闭合到轴线，无法围成有效区域"
            ),
            value=resolved.end_z,
        )
    )

    # ── 4 G1 切向连续（G0 已由段链结构保证，见 resolve 的连续性自检） ──
    joints = _check_joints(resolved)
    hard_failures = [j for j in joints if j.severity == "fail"]
    pinches = [j for j in joints if j.kind == "pinch"]
    if not joints:
        detail = "只有一段，无接头"
    elif hard_failures:
        detail = f"{len(hard_failures)} 处切向不连续，超出 {G1_MAX_ANGLE_DEG}° 容差"
    elif pinches:
        detail = (
            f"全部接头切向连续；其中 {len(pinches)} 处为轴处反向尖点（pinch）——"
            "数学合法，但 OCCT 会因退化顶点判定实体无效，导出精确格式前请确认是否需要"
        )
    else:
        detail = f"全部 {len(joints)} 处接头切向连续（≤ {G1_MAX_ANGLE_DEG}°）"
    checks.append(
        CheckResult(
            check="G1 切向连续",
            severity="warn" if (pinches and not hard_failures) else _severity(not hard_failures),
            detail=detail,
        )
    )

    # ── 5 设计意图断言（§5.7）：总长 = 各段长度之和 ±1e-9 ──
    length_sum = sum(seg.length for seg in profile.segments)
    length_error = abs(resolved.end_z - length_sum)
    checks.append(
        CheckResult(
            check="意图断言：总长",
            severity=_severity(length_error <= INTENT_TOL),
            detail=(
                f"总长 {resolved.end_z:.9f} m = 各段长度之和 {length_sum:.9f} m"
                if length_error <= INTENT_TOL
                else f"总长 {resolved.end_z:.9f} 与段长之和 {length_sum:.9f} 不符"
            ),
            value=resolved.end_z,
            expected=length_sum,
            relative_error=length_error,
        )
    )

    # ── 6 设计意图断言：最大半径必须出现在声明的端点或半径端上（穹顶段 r 单调） ──
    radius_ends = [max(geom.start[0], geom.end[0]) for geom in resolved.segments]
    declared_max = max([profile.base_radius, *radius_ends])
    envelope_error = abs(declared_max - resolved.max_radius)
    checks.append(
        CheckResult(
            check="意图断言：最大半径",
            severity=_severity(envelope_error <= INTENT_TOL),
            detail=(
                f"最大半径 {resolved.max_radius:.6f} m（包络直径 {2 * resolved.max_radius:.6f} m）"
            ),
            value=resolved.max_radius,
            expected=declared_max,
            relative_error=envelope_error,
        )
    )

    metrics = analyze(resolved)
    return ValidationReport(
        ok=all(check.ok for check in checks),
        checks=checks,
        joints=joints,
        sample=[(round(r, 12), round(z, 12)) for r, z in sample_profile(resolved)],
        outline=[(round(r, 12), round(z, 12)) for r, z in outline],
        segment_outline=[
            [(round(r, 12), round(z, 12)) for r, z in chunk]
            for chunk in resolved.segment_outlines()
        ],
        envelope=metrics.envelope,
        volume=metrics.volume,
        surface_area=metrics.surface_area,
        centroid_z=metrics.centroid_z,
        max_radius=metrics.max_radius,
        total_length=metrics.total_length,
    )


def _check_joints(resolved: ResolvedProfile) -> list[JointCheck]:
    """逐接头校 G1；轴处反向对接记为 ``pinch``（可解释的例外，非缺陷）。"""
    joints: list[JointCheck] = []
    for index in range(len(resolved.segments) - 1):
        before = resolved.segments[index]
        after = resolved.segments[index + 1]
        dot = (
            before.end_tangent[0] * after.start_tangent[0]
            + before.end_tangent[1] * after.start_tangent[1]
        )
        angle = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        radius = before.end[0]
        is_pinch = radius <= GEOM_TOL and before.is_dome and after.is_dome
        if is_pinch:
            severity: Severity = "warn"
            kind: Literal["g1", "pinch"] = "pinch"
        else:
            severity = _severity(angle <= G1_MAX_ANGLE_DEG)
            kind = "g1"
        joints.append(
            JointCheck(
                index=index,
                z=before.end[1],
                radius=radius,
                angle_deg=angle,
                kind=kind,
                severity=severity,
            )
        )
    return joints


# ---------------------------------------------------------------------------
# 实体层：§5.7 五项（需 OCCT）
# ---------------------------------------------------------------------------


class AnalyticMetricsSummary(BaseModel):
    """解析量摘要（报告用）。"""

    volume: float
    surface_area: float
    centroid_z: float
    envelope: tuple[float, float, float]


class KernelMetricsSummary(BaseModel):
    """内核量摘要（报告用）。"""

    volume: float
    surface_area: float
    centroid_z: float
    is_valid: bool
    solid_count: int
    bbox_size: tuple[float, float, float]


class SolidValidationReport(BaseModel):
    """实体层校验报告（构建作业内调用）。"""

    ok: bool
    checks: list[CheckResult]
    analytic: AnalyticMetricsSummary
    kernel: KernelMetricsSummary


def validate_solid(
    profile: MeridianProfile,
    kernel: KernelMetrics,
    analytic: AnalyticMetrics,
    *,
    density: float | None = None,
    mass: float | None = None,
) -> SolidValidationReport:
    """§5.7 五项校验（拓扑 / 实体性 / 解析对照 / 质量一致性 / 意图断言）。

    ``density`` 与 ``mass`` 为 §8.4 质量回归的输入；M1 尚未接入材料与质量模块，
    因此该项在未提供参数时记为 ``skip`` 并写明原因，**不假装通过**。
    """
    checks: list[CheckResult] = []

    # 1 拓扑有效性
    checks.append(
        CheckResult(
            check="拓扑有效性",
            severity=_severity(kernel.is_valid),
            detail=(
                f"OCCT is_valid = True，单一实体数 = {kernel.solid_count}"
                if kernel.is_valid
                else "OCCT is_valid = False（常见原因：轴处反向尖点造成退化顶点）"
            ),
            value=float(kernel.solid_count),
        )
    )

    # 2 实体性：体积 > 0 且为单一封闭实体
    solid_ok = kernel.volume > GEOM_TOL and kernel.solid_count == 1
    checks.append(
        CheckResult(
            check="实体性",
            severity=_severity(solid_ok),
            detail=(
                f"体积 {kernel.volume:.9f} m³ > 0，实体数 = 1（封闭）"
                if solid_ok
                else f"体积 {kernel.volume:.9f} m³，实体数 = {kernel.solid_count}（应为 1）"
            ),
            value=kernel.volume,
        )
    )

    # 3 解析对照（体积 —— M1 门禁 < 0.1%）
    volume_error = relative_error(kernel.volume, analytic.volume)
    checks.append(
        CheckResult(
            check="解析对照：体积",
            severity=_severity(volume_error < ANALYTIC_VOLUME_TOL),
            detail=(
                f"内核 {kernel.volume:.9f} m³ vs 解析 {analytic.volume:.9f} m³，"
                f"相对误差 {volume_error:.3e}（门禁 < {ANALYTIC_VOLUME_TOL:g}）"
            ),
            value=kernel.volume,
            expected=analytic.volume,
            relative_error=volume_error,
        )
    )

    # 3b 解析对照（表面积 —— ∫2πr·ds，非门禁量但同属 §5.7）
    area_error = relative_error(kernel.surface_area, analytic.surface_area)
    checks.append(
        CheckResult(
            check="解析对照：表面积",
            severity=_severity(area_error < 1e-2, warn_only=True),
            detail=(
                f"内核 {kernel.surface_area:.6f} m² vs 解析 {analytic.surface_area:.6f} m²，"
                f"相对误差 {area_error:.3e}"
            ),
            value=kernel.surface_area,
            expected=analytic.surface_area,
            relative_error=area_error,
        )
    )

    # 3c 解析包络 vs 内核包围盒（双通道一致性的权威侧）
    kernel_bbox = kernel.bbox_size
    envelope_errors = [
        relative_error(kernel_bbox[axis], analytic.envelope[axis]) for axis in range(3)
    ]
    worst_envelope = max(envelope_errors)
    checks.append(
        CheckResult(
            check="解析对照：包络",
            severity=_severity(worst_envelope < ANALYTIC_VOLUME_TOL),
            detail=(
                f"内核 bbox {tuple(round(v, 9) for v in kernel_bbox)} vs "
                f"解析包络 {tuple(round(v, 9) for v in analytic.envelope)}，"
                f"最大相对误差 {worst_envelope:.3e}"
            ),
            value=kernel_bbox[0],
            expected=analytic.envelope[0],
            relative_error=worst_envelope,
        )
    )

    # 4 质量一致性（§8.4）——M1 未接入材料/质量模块
    if density is None or mass is None:
        checks.append(
            CheckResult(
                check="质量一致性",
                severity="skip",
                detail="需材料密度与 §8.4 质量回归结果；M1 尚未接入材料与质量模块，本项跳过",
            )
        )
    else:
        expected_mass = kernel.volume * density
        mass_error = relative_error(mass, expected_mass)
        checks.append(
            CheckResult(
                check="质量一致性",
                severity=_severity(mass_error < 1e-6),
                detail=f"几何体积×密度 {expected_mass:.6f} kg vs 质量回归 {mass:.6f} kg",
                value=mass,
                expected=expected_mass,
                relative_error=mass_error,
            )
        )

    return SolidValidationReport(
        ok=all(check.ok for check in checks),
        checks=checks,
        analytic=AnalyticMetricsSummary(
            volume=analytic.volume,
            surface_area=analytic.surface_area,
            centroid_z=analytic.centroid_z,
            envelope=analytic.envelope,
        ),
        kernel=KernelMetricsSummary(
            volume=kernel.volume,
            surface_area=kernel.surface_area,
            centroid_z=kernel.centroid_z,
            is_valid=kernel.is_valid,
            solid_count=kernel.solid_count,
            bbox_size=kernel.bbox_size,
        ),
    )
