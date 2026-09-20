"""简化捆绑几何（规格 §1.7.6 OI-36 ④：M4 = 周向均布侧级圆柱体）。

场景图契约（沿用 OI-33 的具名节点机制）
----------------------------------------
``vehicle``（根，无 mesh）→ ``seg-0`` / ``seg-1`` / …（芯级逐段）+
``booster-<k>``（``k`` 为 0 基，0..count-1）。助推器节点缺名同样等于
"前端找不到节点 = 静默失效"，故必须逐枚机检。

M4 简化口径（§8.5 / OI-36 ④）
------------------------------
- 每枚助推器取**轴向基线 = 0** 起算的圆柱体（长度 = 侧级 ``length_m``）；
- 径向位置 = 芯级**全剖面最大半径** + 助推器半径 + 0.1 m 间隙——取全剖面最大
  而非底段半径，保证助推器沿全长（含整流罩段）都不与芯级相交；
- 周向按 ``2πk / count`` 均布；
- 无助推器时 :func:`build_bundle` **原样委托** :func:`aeroforge.geometry.revolve.build_segments`
  ——既有输入的产物字节路径一个字节都不动（§9.2 缓存纪律）。

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

#: 捆绑 GLB 的助推器节点名前缀：``booster-<k>``，``<k>`` 为 0 基枚举下标。
#: ⚠ 前端按名找节点（同 ``seg-<i>`` 口径），改名即关掉助推器显隐。
GLB_BOOSTER_PREFIX = "booster-"


class BoosterSummary(BaseModel):
    """构建入参里的助推器摘要（M4 简化：单一构型的周向均布组）。

    这是**几何域的入参**，不是 :class:`aeroforge.params.schema.Booster` 的搬运：
    从参数层助推器取 ``count`` / 侧级 ``diameter_m`` / 侧级 ``length_m`` 折算而成。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: int = Field(ge=1, description="助推器数量（周向均布的枚数）")
    diameter_m: float = Field(gt=0.0, description="单枚助推器直径（m）")
    length_m: float = Field(gt=0.0, description="单枚助推器长度（m；轴向基线 = 0 起算）")


def booster_label(index: int) -> str:
    """第 ``index`` 枚助推器的 GLB 节点名（0 基）。"""
    return f"{GLB_BOOSTER_PREFIX}{index}"


def booster_axis_radius(core_max_radius_m: float, booster_diameter_m: float) -> float:
    """助推器轴线的径向位置（m）= 芯级最大半径 + 助推器半径 + 间隙。"""
    if core_max_radius_m <= GEOM_TOL:
        msg = "芯级最大半径为 0（剖面全退化），无法布置助推器"
        raise ValueError(msg)
    return core_max_radius_m + booster_diameter_m / 2.0 + BOOSTER_GAP_M


def booster_cylinders(profile: MeridianProfile, boosters: BoosterSummary) -> list[bd.Solid]:
    """逐枚助推器圆柱体（周向均布，具名 ``booster-<k>``）。

    与 GLB / STEP 共用同一份实体——两通道的助推器**是同一几何**，不是各画一份。
    """
    resolved = resolve(profile)
    axis_radius = booster_axis_radius(resolved.max_radius, boosters.diameter_m)
    radius = boosters.diameter_m / 2.0
    solids: list[bd.Solid] = []
    for index in range(boosters.count):
        angle = 2.0 * math.pi * index / boosters.count
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
