"""阈值配置的读写（规格 §1.7.3 OI-31 / §6.5 / §18.4）。

为什么单开一个模块
------------------
:mod:`aeroforge.params.diagnostics` 管的是「规则怎么判」，本模块管的是
「阈值写在哪、当前生效的是哪一个、从哪来」。二者混在一起会让那个模块同时依赖
文件系统与规则逻辑，而**配置读写正是最容易悄悄出错的一层**——写错一个键名，
界面上"保存成功"，实际什么都没生效。

来源优先级（§18.4，不得更改）
-----------------------------
``环境变量 > config.toml > 内置默认值``。故本模块对外的口径一律是**生效值**
（:func:`load_thresholds` 构造的就是叠加后的结果），并额外给出每一项的
``origin``——界面必须显示生效值与来源，否则"保存了却不生效"（被环境变量压住）
会表现为一个无法解释的现象。

``config.toml`` 的形状
----------------------
**顶层扁平键**（没有 ``[table]``）。pydantic-settings 的 ``TomlConfigSettingsSource``
按模型字段名取顶层键，扁平写法与之天然一致；且**未声明的顶层键会被忽略**——
用户手工加注释、加自己的键都不会让应用崩掉（用 ``toml_table_header`` 反而会在
文件存在而缺少该段时抛 ``KeyError``，把一个可恢复的编辑事故变成启动失败）。

写入保持**最小改动**：只改本次涉及的键所在行，其余内容（含注释与空行）原样保留。
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aeroforge.errors import ConfigError
from aeroforge.params.diagnostics import (
    SOURCE_CONVENTION,
    SOURCE_PROCESS_LIMIT,
    DiagnosticThresholds,
)
from aeroforge.params.schema import errors_to_diagnostics
from aeroforge.paths import config_file

#: 环境变量前缀（与 :class:`DiagnosticThresholds` 的 ``env_prefix`` 必须一致）。
ENV_PREFIX = "AEROFORGE_"

#: ``origin`` 的四种取值（界面按此显示"生效值从哪来"）。
ORIGIN_ENV = "环境变量"
ORIGIN_FILE = "config.toml"
ORIGIN_DEFAULT = "默认值"
ORIGIN_UNSET = "未配置"

#: 新建 ``config.toml`` 时写入的文件头。指向模板文件，避免用户只能靠猜。
_HEADER = """\
# AeroForge 配置（由界面「阈值设置」写入，也可手工编辑）
# 来源优先级：环境变量 > 本文件 > 内置默认值（规格 §18.4）
# ⚠ 下列均为「工程惯例值，非权威来源」，不得引用为标准或论文结论（规格 §6.5）
# 完整说明与生效边界见仓库根目录的 config.example.toml
"""


@dataclass(frozen=True, slots=True)
class ThresholdSpec:
    """一个可配置阈值的元数据（**不含数值**——数值的唯一来源是模型字段本身）。"""

    key: str
    """与 :class:`DiagnosticThresholds` 字段名逐字一致（由门禁强制）。"""
    label: str
    """界面上的中文名（§6.5 表的「规则」列口径）。"""
    source: str
    """§6.5 强制的来源标注。"""
    note: str
    """生效边界，写进 ``config.example.toml`` 的注释。"""


#: 阈值清单。**顺序即界面与模板中的呈现顺序**。此处刻意不写默认值：
#: 默认值的唯一来源是 ``DiagnosticThresholds`` 的字段定义，抄一份到本表即形成两份真相。
THRESHOLD_SPECS: tuple[ThresholdSpec, ...] = (
    ThresholdSpec(
        "twr_liquid_min",
        "起飞推重比下界（液体）",
        SOURCE_CONVENTION,
        "档位按**第一级**发动机的 propellant_phase 选择：liquid / hybrid 用本值（§1.7.3 OI-30）",
    ),
    ThresholdSpec(
        "twr_solid_min",
        "起飞推重比下界（固体助推档）",
        SOURCE_CONVENTION,
        "第一级相态为 solid 时用本值；档位与依据会印在推重比规则的账目里，故非隐式判定",
    ),
    ThresholdSpec(
        "structure_mass_ratio_min",
        "结构质量比下界（一级）",
        SOURCE_CONVENTION,
        "判据用 σ 的派生量 σ/(1−σ)，只判一级；其余级由 §6.3 硬约束把守",
    ),
    ThresholdSpec(
        "structure_mass_ratio_max",
        "结构质量比上界（一级）",
        SOURCE_CONVENTION,
        "同上",
    ),
    ThresholdSpec(
        "length_to_diameter_min",
        "长径比下界",
        SOURCE_CONVENTION,
        "逐级判定",
    ),
    ThresholdSpec(
        "length_to_diameter_max",
        "长径比上界",
        SOURCE_CONVENTION,
        "过大有纵向振动风险；逐级判定",
    ),
    ThresholdSpec(
        "min_tank_wall_thickness_m",
        "贮箱最小工艺厚度（m）",
        SOURCE_PROCESS_LIMIT,
        "**留空 = 回落各箱材料的典型工艺下限**（警告级，§7.4 QA-3；材料未知才不判定并留痕），"
        "绝不填一个凭空的毫米数；量级参照：NASA Centaur 不锈钢气球箱 0.51 mm（0.020 in，§1.7.3）",
    ),
    ThresholdSpec(
        "dv_allocation_deviation_max",
        "级间 ΔV 分配允许偏离（相对）",
        SOURCE_CONVENTION,
        "判定属 M4（需 Δv 预算与最优分配）",
    ),
    ThresholdSpec(
        "upper_stage_margin_min",
        "上面级 Δv 余量下界",
        SOURCE_CONVENTION,
        "判定属 M4",
    ),
    ThresholdSpec(
        "upper_stage_margin_max",
        "上面级 Δv 余量上界",
        SOURCE_CONVENTION,
        "判定属 M4",
    ),
)

#: 合法键集合——``PUT`` 遇到集合外的键即拒绝（拒绝而不是忽略）。
KEY_SET: frozenset[str] = frozenset(spec.key for spec in THRESHOLD_SPECS)


class ThresholdEntry(BaseModel):
    """一个阈值的**生效**状态（界面设置项直接消费）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(description="配置键（config.toml 的顶层键名）")
    label: str = Field(description="中文名")
    value: float | None = Field(description="**生效值**；null = 未配置（该判据不判定）")
    source: str = Field(description="§6.5 强制的来源标注（界面不得自拟措辞）")
    origin: str = Field(
        description=f"生效值的来源：{ORIGIN_ENV}/{ORIGIN_FILE}/{ORIGIN_DEFAULT}/{ORIGIN_UNSET}"
    )
    note: str = Field(description="生效边界说明")


