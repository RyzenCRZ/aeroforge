"""参数域的统一报告结构（规格 §6.3 约束 / §6.5 方案诊断）。

为什么必须只有一个结构
----------------------
§6.3 结尾与 §6.5 结尾都写明输出统一为 ``{level, code, field_path, message, suggestion}``
（§6.5 的 ``impact`` 按 OI-11 归 M4，M2 不得用经验系数编造），即**约束层与诊断层同形**。
故本模块定义唯一的 :class:`Diagnostic`，两处共用——否则前端要同时解析两种形状的错误。

本模块是**叶子模块**：不 import 本包其他模块。这是刻意的——否则会出现
``schema → diagnostics → schema`` / ``constraints → schema → constraints`` 这类导入环
（pyproject 的 ruff 规则会报 I001，人却容易看漏）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: 裁定级别。只允许两种，刻意不设 ``info``：
#: §6.3 把约束分「硬约束 → 拒绝」与「工程约束 → 警告」，§6.5 表的「级别」列同样只有「硬 / 警」。
#: 多一个级别就等于多一种"既不拒也不警"的形态，而"没报错 ≠ 正确"正是本项目最贵的一课。
Level = Literal["hard", "warning"]


class Diagnostic(BaseModel):
    """一条字段级裁定（校验错误或方案诊断）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: Level = Field(description="裁定：hard = 拒绝（硬约束）；warning = 警告")
    code: str = Field(description="机器可读的判定码，如 PARAMS_FILL_OVERFILL / TWR_TOO_LOW")
    field_path: str = Field(
        description="字段路径（如 stages[0].engine.mixture_ratio）；整箭层判定用 vehicle 前缀"
    )
    message: str = Field(description="面向用户的中文说明：实测值是多少、为什么不行")
    suggestion: str = Field(description="可操作的修复建议（§10.3 要求必填，禁止「未知错误」）")


def has_hard(diagnostics: Iterable[Diagnostic]) -> bool:
    """是否存在需要拒绝请求的硬约束违反。"""
    return any(item.level == "hard" for item in diagnostics)
