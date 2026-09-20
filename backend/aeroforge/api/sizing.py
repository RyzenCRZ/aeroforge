"""定尺求解域端点（规格 §8.5 / §8.9 / §10.1）。

- ``POST /api/sizing/solve`` —— 多级质量迭代（**同步、纯数值、无 OCCT**）。
- ``POST /api/sizing/sequence`` —— 任务时序与质量事件耦合（§8.9，**同步**：
  solve + 时序账 + 回收代价回写，毫秒级纯数值）。

为什么独立成模块而不挂 ``api/params.py``：main.py 的路由按域注册
（geometry / params / catalog / jobs / artifacts / importers），定尺求解是
§8.5 的计算域而非参数域——挂进 params 会让"参数结构校验"与"求解"两种 422
混在同一 stage 标签下，§10.3 的 ``stage`` 就失去了定位作用。

请求体口径
----------
- ``vehicle``：完整 Vehicle（§6.1）。
- ``target_delta_v_m_s``：目标总 ΔV——**请求体显式携带**。Schema 现状里
  Mission 层没有 ΔV 字段（只有 loss_factors 损失系数，M2 只存储透传），§8.6
  的损失预算 / 轨道 ΔV 需求表属后续片，故目标 ΔV 由调用方直接给出。
- ``stage_delta_v_m_s``（可选）：用户显式给各级 ΔV（逐芯级、自下而上；
  助推器构型下一级的预算 = 0 级段 + 芯级段之和）。

⚠ 本模块的 handler **不调用 OCCT、不访问目录库**：求解是毫秒级纯数值
（§10.1「同步」），GCAT 回归对照（§8.4）属库侧离线产物，不进本请求路径。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.errors import ParamsError
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Vehicle
from aeroforge.perf.sequence import SequenceReport, apply_sequence
from aeroforge.perf.solver import SizingResult, solve

router = APIRouter(tags=["sizing"])


class SizingSolveRequest(BaseModel):
    """``POST /api/sizing/solve`` 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="飞行器参数（§6.1 全量）")
    target_delta_v_m_s: float = Field(
        gt=0.0,
        description="目标总 ΔV（m/s，真空口径；损失预算属 §8.6 另片，由调用方计入）",
    )
    stage_delta_v_m_s: tuple[Annotated[float, Field(gt=0.0)], ...] | None = Field(
        default=None,
        description=(
            "用户显式的逐级 ΔV（m/s，自下而上、逐芯级；求和须等于目标总 ΔV；"
            "省略 = Lagrange √Isp 加权初值分配，§8.5）"
        ),
    )


@router.post("/api/sizing/solve", response_model=SizingResult)
def solve_sizing(request: SizingSolveRequest) -> SizingResult:
    """多级质量迭代求解（§8.5：外层 GLOW 割线 + 内层自上而下，同步纯数值）。

    硬约束违反（Isp custom 缺值、材料不在库等）沿用参数域的拒绝口径
    （``PARAMS_CONSTRAINT_VIOLATION`` → 422，与 ``/api/params/diagnose`` 同判据）；
    求解域自身的问题（无物理解 / 不收敛）由 :class:`~aeroforge.errors.SizingError`
    给出带残差轨迹的 422——不静默给半收敛结果。
    """
    violations = check_vehicle(request.vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝进入定尺求解",
            suggestion=hard[0].suggestion,
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )
    return solve(
        request.vehicle,
        request.target_delta_v_m_s,
        stage_delta_v_m_s=request.stage_delta_v_m_s,
    )


class SizingSequenceRequest(BaseModel):
    """``POST /api/sizing/sequence`` 的请求体（§8.9 / §10.1：同步）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(
        description=(
            "飞行器参数（§6.1 全量；Sequence 层事件与 Recovery 层代价配置内嵌其中——"
            "QA-4 的占位挂点自本片起启用）"
        )
    )
    target_delta_v_m_s: float = Field(
        gt=0.0,
        description="目标总 ΔV（m/s，真空口径；与 /api/sizing/solve 同口径）",
    )
    stage_delta_v_m_s: tuple[Annotated[float, Field(gt=0.0)], ...] | None = Field(
        default=None,
        description="用户显式的逐级 ΔV（m/s，自下而上；省略 = Lagrange √Isp 分配）",
    )


@router.post("/api/sizing/sequence", response_model=SequenceReport)
def solve_sequence(request: SizingSequenceRequest) -> SequenceReport:
    """任务时序与质量事件耦合（§8.9：solve → 事件账 + 回收代价回写，同步）。

    时序（Sequence 层）缺失时按构型合成缺省事件线；回收（Recovery 层）启用时
    三项独立代价进 σ_eff 回写（规则 1 / 3），回收点火的运力代价由
    ``capacity_penalty_kg`` 量化（「直接减少运力」，§8.9 表）。抛罩时刻动压 /
    q̇ 超限只警告不中止（规则 2——时序是用户权威）。
    """
    violations = check_vehicle(request.vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝进入任务时序分析",
            suggestion=hard[0].suggestion,
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )
    sizing = solve(
        request.vehicle,
        request.target_delta_v_m_s,
        stage_delta_v_m_s=request.stage_delta_v_m_s,
    )
    return apply_sequence(
        request.vehicle,
        sizing,
        target_delta_v_m_s=request.target_delta_v_m_s,
        stage_delta_v_m_s=request.stage_delta_v_m_s,
    )
