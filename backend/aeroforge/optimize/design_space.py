"""优化变量空间（规格 §14：从基线 Vehicle 派生的设计变量与合法性校验）。

变量族（任务约定的最小可用集合，field_path 词汇与 §6.3 诊断同构）
------------------------------------------------------------------
- ``stages[i].length_m`` —— **长度缩放**（``continuous_scale``）：基因是
  无量纲缩放因子，施加值 = 基线级长 × 因子；其余显式字段（壁厚 / 级间段 /
  仪器舱）不动——缩放只改「装多少推进剂」，不改结构方案；
- ``stages[i].engine_count`` —— 发动机台数（``integer_count``）；
- ``boosters[j].count`` —— 助推器并联数量（``integer_count``）。

搜索边界由**后端按变量类别推导**（前端不造上下限，ADR-011 同族纪律）：
缩放因子带 ``[0.5, 1.5]``（工程惯例改动带：±50% 是「不改直径 / 不换发动机 /
不改级数」前提下的合理探索范围）；发动机数 ``[1, 2×基线]``（工程惯例：不换
机型的加倍上限）；助推器数量取 Schema 合法域 ``[1, 12]``（§6.1 Booster 层）。

合法性校验（候选淘汰不计 fitness，§14 / Deb 2001 约束支配）
------------------------------------------------------------
候选 = 基线 Vehicle 施加变量取值后的完整参数。淘汰判据两条：

1. :func:`aeroforge.params.constraints.check_vehicle` 有**硬约束**违反；
2. **分区铺满复核**（改长后必做）：复用 :func:`aeroforge.geometry.assembly.plan_stage`
   逐级重排九段分区——级长缩到装不下发动机舱 + 封头 + 级间段等固定占位时，
   几何解析账（同一分区事实）会以异常拒绝，候选即不可行。

判据 2 刻意**复用**装配层的分区推算而不是另写一份「级长下限」公式：
两账同源是 M5 第二片的既定裁定，复制一份必然漂移。plan_stage 所在模块顶层
import OCCT，故在本模块内**惰性导入**——优化评估链本体（perf 子包）保持
不碰几何内核的既有纪律，只有合法性复核在首次调用时把装配模块加载进来。
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aeroforge.errors import OptimizeError
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import Diagnostic, has_hard
from aeroforge.params.schema import Booster, Stage, Vehicle

#: 优化变量类别（与前端 ``OptimizeVariableKind`` 契约一致）。
#: ``continuous_scale`` = 长度按比例连续缩放；``integer_count`` = 数量类整数变量。
OptimizeVariableKind = Literal["continuous_scale", "integer_count"]

#: 长度缩放因子带（工程惯例改动带 ±50%，非权威来源；边界随请求下发供审计）。
LENGTH_SCALE_MIN = 0.5
LENGTH_SCALE_MAX = 1.5

#: 助推器并联数量的合法域（§6.1 Booster 层 Schema：count ge=1 le=12）。
BOOSTER_COUNT_MIN = 1
BOOSTER_COUNT_MAX = 12


#: 一个设计变量：请求形态（path + kind + base）与解析后的施加位置。
class OptimizeVariableSpec(BaseModel):
    """优化变量声明（前端逐项下发；``base`` 是基线 Vehicle 的当前值）。"""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        description=(
            "变量路径（跨层共享的 field_path 词汇，§6.3 末注）："
            "stages[i].length_m / stages[i].engine_count / boosters[j].count"
        )
    )
    kind: OptimizeVariableKind = Field(
        description=(
            "变量类别：continuous_scale = 长度按比例缩放（取值为无量纲因子）；"
            "integer_count = 数量类整数变量（发动机台数 / 助推器数量）"
        )
    )
    base: float = Field(
        description="基线 Vehicle 在该路径上的当前值（后端按其校验一致性，防陈旧请求）"
    )


#: 变量路径的解析形态（``stages[i].length_m`` 等——正则一处收敛，不散落字符串拼接）。
_STAGE_LENGTH_RE = re.compile(r"^stages\[(\d+)\]\.length_m$")
_STAGE_ENGINE_RE = re.compile(r"^stages\[(\d+)\]\.engine_count$")
BOOSTER_COUNT_RE = re.compile(r"^boosters\[(\d+)\]\.count$")

#: 每类路径的**期望类别**：kind 与路径不匹配即请求不合法（422）。
_EXPECTED_KIND: dict[str, OptimizeVariableKind] = {
    "stage_length": "continuous_scale",
    "stage_engine_count": "integer_count",
    "booster_count": "integer_count",
}


class DesignVariable(BaseModel):
    """解析后的设计变量：施加位置（级序 / 助推组序）+ 类别 + 基线值 + 搜索边界。"""

    path: str = Field(description="变量路径（原样回填 provenance 与方案参数摘要）")
    kind: OptimizeVariableKind
    slot: Literal["stage_length", "stage_engine_count", "booster_count"] = Field(
        description="施加位置类别"
    )
    position: int = Field(description="级序（0 起，对应 stages[position]）或助推组序")
    base: float = Field(description="基线值（施加值 = base × 因子 或绝对数量）")
    lower: float = Field(description="搜索下界（continuous_scale 为因子；integer_count 为数量）")
    upper: float = Field(description="搜索上界（口径同 lower）")

    def decode(self, gene: float) -> float:
        """归一化基因 ∈ [0,1] → 变量施加值（连续 = 因子；整数 = 数量取整夹取）。"""
        raw = self.lower + gene * (self.upper - self.lower)
        if self.kind == "integer_count":
            return float(min(max(round(raw), int(self.lower)), int(self.upper)))
        return raw

    def applied_value(self, vehicle: Vehicle) -> float:
        """该变量在候选 Vehicle 上的当前施加值（方案参数摘要 / 回写用）。

        数量类取值为整数的浮点表示（JSON number；前端 ``Number.isInteger`` 判整后
        按整数排版，「载入此构型」写回时由参数 Schema 的 lax 模式收成 int）。
        """
        return read_variable_value(vehicle, self.path)


def read_variable_value(vehicle: Vehicle, path: str) -> float:
    """按 field_path 读出基线 Vehicle 的当前值（路径不存在即 :class:`OptimizeError`）。"""
    match = _STAGE_LENGTH_RE.match(path)
    if match:
        index = int(match.group(1))
        if index >= len(vehicle.stages):
            raise _unknown_path_error(path, len(vehicle.stages), len(vehicle.boosters))
        return vehicle.stages[index].length_m
    match = _STAGE_ENGINE_RE.match(path)
    if match:
        index = int(match.group(1))
        if index >= len(vehicle.stages):
            raise _unknown_path_error(path, len(vehicle.stages), len(vehicle.boosters))
        return float(vehicle.stages[index].engine_count)
    match = BOOSTER_COUNT_RE.match(path)
    if match:
        index = int(match.group(1))
        if index >= len(vehicle.boosters):
            raise _unknown_path_error(path, len(vehicle.stages), len(vehicle.boosters))
        return float(vehicle.boosters[index].count)
    raise _unknown_path_error(path, len(vehicle.stages), len(vehicle.boosters))


def _unknown_path_error(path: str, stage_count: int, booster_count: int) -> OptimizeError:
    """未知 / 越界变量路径的统一拒绝（§14：变量族限于三类路径）。"""
    return OptimizeError(
        f"优化变量路径 {path!r} 不在可用变量族内（可用：stages[i].length_m / "
        f"stages[i].engine_count / boosters[j].count，且下标越界不得超出 "
        f"stages×{stage_count} / boosters×{booster_count}）",
        suggestion="从面板变量清单勾选（路径由前端按基线 Vehicle 推导）；"
        "其余参数维度（直径 / σ / 推进剂）属构型方案变更，不在本片变量空间内",
    )


def parse_variables(
    vehicle: Vehicle, specs: list[OptimizeVariableSpec]
) -> tuple[DesignVariable, ...]:
    """把请求变量声明解析为设计变量（路径 / 类别 / 基线一致性三重校验，不合法即 422）。"""
    if not specs:
        raise OptimizeError(
            "优化变量清单为空——至少给出一个变量才能构成搜索空间",
            suggestion="从面板变量清单勾选至少一项（长度缩放 / 发动机数 / 助推器数量）",
        )
    variables: list[DesignVariable] = []
    seen: set[str] = set()
    for spec in specs:
        if spec.path in seen:
            raise OptimizeError(
                f"优化变量 {spec.path!r} 重复声明——同一变量只能出现一次",
                suggestion="去掉重复项后再提交",
            )
        seen.add(spec.path)
        actual = read_variable_value(vehicle, spec.path)
        if abs(spec.base - actual) > 1e-6 * max(1.0, abs(actual)):
            raise OptimizeError(
                f"变量 {spec.path!r} 的基线值 {spec.base} 与 Vehicle 当前值 {actual} 不一致"
                "（请求可能基于陈旧参数）",
                suggestion="重新载入当前构型后再发起优化（base 取基线 Vehicle 的实时值）",
            )
        slot = _slot_of(spec.path, spec.kind)
        lower, upper = _bounds_for(slot, actual)
        variables.append(
            DesignVariable(
                path=spec.path,
                kind=spec.kind,
                slot=slot,
                position=_position_of(spec.path),
                base=actual,
                lower=lower,
                upper=upper,
            )
        )
    return tuple(variables)


def _slot_of(
    path: str, kind: OptimizeVariableKind
) -> Literal["stage_length", "stage_engine_count", "booster_count"]:
    """路径 → 施加位置；类别与路径不匹配即拒绝（长度是连续、数量是整数）。"""
    if _STAGE_LENGTH_RE.match(path):
        slot = "stage_length"
    elif _STAGE_ENGINE_RE.match(path):
        slot = "stage_engine_count"
    elif BOOSTER_COUNT_RE.match(path):
        slot = "booster_count"
    else:
        raise _unknown_path_error(path, 0, 0)
    expected = _EXPECTED_KIND[slot]
    if kind != expected:
        raise OptimizeError(
            f"变量 {path!r} 的类别 {kind!r} 与路径不符（应为 {expected!r}）",
            suggestion="长度类路径用 continuous_scale（缩放因子），数量类路径用 integer_count",
        )
    return slot  # type: ignore[return-value]


def _position_of(path: str) -> int:
    """路径中的下标（级序 / 助推组序，0 起）。"""
    match = (
        _STAGE_LENGTH_RE.match(path) or _STAGE_ENGINE_RE.match(path) or BOOSTER_COUNT_RE.match(path)
    )
    assert match is not None  # parse_variables 已先行校验
    return int(match.group(1))


def _bounds_for(
    slot: Literal["stage_length", "stage_engine_count", "booster_count"], base: float
) -> tuple[float, float]:
    """按施加位置推导搜索边界（见模块 docstring：边界由后端推导并随 provenance 下发）。"""
    if slot == "stage_length":
        return (LENGTH_SCALE_MIN, LENGTH_SCALE_MAX)
    if slot == "booster_count":
        return (float(BOOSTER_COUNT_MIN), float(BOOSTER_COUNT_MAX))
    # 发动机台数：Schema 下界 1，上界取基线加倍（工程惯例：不换机型的探索上限）
    return (1.0, max(2.0, 2.0 * base))


def apply_variables(
    vehicle: Vehicle, variables: tuple[DesignVariable, ...], genes: list[float]
) -> Vehicle:
    """把基因向量施加到基线 Vehicle 上生成候选（model_copy 更新，基线不被改动）。

    长度路径：施加值 = 基线级长 × 因子（因子 = 解码基因）；数量路径：整数取值。
    其余字段（壁厚 / 级间段 / 发动机定义）保持基线不变——变量空间只覆盖
    「装多少推进剂 + 装几台机」。
    """
    stages: list[Stage] = list(vehicle.stages)
    boosters: list[Booster] = list(vehicle.boosters)
    for variable, gene in zip(variables, genes, strict=True):
        value = variable.decode(gene)
        if variable.slot == "stage_length":
            stages[variable.position] = stages[variable.position].model_copy(
                update={"length_m": stages[variable.position].length_m * value}
            )
        elif variable.slot == "stage_engine_count":
            stages[variable.position] = stages[variable.position].model_copy(
                update={"engine_count": round(value)}
            )
        else:
            boosters[variable.position] = boosters[variable.position].model_copy(
                update={"count": round(value)}
            )
    return vehicle.model_copy(update={"stages": tuple(stages), "boosters": boosters})


def design_variable_from_path(vehicle: Vehicle, path: str) -> DesignVariable:
    """按路径从基线 Vehicle 构建设计变量（批量扫描轴用：kind 与边界由后端推导）。"""
    actual = read_variable_value(vehicle, path)
    if _STAGE_LENGTH_RE.match(path):
        slot: Literal["stage_length", "stage_engine_count", "booster_count"] = "stage_length"
        kind: OptimizeVariableKind = "continuous_scale"
    elif _STAGE_ENGINE_RE.match(path):
        slot = "stage_engine_count"
        kind = "integer_count"
    elif BOOSTER_COUNT_RE.match(path):
        slot = "booster_count"
        kind = "integer_count"
    else:
        raise _unknown_path_error(path, len(vehicle.stages), len(vehicle.boosters))
    lower, upper = _bounds_for(slot, actual)
    return DesignVariable(
        path=path,
        kind=kind,
        slot=slot,
        position=_position_of(path),
        base=actual,
        lower=lower,
        upper=upper,
    )


def apply_values(
    vehicle: Vehicle, variables: tuple[DesignVariable, ...], values: list[float]
) -> Vehicle:
    """把**显式变量值**施加到基线 Vehicle 上生成候选（批量扫描轴用，不做解码）。

    与 :func:`apply_variables` 的差别：值已是施加语义（连续 = 缩放因子、
    整数 = 数量），不再经基因解码——扫描的轴取值由用户显式给定。
    """
    stages: list[Stage] = list(vehicle.stages)
    boosters: list[Booster] = list(vehicle.boosters)
    for variable, value in zip(variables, values, strict=True):
        if variable.slot == "stage_length":
            stages[variable.position] = stages[variable.position].model_copy(
                update={"length_m": stages[variable.position].length_m * value}
            )
        elif variable.slot == "stage_engine_count":
            stages[variable.position] = stages[variable.position].model_copy(
                update={"engine_count": round(value)}
            )
        else:
            boosters[variable.position] = boosters[variable.position].model_copy(
                update={"count": round(value)}
            )
    return vehicle.model_copy(update={"stages": tuple(stages), "boosters": boosters})


def candidate_infeasibility(vehicle: Vehicle) -> tuple[bool, str | None, list[Diagnostic]]:
    """候选合法性复核（§14：不可行候选淘汰，不计 fitness）。

    返回 ``(infeasible, reason, hard_diagnostics)``：

    1. :func:`check_vehicle` 的**硬约束**违反 → 不可行（附带裁定供排障）；
    2. **分区铺满复核**：逐级复用 :func:`aeroforge.geometry.assembly.plan_stage`
       重排九段分区——级长缩到装不下固定占位（发动机舱 / 封头 / 级间段）时异常
       → 不可行（惰性导入：评估链本体不碰几何内核，见模块 docstring）。
    """
    violations = check_vehicle(vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        return True, f"硬约束违反 {len(hard)} 条（首条 {hard[0].code}：{hard[0].message}）", hard

    # 惰性导入：装配模块顶层拉起 OCCT（build123d），只在候选复核首次执行时加载——
    # 分区推算本身是纯数学，复用它（而非另写级长下限公式）是 M5「两账同源」的既定裁定
    from aeroforge.geometry.assembly import AssemblyError, plan_stage

    try:
        for stage in vehicle.stages:
            plan_stage(stage)
        for booster in vehicle.boosters:
            plan_stage(booster.stage)
    except (AssemblyError, ValueError, ArithmeticError) as exc:
        return True, f"分区铺满复核失败（{type(exc).__name__}）：{exc}", []
    return False, None, []
