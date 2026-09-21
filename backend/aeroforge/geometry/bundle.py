"""捆绑几何（规格 §1.7.6 OI-36：M4 = 周向均布；M5 = 完整布局）。

场景图契约（沿用 OI-33 的具名节点机制）
----------------------------------------
``vehicle``（根，无 mesh）→ ``seg-0`` / ``seg-1`` / …（芯级逐段）+
``booster-<k>``（``k`` 为 0 基，0..count-1）。助推器节点缺名同样等于
"前端找不到节点 = 静默失效"，故必须逐枚机检。

M4 简化口径（§8.5 / OI-36 ④，缺省路径——字节不变的现状）
--------------------------------------------------------
- 每枚助推器取**轴向基线 = 0** 起算的圆柱体（长度 = 侧级 ``length_m``）；
- 径向位置 = 芯级**全剖面最大半径** + 助推器半径 + 0.1 m 间隙——取全剖面最大
  而非底段半径，保证助推器沿全长（含整流罩段）都不与芯级相交；
- 周向按 ``2πk / count`` 均布；
- 无助推器时 :func:`build_bundle` **原样委托** :func:`aeroforge.geometry.revolve.build_segments`
  ——既有输入的产物字节路径一个字节都不动（§9.2 缓存纪律）。

M5 完整布局（OI-36 的 M5 部分）
------------------------------
- ``radial_offset_m``：助推器**轴线**距芯级轴线的径向距离（用户权威；缺省 None =
  M4 贴接现状）；须 ≥ 芯级最大半径（硬校验在约束引擎，几何层对间隙 ≥ 0 复核）；
- ``angles_deg``：自定义角位（°，自 +X 逆时针；缺省 None = 周向均布）；个数必须
  等于 ``count``；相邻助推器表面间隙 < 0.05 m（工程惯例最小间隙）→ ValueError
  （约束引擎为 warning 级、几何层兜底报错——两层都留痕）；
- 参数缺省时与 M4 路径**逐字节一致**（canonical 回归测试钉死，§9.2）。

量测口径（"看到的"与"算的"一致性，P1 / ADR-012）
------------------------------------------------
权威量测（体积 / 表面积 / 质心）与解析对照**仍取芯级整体体**；助推器体积在
metrics 的 ``boosters`` 块内**单独成账**——不得静默并入核心体积，否则解析对照
（§5.7）自证失效。
"""

from __future__ import annotations

import json
import math

import build123d as bd
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.geometry.meridian import GEOM_TOL, MeridianProfile, resolve, round_floats
from aeroforge.geometry.revolve import (
    GLB_ROOT_NAME,
    build_segments,
    segment_solids,
)

#: 助推器与芯级之间的径向间隙（m）。工程惯例值：并联构型的结构间隙下限。
BOOSTER_GAP_M = 0.1

#: 相邻助推器表面最小间隙（m）。工程惯例值：并联构型的安装 / 分离安全间隙；
#: 违反在约束引擎为 warning 级、几何层兜底报错。
BOOSTER_BOOSTER_MIN_GAP_M = 0.05

#: 捆绑 GLB 的助推器节点名前缀：``booster-<k>``，``<k>`` 为 0 基枚举下标。
#: ⚠ 前端按名找节点（同 ``seg-<i>`` 口径），改名即关掉助推器显隐。
GLB_BOOSTER_PREFIX = "booster-"


