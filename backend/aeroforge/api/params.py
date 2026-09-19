"""参数域端点（规格 §6.3 / §6.4 / §6.5 / §10.1）。

- ``POST /api/params/diagnose`` —— 方案诊断与改进建议，同步。
- ``GET  /api/params/units`` —— §6.4 显示单位全表（前端零换算系数，§1.7.3 OI-32）。
- ``GET  /api/params/thresholds`` —— §6.5 阈值的**生效值**与来源（含「未配置」，OI-31）。
- ``PUT  /api/params/thresholds`` —— 写入 ``config.toml``（§18.4 优先级不变）。
- ``GET  /api/params/template`` —— 界面可编辑的起始箭（示例骨架，未经来源核对）。

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
from aeroforge.params import template as template_module
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.diagnostics import RuleOutcome, run_diagnostics
from aeroforge.params.report import Diagnostic, has_hard
from aeroforge.params.schema import Vehicle
from aeroforge.params.thresholds import (
    ThresholdEntry,
    load_thresholds,
    patch_thresholds,
    threshold_entries,
)
from aeroforge.params.units import DISPLAY_UNITS, Quantity
from aeroforge.paths import config_file

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

    report = run_diagnostics(vehicle, thresholds=load_thresholds())
    return DiagnoseResponse(
        constraints=tuple(violations),
        diagnostics=report.diagnostics,
        rules=report.rules,
    )


class DisplayUnitOut(BaseModel):
    """一个量的显示口径（§6.4 表逐行）。"""

    quantity: Quantity = Field(description="量（规格 §6.4 表的「量」列）")
    label: str = Field(description="中文名（规格原文）")
    symbol: str = Field(description="显示单位符号（界面与导出用）")
    si_symbol: str = Field(description="内部 SI 符号（存储 / 计算 / 缓存键用）")
    factor: float = Field(
        description="换算系数，方向固定 **SI = 显示值 × factor**（方向写反是本表最易犯的错）"
    )


class UnitsResponse(BaseModel):
    """``GET /api/params/units`` 的响应体（§1.7.3 OI-32）。"""

    units: tuple[DisplayUnitOut, ...] = Field(
        description="§6.4 全表；界面**只按此格式化**，不得在前端再抄一份表或反推系数"
    )


@router.get("/api/params/units", response_model=UnitsResponse)
def units() -> UnitsResponse:
    """下发 §6.4 的显示单位全表。

    这是**只读呈现口径**：不改变存储 / 计算 / 缓存键，故调整显示单位**不得**使
    ``artifacts/`` 缓存失效（§9.2）。
    """
    return UnitsResponse(
        units=tuple(
            DisplayUnitOut(
                quantity=unit.quantity,
                label=unit.label,
                symbol=unit.symbol,
                si_symbol=unit.si_symbol,
                factor=unit.factor,
            )
            for unit in DISPLAY_UNITS.values()
        )
    )


class ThresholdsResponse(BaseModel):
    """``GET`` / ``PUT /api/params/thresholds`` 的响应体（§1.7.3 OI-31）。"""

    config_file: str = Field(description="写入目标：config.toml 的完整路径（§18.4）")
    thresholds: tuple[ThresholdEntry, ...] = Field(
        description="各项的**生效值**与来源；value 为 null 表示「未配置」（该判据不判定）"
    )


@router.get("/api/params/thresholds", response_model=ThresholdsResponse)
def read_thresholds() -> ThresholdsResponse:
    """返回阈值的生效值与来源。

    刻意返回**生效值**而不是"文件里的值"：``PUT`` 之后环境变量仍然覆盖 ``config.toml``，
    若界面显示的是文件内容，用户会看到一个"保存了却不生效"的数字而找不到原因。
    """
    return ThresholdsResponse(
        config_file=str(config_file()),
        thresholds=threshold_entries(),
    )


@router.put("/api/params/thresholds", response_model=ThresholdsResponse)
def write_thresholds(patch: dict[str, float | None]) -> ThresholdsResponse:
    """把补丁写进 ``config.toml``，然后返回**重新读出的**生效状态。

    请求体是扁平的 ``{键: 值}``；值为 ``null`` 即**清空该键**（回到「未配置」，
    对最小工艺厚度而言就是回到"不判定"）。键必须是 §6.5 已声明的阈值——
    多一个键即 422，而不是静默忽略（忽略会让用户以为保存成功）。
    """
    patch_thresholds(patch)
    return ThresholdsResponse(
        config_file=str(config_file()),
        thresholds=threshold_entries(),
    )


class TemplateResponse(BaseModel):
    """``GET /api/params/template`` 的响应体（界面可编辑的起始箭）。"""

    template_id: str = Field(description="骨架标识")
    label: str = Field(description="界面显示名（含「未经来源核对」字样）")
    note: str = Field(description="必须原样呈现的说明；不得改写为更肯定的措辞")
    sourced_fields: dict[str, str] = Field(
        description=(
            "有出处的字段路径 → 出处。**不在本表里的数值一律是占位值**（§1.4-4）；"
            "路径口径与 §6.3 / §6.5 的 field_path 逐字一致"
        )
    )
    vehicle: Vehicle = Field(description="起始箭本体（结构合法，可直接提交诊断）")


@router.get("/api/params/template", response_model=TemplateResponse)
def read_template() -> TemplateResponse:
    """返回界面可编辑的示例骨架（§1.7.3 OI-32 的前置）。

    ⚠ 这**不是** OI-29 的内置示例模板——模板库须逐条核对公开来源，归 M3。
    本端点只提供一份标注为「未经来源核对」的骨架，使参数面板不必自建一份
    "看起来像真的"的数字（那会形成与 §13.2 基准表冲突的第二套数字，违反 P1）。
    """
    return TemplateResponse(
        template_id=template_module.TEMPLATE_ID,
        label=template_module.LABEL,
        note=template_module.NOTE,
        sourced_fields=dict(template_module.SOURCED_FIELDS),
        vehicle=template_module.skeleton_vehicle(),
    )
