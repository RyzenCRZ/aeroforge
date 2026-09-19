"""单位系统（规格 §6.4 / OI-07）。

口径
----
- 内部一律 SI（红线 §1.4-3）；界面显示单位**固定一套，不做用户可切换**（OI-07）。
- **换算只在 API 边界发生**（原则 P1：禁止界面层做单位换算）。本模块就是那处边界的
  唯一实现——组件里不得再出现任何换算系数。
- 显示单位是**呈现口径**：不改变存储、计算与缓存键。缓存键里的 canonical JSON
  **一律是 SI**，故调整本表**不得**使 ``artifacts/`` 缓存失效（§9.2）。

与 §6.4 表的关系
----------------
:data:`DISPLAY_UNITS` 与规格 §6.4 的表格**逐行对应**，由
``backend/tests/unit/test_params_spec_contract.py`` 机检——表在文档里改了而代码没跟，
门禁即失败（同 ``test_perf_budget`` 的口径）。

未纳入本表的量（以及为什么）
----------------------------
- **面积（m²）/ 时间（s）/ 角度（°）**：显示单位与 SI 一致或为 SI 的导出单位，**没有换算可言**，
  故不设条目（换算表只登记"显示 ≠ 内部"以及 §6.4 已明确的量）。
- ⚠ 角度按工程惯例以**度**存储，字段名一律带 ``_deg`` 后缀自证量纲（纬度 / 方位角 / 倾角）。
  规格 §6.4 未给角度行，故本工程**不引入**度↔弧度的隐式换算：任何地方看到 ``_deg`` 就是度。
- ⚠ 新增显示单位必须**先回填规格 §6.4 表**再落到本模块，不得在组件里就地格式化。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field
from pydantic_core import PydanticUndefined

#: §6.4 已明确的量。键即规格表的「量」列（中文），以便机检逐行对照。
Quantity = Literal[
    "force",
    "mass",
    "length",
    "isp",
    "velocity",
    "pressure",
    "temperature",
    "density",
]


@dataclass(frozen=True, slots=True)
class DisplayUnit:
    """一个量的显示口径。

    ``factor`` 的定义方向刻意固定为 **SI = 显示值 × factor**：方向一旦不统一，
    调用点就得逐个记"这一次是乘还是除"，而这正是最容易写反的地方。
    """

    quantity: Quantity
    label: str
    """规格 §6.4 表的「量」列原文（中文）。"""
    symbol: str
    """显示单位符号（界面与导出用）。"""
    si_symbol: str
    """内部 SI 符号（存储 / 计算 / 缓存键用）。"""
    factor: float
    """SI = 显示值 × factor。"""


DISPLAY_UNITS: dict[Quantity, DisplayUnit] = {
    "force": DisplayUnit("force", "力 / 推力", "kN", "N", 1e3),
    "mass": DisplayUnit("mass", "质量", "t", "kg", 1e3),
    "length": DisplayUnit("length", "长度", "m", "m", 1.0),
    "isp": DisplayUnit("isp", "比冲", "s", "s", 1.0),
    "velocity": DisplayUnit("velocity", "ΔV / 速度", "km·s⁻¹", "m·s⁻¹", 1e3),
    "pressure": DisplayUnit("pressure", "压力", "bar", "Pa", 1e5),
    "temperature": DisplayUnit("temperature", "温度", "K", "K", 1.0),
    "density": DisplayUnit("density", "密度", "kg·m⁻³", "kg·m⁻³", 1.0),
}


def to_display(quantity: Quantity, si_value: float) -> float:
    """SI → 显示值（只在 API 出参处调用）。"""
    return si_value / DISPLAY_UNITS[quantity].factor


def to_si(quantity: Quantity, display_value: float) -> float:
    """显示值 → SI（只在 API 入参处调用）。"""
    return display_value * DISPLAY_UNITS[quantity].factor


def si_field(
    quantity: Quantity,
    description: str,
    *,
    default: Any = PydanticUndefined,
    **constraints: Any,
) -> Any:
    """声明一个 **SI 存储** 的字段，并把单位随 OpenAPI 一起下发（§6.1「字段带 unit 元数据」）。

    返回 ``Any`` 是刻意的：pydantic 的 ``Field()`` 在类型层面就是 ``Any``，
    这样 ``x: float = si_field(...)`` 在 ``mypy --strict`` 下与 ``x: float = Field(...)`` 等价。
    前端据此显示 ``display_unit``，**不需要**自己维护一张换算表。

    ``default`` 必须**显式**传（``default=None`` 表示"可省略"）：pydantic 对
    ``float | None = Field(...)`` 的判定是**必填**——签名上的 ``| None`` 只说明值可为 null，
    不说明可以省略。曾因此让 ``Tank.diameter_m`` 等 11 个"可省略"字段在 OpenAPI 里
    变成 required（文档写着"省略 = 继承该级直径"，契约却要求必填）。
    """
    unit = DISPLAY_UNITS[quantity]
    return Field(
        default=default,
        description=description,
        json_schema_extra={"unit": unit.si_symbol, "display_unit": unit.symbol},
        **constraints,
    )