class BoosterSummary(BaseModel):
    """构建入参里的助推器摘要（M4 简化 + M5 完整布局字段）。

    这是**几何域的入参**，不是 :class:`aeroforge.params.schema.Booster` 的搬运：
    从参数层助推器取 ``count`` / 侧级 ``diameter_m`` / 侧级 ``length_m`` 折算而成；
    M5 增补 ``radial_offset_m`` / ``angles_deg``（缺省 None = M4 周向均布现状，
    canonical 字节稳定，§9.2）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(ge=1, description="助推器数量（周向均布的枚数）")
    diameter_m: float = Field(gt=0.0, description="单枚助推器直径（m）")
    length_m: float = Field(gt=0.0, description="单枚助推器长度（m；轴向基线 = 0 起算）")
    radial_offset_m: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            "助推器轴线距芯级轴线的径向距离（m）；None = 贴接（芯级半径+助推器半径+间隙）"
        ),
    )
    angles_deg: tuple[float, ...] | None = Field(
        default=None, description="自定义角位（°，自 +X 逆时针）；None = 周向均布 2πk/count"
    )


def booster_label(index: int) -> str:
    """第 ``index`` 枚助推器的 GLB 节点名（0 基）。"""
    return f"{GLB_BOOSTER_PREFIX}{index}"


def booster_axis_radius(core_max_radius_m: float, booster_diameter_m: float) -> float:
    """助推器轴线的径向位置（m）= 芯级最大半径 + 助推器半径 + 间隙。"""
    if core_max_radius_m <= GEOM_TOL:
        msg = "芯级最大半径为 0（剖面全退化），无法布置助推器"
        raise ValueError(msg)
    return core_max_radius_m + booster_diameter_m / 2.0 + BOOSTER_GAP_M


def _booster_placements(
    core_max_radius_m: float, boosters: BoosterSummary
) -> list[tuple[float, float]]:
    """逐枚助推器的 (径向距离, 角度弧度)——M4 均布 / M5 完整布局的单一实现。"""
    booster_radius = boosters.diameter_m / 2.0
    if boosters.radial_offset_m is not None:
        axis_radius = boosters.radial_offset_m
        # 硬校验（与约束引擎同判据）：不得与芯级干涉（径向间隙 ≥ 0）
        if axis_radius < core_max_radius_m + booster_radius - GEOM_TOL:
            msg = (
                f"助推器径向偏移 {axis_radius:.3f} m 使其与芯级干涉（芯级最大半径 "
                f"{core_max_radius_m:.3f} m + 助推器半径 {booster_radius:.3f} m）——"
                "径向间隙必须 ≥ 0（radial_offset_m ≥ 芯级最大半径 + 助推器半径）"
            )
            raise ValueError(msg)
    else:
        axis_radius = booster_axis_radius(core_max_radius_m, boosters.diameter_m)

    if boosters.angles_deg is not None:
        if len(boosters.angles_deg) != boosters.count:
            msg = (
                f"自定义角位个数 {len(boosters.angles_deg)} ≠ 助推器数量 {boosters.count}"
                "（angles_deg 与 count 必须一一对应）"
            )
            raise ValueError(msg)
        angles = [math.radians(a % 360.0) for a in boosters.angles_deg]
        # 兜底校验（约束引擎为 warning 级）：相邻助推器表面间隙 ≥ 工程惯例最小值
        sorted_angles = sorted(angles)
        for index in range(len(sorted_angles)):
            if len(sorted_angles) < 2:
                break
            step = sorted_angles[(index + 1) % len(sorted_angles)] - sorted_angles[index]
            if step <= 0.0:
                step += 2.0 * math.pi
            surface_gap = 2.0 * axis_radius * math.sin(step / 2.0) - boosters.diameter_m
            if surface_gap < BOOSTER_BOOSTER_MIN_GAP_M - GEOM_TOL:
                msg = (
                    f"相邻助推器表面间隙 {surface_gap:.4f} m 小于工程惯例最小值 "
                    f"{BOOSTER_BOOSTER_MIN_GAP_M} m（角位布置过密）——请增大角位间隔"
                    "或减小径向偏移"
                )
                raise ValueError(msg)
    else:
        angles = [2.0 * math.pi * k / boosters.count for k in range(boosters.count)]
    return [(axis_radius, angle) for angle in angles]


def booster_cylinders(profile: MeridianProfile, boosters: BoosterSummary) -> list[bd.Solid]:
    """逐枚助推器圆柱体（周向均布，具名 ``booster-<k>``）。

    与 GLB / STEP 共用同一份实体——两通道的助推器**是同一几何**，不是各画一份。
    """
    resolved = resolve(profile)
    return booster_cylinders_for_radius(resolved.max_radius, boosters)


def booster_cylinders_for_radius(
    core_max_radius_m: float, boosters: BoosterSummary
) -> list[bd.Solid]:
    """按芯级最大半径布置的助推器圆柱体（M5 装配通路复用，几何与 M4 形态一致）。

    车辆形态构建没有母线剖面，芯级最大半径来自装配布局（整流罩 / 各级直径的最大
    半径）——与 :func:`booster_cylinders` 共用同一份径向定位公式与节点名约定。
    M5 完整布局（``radial_offset_m`` / ``angles_deg`` 显式给出时）生效；
    二者缺省时与 M4 周向均布路径**逐字节一致**（§9.2 canonical 纪律）。
    """
    radius = boosters.diameter_m / 2.0
    solids: list[bd.Solid] = []
    for index, (axis_radius, angle) in enumerate(_booster_placements(core_max_radius_m, boosters)):
        solid = bd.Solid.make_cylinder(
            radius,
            boosters.length_m,
            bd.Plane((axis_radius * math.cos(angle), axis_radius * math.sin(angle), 0.0)),
        )
        solid.label = booster_label(index)
        solids.append(solid)
    return solids


def canonical_json(boosters: BoosterSummary) -> str:
    """助推器摘要的 canonical JSON（缓存键分量，§9.2）。

    键序固定、浮点定量、无多余空白；量化器与剖面 canonical 共用
    :func:`aeroforge.geometry.meridian.round_floats`——两份字节形态不得各自漂移。
    """
    payload = round_floats(boosters.model_dump(mode="json", exclude_none=True))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_bundle(profile: MeridianProfile, boosters: BoosterSummary | None = None) -> bd.Compound:
    """构建 GLB 场景图：芯级逐段具名 + （可选）助推器逐枚具名。

    ``boosters=None`` 时**原样返回** :func:`aeroforge.geometry.revolve.build_segments`
    的结果——无助推器的构建路径与现状逐字节一致（§9.2）。
    """
    if boosters is None:
        return build_segments(profile)

    children: list[bd.Compound | bd.Solid] = [
        *segment_solids(profile),
        *booster_cylinders(profile, boosters),
    ]
    root = bd.Compound(children=children)
    root.label = GLB_ROOT_NAME
    return root


def booster_assembly(
    profile: MeridianProfile, boosters: BoosterSummary | None, core_part: bd.Compound
) -> tuple[bd.Compound, list[bd.Solid]]:
    """STEP 导出用的捆绑组装：``(导出形状, 助推器实体清单)``。

    无助推器 → ``(core_part, [])``，STEP 路径与现状完全一致；有助推器 →
    芯级整体体与助推器圆柱体装进同一复合体（产品名随节点名写入 STEP）。
    返回实体清单供 metrics 单独成账（OI-36：助推器体积不得并入核心体积）。
    （``core_part`` 声明为 ``Compound``：:class:`bd.Part` 是它的子类，
    :func:`build_solid` 的返回值可直接传入。）
    """
    if boosters is None:
        return core_part, []
    solids = booster_cylinders(profile, boosters)
    return bd.Compound(children=[core_part, *solids]), solids