def _top_level_key(line: str) -> str | None:
    """顶层赋值行的键名；空行 / 注释 / 非赋值行返回 ``None``。"""
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    return match.group(1) if match else None


def _format_value(value: float) -> str:
    """TOML 字面量。``repr`` 对 float 是往返精确的，故写回再读回恒等。"""
    return repr(float(value))


def _read_config_text() -> str:
    path = config_file()
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _parse(text: str) -> dict[str, object]:
    """解析配置文本；语法错即抛 :class:`ConfigError`（**绝不**把坏文件改得更坏）。"""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"config.toml 不是合法的 TOML：{exc}",
            suggestion=(
                "用文本编辑器打开 config.toml 修正语法（常见：值缺引号、重复键、"
                "中文全角引号）；也可先删掉该文件，应用会按内置默认值运行"
            ),
            details={"config_file": str(config_file())},
        ) from exc


def _validate(known: Mapping[str, object]) -> None:
    """写盘前**先过产品自己的校验器**（§13.8 的夹具教训：校验器与产品必须同一套）。

    只把**文件里的**键交给模型（``init`` 优先级最高，故环境变量不会掩盖文件里的错误）。
    """
    try:
        DiagnosticThresholds.model_validate(dict(known))
    except ValidationError as exc:
        raise ConfigError(
            "阈值取值不合法，已拒绝写入（config.toml 未被修改）",
            suggestion="按 details.diagnostics 的字段级提示改正取值后重试",
            details={
                "config_file": str(config_file()),
                "diagnostics": [
                    item.model_dump(mode="json") for item in errors_to_diagnostics(exc.errors())
                ],
            },
        ) from exc


