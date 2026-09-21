"""轨道精算域端点（规格 §8.10，M6 轨道层第一片）。

- ``POST /api/orbits/transfer`` —— §8.10 全项解析闭式的独立端点：Hohmann 两脉冲、
  轨道圆化、平面变更、复合机动（圆化 + 平面变更**矢量合成**）、TLI / TMI / 逃逸
  单脉冲射入（C3 与 ΔV 成对输出，TMI 必带窗口/相位假设）。

纯解析毫秒级同步、**不触作业体系**（§9.1 的 worker 与两阶段契约都不涉及——
这里没有 MC、没有进程池、没有缓存，一次几何级求值）。计算本体在
:mod:`aeroforge.perf.orbits`（纯数值、无 FastAPI 依赖），本模块只做请求装配
与 §10.3 错误映射（:class:`~aeroforge.errors.PerfError` → 422）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.errors import PerfError
from aeroforge.perf.orbits import (
    GEO_RADIUS_M,
    MU_EARTH_M3_S2,
    apogee_composite_km_s,
    circular_velocity_km_s,
    circularization_km_s,
    hohmann_transfer_km_s,
    parking_injection_km_s,
    plane_change_km_s,
)

router = APIRouter(tags=["orbits"])

#: 目标型（与 r_a_km 二选一）：GEO = 同步轨道半径；TLI/TMI/escape = 单脉冲射入。
_TransferTarget = Literal["GEO", "TLI", "TMI", "escape"]


class OrbitTransferRequest(BaseModel):
    """``POST /api/orbits/transfer`` 的请求体（§8.10 解析口径）。

    ``r_a_km``（Hohmann / 圆化 / 复合机动的外半径）与 ``target``（GEO / TLI /
    TMI / escape 定型目标）**恰给其一**；``r_p_km`` 是停泊轨道地心半径。
    """

    model_config = ConfigDict(extra="forbid")

    r_p_km: float = Field(
        gt=0.0,
        description="停泊轨道地心半径（km，如 200 km 高度 ≈ 6578.137）",
    )
    r_a_km: float | None = Field(
        default=None,
        gt=0.0,
        description="目标轨道地心半径（km）——Hohmann / 圆化 / 复合机动用；与 target 二选一",
    )
    target: _TransferTarget | None = Field(
        default=None,
        description="定型目标：GEO（同步半径）/ TLI / TMI / escape——与 r_a_km 二选一",
    )
    inclination_deg: float | None = Field(
        default=None,
        ge=0.0,
        le=180.0,
        description="转移轨道倾角（°）——复合机动消倾角用；缺省取 latitude_deg（向东发射自然倾角）",
    )
    latitude_deg: float | None = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description="发射场纬度（°）——inclination_deg 缺省时的自然倾角来源",
    )
    c3_km2_s2: float | None = Field(
        default=None,
        description=(
            "特征能量 C3（km²/s²）——TLI/TMI/escape 的 C3 覆写"
            "（缺省：TLI −1.65、TMI 11.5、escape 0，§8.6 表注口径）"
        ),
    )
    window_assumption: str | None = Field(
        default=None,
        description="窗口/相位假设覆写（TMI；缺省用典型窗口文案——§8.10 约束 4 必填口径）",
    )


class HohmannOut(BaseModel):
    """Hohmann 两脉冲分段输出（§8.10 公式原文）。"""

    dv1_km_s: float = Field(description="第一脉冲（内圆轨道，km/s；下降转移为负）")
    dv2_km_s: float = Field(description="第二脉冲（外圆轨道，km/s；下降转移为负）")
    total_km_s: float = Field(description="两脉冲代数和（km/s）")
    a_t_km: float = Field(description="转移椭圆半长轴（km）")


class CompositeOut(BaseModel):
    """远地点复合机动输出——矢量合成为权威值，标量和仅供差值机检（§8.10 约束 2）。"""

    vector_km_s: float = Field(description="矢量合成脉冲（km/s，权威值）")
    circularization_km_s: float = Field(description="远地点圆化单脉冲（km/s）")
    plane_change_km_s: float = Field(description="单独平面变更脉冲（km/s）")
    scalar_sum_km_s: float = Field(description="标量相加（km/s，禁止口径——仅供差值机检）")
    vector_minus_scalar_km_s: float = Field(description="矢量 − 标量和（km/s，恒 ≤ 0）")
    inclination_deg: float = Field(description="消倾角转角（°）")


class InjectionOut(BaseModel):
    """转移射入输出——C3 与 ΔV 成对（OI-22 / §8.10 约束 1）。"""

    dv_km_s: float = Field(description="停泊轨道单脉冲射入 ΔV（km/s，由 C3 反算）")
    c3_km2_s2: float = Field(description="特征能量 C3 = v∞²（km²/s²；TLI<0，escape≥0）")
    window_assumption: str | None = Field(
        description="窗口/相位假设（TMI 必填；TLI/escape 为 null）"
    )


class OrbitTransferResponse(BaseModel):
    """§8.10 全项解析响应（μ / r_p 随行标注——约束 3；理想脉冲声明在 assumptions）。"""

    r_p_km: float = Field(description="停泊轨道地心半径（km，回显）")
    r_a_km: float | None = Field(description="目标轨道地心半径（km；定型目标换算给出）")
    mu_m3_s2: float = Field(description="所用地心引力常数（m³/s²，WGS-84 GM——约束 3 标注）")
    parking_velocity_km_s: float = Field(description="停泊圆轨道速度 √(μ/r_p)（km/s）")
    hohmann: HohmannOut | None = Field(description="Hohmann 两脉冲（r_a 路径时给出）")
    circularization_km_s: float | None = Field(
        description="远地点圆化单脉冲（km/s，r_a 路径时给出）"
    )
    plane_change_km_s: float | None = Field(
        description="单独平面变更脉冲（km/s，远地点圆轨道速度口径——复合时勿单独相加）"
    )
    composite: CompositeOut | None = Field(
        description="远地点复合机动（圆化+平面变更矢量合成；inclination 缺省且 r_a 路径时给出）"
    )
    injection: InjectionOut | None = Field(
        description="转移射入（TLI/TMI/escape；C3 与 ΔV 成对，TMI 带窗口假设）"
    )
    assumptions: tuple[str, ...] = Field(
        description="模型假设与常量标注（理想脉冲声明 + μ / r_p / r_a 口径，§8.10 约束 1/3）"
    )


@router.post("/api/orbits/transfer", response_model=OrbitTransferResponse)
def orbit_transfer(request: OrbitTransferRequest) -> OrbitTransferResponse:
    """轨道机动解析（§8.10 全项闭式；纯解析毫秒级同步，不触作业体系）。

    ``r_a_km`` 路径：Hohmann 两脉冲 + 远地点圆化 + 平面变更 + 复合矢量合成
    （GEO 直送与 GTO+圆化同口径——target="GEO" 即 r_a 换算为同步半径）。
    ``target="TLI"/"TMI"/"escape"`` 路径：停泊轨道单脉冲射入，C3 与 ΔV 成对输出
    （TMI 的 ``window_assumption`` 必填，缺省给典型窗口文案——§8.10 约束 4）。
    请求自相矛盾（r_a_km 与 target 同给 / 全缺、C3 越域）走
    :class:`~aeroforge.errors.PerfError` → 422。
    """
    r_p_m = request.r_p_km * 1000.0
    given = [request.r_a_km is not None, request.target is not None]
    if all(given):
        raise PerfError(
            "r_a_km 与 target 只能二选一（同给则目标半径口径不唯一）",
            suggestion="定型目标（GEO/TLI/TMI/escape）给 target；自定义半径给 r_a_km",
        )
    if not any(given):
        raise PerfError(
            "r_a_km 与 target 必须恰给其一（§8.10 表按目标半径或定型目标二选一）",
            suggestion="GEO/TLI/TMI/escape 给 target；自定义圆轨道给 r_a_km",
        )

    assumptions: list[str] = [
        f"理想脉冲近似：全部结果不含有限推力损失（有限推力由 §8.6 损失预算覆盖，"
        f"§8.10 约束 1）；μ={MU_EARTH_M3_S2:.6e} m³/s²（WGS-84 GM）、"
        f"停泊半径 r_p={r_p_m:.1f} m（约束 3 标注）"
    ]
    latitude = request.latitude_deg
    inclination = request.inclination_deg if request.inclination_deg is not None else latitude
    if request.target == "GEO":
        r_a_m = GEO_RADIUS_M
    elif request.target in ("TLI", "TMI", "escape"):
        r_a_m = None
    else:
        assert request.r_a_km is not None
        r_a_m = request.r_a_km * 1000.0

    hohmann_out: HohmannOut | None = None
    circularization: float | None = None
    plane: float | None = None
    composite_out: CompositeOut | None = None
    injection_out: InjectionOut | None = None
    r_a_km_out: float | None = None

    if r_a_m is not None:
        transfer = hohmann_transfer_km_s(r_p_m, r_a_m)
        assumptions.append(transfer.assumption)
        hohmann_out = HohmannOut(
            dv1_km_s=transfer.dv1_km_s,
            dv2_km_s=transfer.dv2_km_s,
            total_km_s=transfer.total_km_s,
            a_t_km=transfer.semi_major_m / 1000.0,
        )
        circularization = circularization_km_s(r_p_m, r_a_m, at_apogee=True)
        v_circ_target = circular_velocity_km_s(r_a_m)
        if inclination is not None:
            plane = plane_change_km_s(v_circ_target, inclination)
            composite = apogee_composite_km_s(r_p_m, r_a_m, inclination)
            assumptions.append(composite.assumption)
            composite_out = CompositeOut(
                vector_km_s=composite.vector_km_s,
                circularization_km_s=composite.circularization_km_s,
                plane_change_km_s=composite.plane_change_km_s,
                scalar_sum_km_s=composite.scalar_sum_km_s,
                vector_minus_scalar_km_s=composite.vector_km_s - composite.scalar_sum_km_s,
                inclination_deg=inclination,
            )
        r_a_km_out = r_a_m / 1000.0
    else:
        injection_target = request.target
        # 前置分支已把 GEO 分流到半径路径：此处必为 TLI/TMI/escape
        assert injection_target is not None and injection_target != "GEO"
        injection = parking_injection_km_s(
            injection_target,
            r_p_m,
            c3_km2_s2=request.c3_km2_s2,
            window_assumption=request.window_assumption,
        )
        assumptions.append(injection.assumption)
        injection_out = InjectionOut(
            dv_km_s=injection.dv_km_s,
            c3_km2_s2=injection.c3_km2_s2,
            window_assumption=injection.window_assumption,
        )
        if injection_target == "TMI" and injection.window_assumption is None:  # pragma: no cover
            raise PerfError(
                "TMI 输出缺窗口/相位假设（§8.10 约束 4：不可复现）",
                suggestion="传 window_assumption 或使用缺省典型窗口文案",
            )

    return OrbitTransferResponse(
        r_p_km=request.r_p_km,
        r_a_km=r_a_km_out,
        mu_m3_s2=MU_EARTH_M3_S2,
        parking_velocity_km_s=circular_velocity_km_s(r_p_m),
        hohmann=hohmann_out,
        circularization_km_s=circularization,
        plane_change_km_s=plane,
        composite=composite_out,
        injection=injection_out,
        assumptions=tuple(assumptions),
    )


__all__ = [
    "OrbitTransferRequest",
    "OrbitTransferResponse",
    "router",
]
