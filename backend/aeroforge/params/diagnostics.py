"""方案诊断规则集（规格 §6.5）。

参数合法**不等于**方案合理，故 §6.5 要求每次求解后跑一遍诊断规则集，
输出**结构化诊断**而不是"通过/不通过"。

阈值可配置（OI-08 / §18.4）
---------------------------
:class:`DiagnosticThresholds` 的**默认值即规格 §6.5 表内的数值**，可经环境变量
（``AEROFORGE_`` 前缀）或 ``config.toml`` 覆盖，优先级
``环境变量 > config.toml > 默认值``。

> ⚠ 全部阈值为**工程惯例值，非权威来源**（§6.5 的强制来源声明）：它们来自公开工程的
> 量级经验，**没有任何一条可以引用为标准或论文结论**。故每条阈值的 ``description``
> 都必须带该标注——**不得**以"规格规定"的语气呈现为硬性真理。M4 用基准火箭（§13.2）
> 反标定时**只改配置、不改代码**。

M2 的范围边界（OI-11 裁决）
---------------------------
``impact``（"若按建议调整，运力预计提升 X%"）**必须**由 §8.7 敏感度实算得出，而敏感度
属 **M4**，故 **M2 不交付带 ``impact`` 的诊断**，一律输出六字段
``{level, code, field_path, message, suggestion}``（与 §6.3 校验结构同形，见
:mod:`aeroforge.params.report`）。**不得**为凑齐验收而用经验系数编造 ``impact``。

由此产生三类**必须留痕**的账目，落在 :class:`RuleOutcome` 上：

- ``diagnostics``：规则已判定，产出的裁定（可能为空 = 该规则通过）；
- ``deferred_reason``：**整条规则未判定**，并写明缺什么（如"需要 Δv 预算，属 M4"）；
- ``uncovered``：规则已判，但有**判据分支**未启用（如"固体助推阈值缺标识"）。

刻意**不设** ``info`` 级别，也刻意**不把未判定的规则静默跳过**——空集通过会让
"未验证"与"已验证"长得一模一样，而"没报错 ≠ 正确"正是本项目最贵的一课。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from aeroforge.params import dag
from aeroforge.params.report import Diagnostic, Level
from aeroforge.params.schema import ParamsModel, Vehicle
from aeroforge.paths import config_file

#: 阈值来源标注（§6.5 强制）。出现在每条阈值字段的 description 里，随 OpenAPI 下发给前端。
SOURCE_CONVENTION = "工程惯例值，非权威来源（不得引用为标准或论文结论）"
#: 「最小工艺厚度」的来源标注：§6.5 表只写了判据、**没给数值**，故必须显式配置。
SOURCE_PROCESS_LIMIT = "工艺下限（§6.5 表未给数值：须显式配置，未配置即不判定）"

#: 判定码（§6.5 表逐行对应；前四条是 M2 可判的，后四条按 OI-11 登记为显式 deferred）。
CODE_TWR = "TWR_TOO_LOW"
CODE_STRUCTURE_MASS_RATIO = "STRUCTURE_MASS_RATIO_OUT_OF_RANGE"
CODE_TANK_WALL = "TANK_WALL_BELOW_PROCESS_MINIMUM"
CODE_LENGTH_TO_DIAMETER = "LENGTH_TO_DIAMETER_OUT_OF_RANGE"
CODE_DV_ALLOCATION = "DV_ALLOCATION_DEVIATION"
CODE_UPPER_STAGE_MARGIN = "UPPER_STAGE_MARGIN_OUT_OF_RANGE"
CODE_CAPACITY_GAP = "PAYLOAD_CAPACITY_GAP"
CODE_NOZZLE_DIAMETER = "NOZZLE_EXIT_EXCEEDS_STAGE_DIAMETER"


class DiagnosticThresholds(BaseSettings):
    """§6.5 阈值集。**默认值即规格表内数值**（OI-08：默认值即下表数值）。

    来源优先级 ``环境变量 > config.toml > 默认值``（§18.4）。环境变量名 = 字段名大写加
    ``AEROFORGE_`` 前缀，如 ``AEROFORGE_TWR_LIQUID_MIN=1.25``。
    """

    model_config = SettingsConfigDict(env_prefix="AEROFORGE_", extra="ignore")

    twr_liquid_min: float = Field(
        default=1.2,
        gt=0.0,
        description=f"起飞推重比下界（液体火箭）。{SOURCE_CONVENTION}",
    )
    twr_solid_min: float = Field(
        default=1.5,
        gt=0.0,
        description=(
            f"起飞推重比下界（固体助推）。{SOURCE_CONVENTION}；"
            "⚠ M2 的 Schema 尚无固体助推标识，本阈值暂不参与判定（见规则 uncovered）"
        ),
    )
    structure_mass_ratio_min: float = Field(
        default=0.03,
        gt=0.0,
        description=f"结构质量比（箭体结构质量 / 推进剂质量）下界，一级。{SOURCE_CONVENTION}",
    )
    structure_mass_ratio_max: float = Field(
        default=0.08,
        gt=0.0,
        description=f"结构质量比上界，一级。{SOURCE_CONVENTION}",
    )
    length_to_diameter_min: float = Field(
        default=5.0,
        gt=0.0,
        description=f"长径比下界。{SOURCE_CONVENTION}",
    )
    length_to_diameter_max: float = Field(
        default=30.0,
        gt=0.0,
        description=f"长径比上界（过大纵振风险）。{SOURCE_CONVENTION}",
    )
    min_tank_wall_thickness_m: float | None = Field(
        default=None,
        gt=0.0,
        description=(
            f"贮箱最小工艺厚度（m）。{SOURCE_PROCESS_LIMIT}；"
            "默认 None = 未配置，此时该条判据**不判定**并留痕，"
            "而不是填一个凭空的毫米数（§1.4-4 溯源红线）"
        ),
    )
    dv_allocation_deviation_max: float = Field(
        default=0.20,
        gt=0.0,
        description=f"级间 ΔV 分配相对最优分配的允许偏离。{SOURCE_CONVENTION}；判定属 M4",
    )
    upper_stage_margin_min: float = Field(
        default=0.01,
        ge=0.0,
        description=f"上面级分离后 ΔV 余量下界。{SOURCE_CONVENTION}；判定属 M4",
    )
    upper_stage_margin_max: float = Field(
        default=0.05,
        ge=0.0,
        description=f"上面级分离后 ΔV 余量上界。{SOURCE_CONVENTION}；判定属 M4",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """来源优先级 ``环境变量 > config.toml > 默认值``（§18.4）。

        配置文件路径必须在**实例化时**求值：写在类体里会在 import 期把路径冻住，
        测试用 ``AEROFORGE_DATA_DIR`` 重定向数据根后就取不到该重定向了
        （同 ``artifacts_root`` 那一课的成因）。文件不存在即只有环境变量与默认值——
        缺配置文件是正常状态，不是错误。
        """
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        path = config_file()
        if path.is_file():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=path))
        sources.append(file_secret_settings)
        return tuple(sources)


class RuleOutcome(ParamsModel):
    """一条 §6.5 规则的执行账目——**含未判定的部分**。"""

    code: str = Field(description="规则判定码")
    title: str = Field(description="规则名（§6.5 表「规则」列）")
    level: Level = Field(description="该规则违反时的级别（§6.5 表「级别」列）")
    threshold: str = Field(description="本次实际生效的阈值（含来源标注）")
    source: str = Field(description="阈值来源标注（§6.5 强制携带）")
    diagnostics: tuple[Diagnostic, ...] = Field(
        default=(), description="本规则产出的裁定；空 = 该规则通过"
    )
    deferred_reason: str | None = Field(
        default=None, description="**整条规则**未判定的原因（缺输入 / 属后续里程碑）"
    )
    uncovered: tuple[str, ...] = Field(
        default=(), description="规则已判、但**判据分支**未启用的说明（空 = 全覆盖）"
    )

    @property
    def deferred(self) -> bool:
        return self.deferred_reason is not None


class DiagnosticsReport(ParamsModel):
    """一次诊断的完整结果：裁定清单 + 逐规则账目。"""

    diagnostics: tuple[Diagnostic, ...] = ()
    rules: tuple[RuleOutcome, ...] = ()

    @property
    def hard_failures(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.level == "hard")

    @property
    def deferred_rule_codes(self) -> tuple[str, ...]:
        """未判定的规则码——**必须随结果一并下发**，否则"未验证"会与"已验证"同貌。"""
        return tuple(rule.code for rule in self.rules if rule.deferred)


def _hard(code: str, field_path: str, message: str) -> Diagnostic:
    return Diagnostic(
        level="hard",
        code=code,
        field_path=field_path,
        message=message,
        suggestion="按 §6.5 表给出的方向调整（该阈值为工程惯例值，非权威来源）",
    )


def _warn(code: str, field_path: str, message: str, suggestion: str) -> Diagnostic:
    return Diagnostic(
        level="warning", code=code, field_path=field_path, message=message, suggestion=suggestion
    )


def _deferred(code: str, title: str, level: Level, *, reason: str, threshold: str) -> RuleOutcome:
    return RuleOutcome(
        code=code,
        title=title,
        level=level,
        threshold=threshold,
        source=SOURCE_CONVENTION,
        deferred_reason=reason,
    )


def _rule_head(code: str, title: str, level: Level, threshold: str, source: str) -> dict[str, Any]:
    """规则的公共表头。

    返回 ``dict[str, Any]``（而非 ``dict[str, str]``）：``level`` 是
    ``Literal["hard","warning"]``，dict 字面量会被推成 ``dict[str, str]`` 而与模型字段
    类型不符，故统一在此给出宽类型——四条 M2 规则的构造都走这一形状。
    """
    return {"code": code, "title": title, "level": level, "threshold": threshold, "source": source}


def _twr_rule(result: dag.PropagationResult, limits: DiagnosticThresholds) -> RuleOutcome:
    """起飞推重比 ≥ 下界（硬）。值的来源是 DAG 的 ``vehicle.twr_liftoff``。"""
    outcome = _rule_head(
        CODE_TWR, "起飞推重比", "hard", f"T/W ≥ {limits.twr_liquid_min}（液体）", SOURCE_CONVENTION
    )
    # ⚠ Schema 里没有"固体助推"标识，故 §6.5 表的第二档阈值**判不了**——登记为留痕，
    # 而不是把 1.2 悄悄套到所有构型上还说"全部规则已覆盖"。
    partial = ("固体助推阈值（§6.5 表：1.5）未参与判定：Schema 尚无固体助推标识",)

    value = result.values.get("vehicle.twr_liftoff")
    if value is None:
        return RuleOutcome(
            **outcome,
            deferred_reason=(
                "起飞推重比需要起飞质量 GLOW，而 GLOW 需要各级推进剂质量——"
                "该量是 M4 定尺求解的输出（§6.2），当前未提供"
            ),
        )
    if value < limits.twr_liquid_min:
        return RuleOutcome(
            **outcome,
            diagnostics=(
                _hard(
                    CODE_TWR,
                    "vehicle",
                    f"起飞推重比 {value:.3f} 低于下界 {limits.twr_liquid_min}"
                    "（推力不足以在重力损失吃掉运力前离塔）",
                ),
            ),
            uncovered=partial,
        )
    return RuleOutcome(**outcome, uncovered=partial)


def _structure_mass_ratio_rule(
    result: dag.PropagationResult, limits: DiagnosticThresholds, *, first_stage: int
) -> RuleOutcome:
    """结构质量比 ∈ [下界, 上界]（警），§6.5 表限定**一级**。

    判据用 σ 的派生量 ``σ/(1−σ)``，故只需 σ 即可判定——不依赖 M4 的任何结果。
    """
    outcome = _rule_head(
        CODE_STRUCTURE_MASS_RATIO,
        "结构质量比",
        "warning",
        (
            f"m_dry/m_prop ∈ [{limits.structure_mass_ratio_min}, "
            f"{limits.structure_mass_ratio_max}]（一级）"
        ),
        SOURCE_CONVENTION,
    )
    node = f"{dag.stage_prefix(first_stage)}.dry_to_prop_ratio"
    value = result.values.get(node)
    if value is None:
        return RuleOutcome(
            **outcome,
            deferred_reason=f"缺派生量 {node}（需要该级的 structure_coefficient）",
        )

    field_path = "stages[0].structure_coefficient"
    note = ("§6.5 表把本判据限定为一级；其余级未判定（其 σ 由 §6.3 的硬约束管线把守）",)
    low, high = limits.structure_mass_ratio_min, limits.structure_mass_ratio_max
    if low <= value <= high:
        return RuleOutcome(**outcome, uncovered=note)
    direction = "偏低" if value < low else "偏高"
    return RuleOutcome(
        **outcome,
        diagnostics=(
            _warn(
                CODE_STRUCTURE_MASS_RATIO,
                field_path,
                f"第一级结构质量比 m_dry/m_prop = {value:.4f}，{direction}（区间 [{low}, {high}]）",
                "复核壁厚与材料；偏低多意味着结构质量被低估，偏高则运力受损"
                "（阈值是工程惯例值，非权威来源）",
            ),
        ),
        uncovered=note,
    )


def _tank_wall_rule(vehicle: Vehicle, limits: DiagnosticThresholds) -> RuleOutcome:
    """贮箱壁厚 ≥ 最小工艺厚度（硬）。

    ⚠ 只判**下界**：上界（壁厚 < 直径/2）由 §6.3 的安全边界
    ``SAFETY_TANK_WALL_TOO_THICK`` 判定——同一违反不能有两个判定码，
    否则前端会渲染出两条"看起来是两处缺陷"的告警。
    """
    lower = limits.min_tank_wall_thickness_m
    outcome = _rule_head(
        CODE_TANK_WALL,
        "贮箱壁厚",
        "hard",
        (
            f"壁厚 ≥ {lower} m（最小工艺厚度，显式配置）"
            if lower is not None
            # 未配置时阈值字符串也必须如实说明，不得印一个凭空的数字
            else "壁厚 ≥ 最小工艺厚度（未配置）"
        ),
        SOURCE_PROCESS_LIMIT,
    )
    scope = ("上界（壁厚 < 直径/2）由 §6.3 安全边界 SAFETY_TANK_WALL_TOO_THICK 判定，本规则不重复",)
    if lower is None:
        return RuleOutcome(
            **outcome,
            deferred_reason=(
                "§6.5 表只给了判据『≥ 最小工艺厚度』、未给数值，且本机未配置"
                "（AEROFORGE_MIN_TANK_WALL_THICKNESS_M 或 config.toml）——不判定，"
                "而不是填一个凭空的下界"
            ),
        )

    diagnostics: list[Diagnostic] = []
    for position, stage in enumerate(vehicle.stages):
        for role, key, tank in (
            ("氧化剂箱", "oxidizer_tank", stage.geometry.oxidizer_tank),
            ("燃料箱", "fuel_tank", stage.geometry.fuel_tank),
        ):
            if tank.wall_thickness_m >= lower:
                continue
            diagnostics.append(
                _hard(
                    CODE_TANK_WALL,
                    f"stages[{position}].geometry.{key}.wall_thickness_m",
                    f"第 {stage.index} 级 {role}壁厚 {tank.wall_thickness_m} m 小于"
                    f"最小工艺厚度 {lower} m",
                )
            )
    return RuleOutcome(**outcome, diagnostics=tuple(diagnostics), uncovered=scope)


def _length_to_diameter_rule(
    result: dag.PropagationResult, limits: DiagnosticThresholds, *, indices: tuple[int, ...]
) -> RuleOutcome:
    """长径比 ∈ [下界, 上界]（警）。取 DAG 的 ``stage{i}.length_to_diameter``。"""
    outcome = _rule_head(
        CODE_LENGTH_TO_DIAMETER,
        "长径比",
        "warning",
        f"L/D ∈ [{limits.length_to_diameter_min}, {limits.length_to_diameter_max}]（每级）",
        SOURCE_CONVENTION,
    )
    low, high = limits.length_to_diameter_min, limits.length_to_diameter_max
    diagnostics: list[Diagnostic] = []
    missing: list[str] = []
    for position, index in enumerate(indices):
        node = f"{dag.stage_prefix(index)}.length_to_diameter"
        value = result.values.get(node)
        if value is None:
            missing.append(node)
            continue
        if low <= value <= high:
            continue
        diagnostics.append(
            _warn(
                CODE_LENGTH_TO_DIAMETER,
                f"stages[{position}].length_m",
                f"第 {index} 级长径比 {value:.2f} 超出区间 [{low}, {high}]",
                "调整分级或直径；长径比过大有纵向振动风险（阈值是工程惯例值，非权威来源）",
            )
        )
    if missing:
        return RuleOutcome(
            **outcome,
            deferred_reason=f"缺派生量 {missing}（需要该级的 length_m 与 diameter_m）",
        )
    return RuleOutcome(**outcome, diagnostics=tuple(diagnostics))


def run_diagnostics(
    vehicle: Vehicle,
    *,
    propellant_mass_kg: Mapping[int, float] | None = None,
    thresholds: DiagnosticThresholds | None = None,
) -> DiagnosticsReport:
    """跑完 §6.5 规则集（M2 可判的四条 + 显式 deferred 的四条）。

    ``propellant_mass_kg``（级号 → 质量）由 M4 的定尺求解提供；M2 不提供时依赖它的
    规则（推重比）整体进 ``deferred_reason``，**不填默认值**。
    """
    limits = thresholds or DiagnosticThresholds()
    result = dag.propagate_vehicle(vehicle, propellant_mass_kg=propellant_mass_kg)
    indices = tuple(stage.index for stage in vehicle.stages)

    rules = (
        _twr_rule(result, limits),
        _structure_mass_ratio_rule(result, limits, first_stage=indices[0]),
        _tank_wall_rule(vehicle, limits),
        _length_to_diameter_rule(result, limits, indices=indices),
        _deferred(
            CODE_DV_ALLOCATION,
            "级间 ΔV 分配",
            "warning",
            reason=(
                "需要一个 Δv 预算与 §8.5 的最优分配结果；Δv 与质量迭代属 M4"
                "（M2 只存透传 mission.loss_factors）"
            ),
            threshold=f"相对最优分配偏离 ≤ {limits.dv_allocation_deviation_max:.0%}",
        ),
        _deferred(
            CODE_UPPER_STAGE_MARGIN,
            "上面级余量",
            "warning",
            reason="需要分离后的剩余 Δv 与上面级 Δv 需求，属 M4",
            threshold=(
                f"Δv 余量 ∈ [{limits.upper_stage_margin_min:.0%}, "
                f"{limits.upper_stage_margin_max:.0%}]"
            ),
        ),
        _deferred(
            CODE_CAPACITY_GAP,
            "运力缺口",
            "hard",
            reason=(
                "需要估算运力（§8.3 定尺求解 / §8.10 轨道运力），属 M4；"
                "并按 OI-11 须由 §8.7 敏感度给出可改进参数，M2 不得编造"
            ),
            threshold="估算运力 ≥ 载荷需求",
        ),
        _deferred(
            CODE_NOZZLE_DIAMETER,
            "喷管出口直径 < 级直径",
            "warning",
            reason=(
                "§6.3 相容约束举例之一，但出口直径需要 §8.4 喷管型面（面积比 + 室压）的结果，属 M4"
            ),
            threshold="喷管出口直径 < 该级直径",
        ),
    )

    diagnostics = tuple(item for rule in rules for item in rule.diagnostics)
    return DiagnosticsReport(diagnostics=diagnostics, rules=rules)
