"""参数域端点（规格 §6.3 / §6.5 / §10.1）。

``POST /api/params/diagnose`` —— 方案诊断与改进建议，同步。

两条通路（§6.3 的「处理」列决定了它们必须分开）
----------------------------------------------
1. **硬约束被违反 → 拒绝**：抛 :class:`~aeroforge.errors.ParamsError`
   （``PARAMS_CONSTRAINT_VIOLATION`` → 422，§10.3 结构），把**六字段裁定清单**
   放进 ``details.diagnostics``。拒绝而不是"带警告放行"——放行只会把错误推到更晚、
   更难定位的地方。
2. **无硬违反 → 200**：返回 §6.3 的警告（工程 / 相容 / 安全边界）与 §6.5 的诊断
   **合并成一张清单**，外加 §6.5 的逐规则账目 ``rules``。

为什么两张清单要合并
--------------------
§6.3 与 §6.5 的输出结构刻意同形（见 :mod:`aeroforge.params.report`）。若在响应里分成
两个数组，前端就得维护两套渲染与两套"哪条更严重"的排序规则——而同形结构本可以让它
只遍历一次。

⚠ 本模块的 handler **不调用 OCCT**（同 §9.1 规则 1 的口径）：诊断全部是纯数值与
Schema 判定，可高频调用。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from aeroforge.errors import ParamsError
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.diagnostics import RuleOutcome, run_diagnostics
from aeroforge.params.report import Diagnostic, has_hard
from aeroforge.params.schema import Vehicle

router = APIRouter(tags=["params"])


class DiagnoseResponse(BaseModel):
    """``POST /api/params/diagnose`` 的响应体。

    ``constraints`` 只可能含**非硬**裁定：硬违反一律走 422，故前端在 200 里看到的
    每一条都是"警告级或更轻"，不需要再判一次"是不是该拒绝"。
    """

    constraints: tuple[Diagnostic, ...] = Field(
        description="§6.3 的四类约束裁定（进入 200 时必为非硬）"
    )
    diagnostics: tuple[Diagnostic, ...] = Field(
        description="§6.5 规则集产出的诊断（六字段；impact 按 OI-11 归 M4）"
    )
    rules: tuple[RuleOutcome, ...] = Field(
        description="§6.5 逐规则账目，含**未判定**的规则与其原因（不得省略）"
    )


@router.post("/api/params/diagnose", response_model=DiagnoseResponse)
def diagnose(vehicle: Vehicle) -> DiagnoseResponse:
    """跑 §6.3 约束 + §6.5 诊断规则集。

    结构错误（缺字段 / 类型不符 / 超出枚举）在进入本函数之前就被 pydantic 拦下，
    由 ``main`` 的 ``RequestValidationError`` 处理器翻成同一形状的六字段裁定。
    """
    violations = check_vehicle(vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝进入诊断",
            suggestion=hard[0].suggestion,
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )

    report = run_diagnostics(vehicle)
    return DiagnoseResponse(
        constraints=tuple(violations),
        diagnostics=report.diagnostics,
        rules=report.rules,
    )