def load_thresholds() -> DiagnosticThresholds:
    """构造叠加后的阈值集（``环境变量 > config.toml > 默认值``）。

    端点与诊断都走这里而不是直接 ``DiagnosticThresholds()``：配置出错时要给的是
    "哪一行写错了"，而不是一句 ``INTERNAL_ERROR``（§10.3 禁止「未知错误」）。
    """
    try:
        return DiagnosticThresholds()
    except ValidationError as exc:
        raise ConfigError(
            "config.toml 的阈值取值不合法",
            suggestion="按 details.diagnostics 的字段级提示改正取值，或删除该键改用默认值",
            details={
                "config_file": str(config_file()),
                "diagnostics": [
                    item.model_dump(mode="json") for item in errors_to_diagnostics(exc.errors())
                ],
            },
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"config.toml 不是合法的 TOML：{exc}",
            suggestion=("用文本编辑器修正语法；也可先删掉该文件，应用会按内置默认值运行"),
            details={"config_file": str(config_file())},
        ) from exc


def _origin(key: str, value: float | None, file_keys: frozenset[str]) -> str:
    """生效值来自哪一层。顺序必须与 §18.4 的优先级一致，否则会指错来源。"""
    # Windows 的 os.environ 大小写不敏感，故这一查与 pydantic-settings 的
    # ``case_sensitive=False`` 语义一致（§18.4 约定的名字即字段名大写加前缀）。
    if f"{ENV_PREFIX}{key.upper()}" in os.environ:
        return ORIGIN_ENV
    if key in file_keys:
        return ORIGIN_FILE
    if value is None:
        return ORIGIN_UNSET
    return ORIGIN_DEFAULT


def threshold_entries(limits: DiagnosticThresholds | None = None) -> tuple[ThresholdEntry, ...]:
    """全部阈值的生效状态（含「未配置」）。"""
    resolved = limits if limits is not None else load_thresholds()
    text = _read_config_text()
    file_keys = frozenset(_parse(text)) if text else frozenset()
    entries: list[ThresholdEntry] = []
    for spec in THRESHOLD_SPECS:
        value = getattr(resolved, spec.key)  # 键名与字段名一致由门禁测试强制
        entries.append(
            ThresholdEntry(
                key=spec.key,
                label=spec.label,
                value=value,
                source=spec.source,
                origin=_origin(spec.key, value, file_keys),
                note=spec.note,
            )
        )
    return tuple(entries)


def _apply_patch(text: str, patch: Mapping[str, float | None]) -> str:
    """在**保留其余内容**的前提下改动涉及的顶层键。

    - 键已存在：原位替换；值为 ``null`` 时整行删除（= 回到「未配置」）。
    - 键不存在：插到**第一个 ``[table]`` 之前**——插到文件末尾会把它落进那个表里，
      而那正是"看起来写进去了、其实读不到"的形态。
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    remaining = dict(patch)
    kept: list[str] = []
    insert_at: int | None = None
    in_table = False

    for line in lines:
        if not in_table and re.match(r"^\s*\[", line):
            in_table = True
            insert_at = len(kept)
        key = None if in_table else _top_level_key(line)
        if key is not None and key in remaining:
            value = remaining.pop(key)
            if value is None:
                continue
            kept.append(f"{key} = {_format_value(value)}")
            continue
        kept.append(line)

    if insert_at is None:
        insert_at = len(kept)
    additions = [
        f"{key} = {_format_value(value)}" for key, value in remaining.items() if value is not None
    ]
    kept[insert_at:insert_at] = additions

    body = newline.join(kept).rstrip("\r\n")
    return f"{body}{newline}" if body else ""


def patch_thresholds(patch: Mapping[str, float | None]) -> Path:
    """把补丁写进 ``config.toml``，返回写入的路径。

    写前两道关：① 现有文件必须能解析；② 结果必须过产品校验器。任一不过即**不改动文件**
    ——半个合法的配置文件比一个明摆着写错的更难查。
    """
    unknown = sorted(set(patch) - KEY_SET)
    if unknown:
        raise ConfigError(
            f"config.toml 没有这些阈值项：{unknown}",
            suggestion=f"可用键见 config.example.toml；合法键集合为 {sorted(KEY_SET)}",
        )

    path = config_file()
    text = _read_config_text()
    if text:
        _parse(text)  # 现有文件不能有语法错，否则改完仍是坏的
        updated = _apply_patch(text, patch)
    else:
        additions = [
            f"{key} = {_format_value(value)}" for key, value in patch.items() if value is not None
        ]
        updated = _HEADER + "\n" + "\n".join(additions) + "\n" if additions else _HEADER

    _validate(_parse(updated))

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8", newline="")
    return path
