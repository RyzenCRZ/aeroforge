"""最优 O/F 工作点求解（规格 §8.2 工作点表）。

CEA 表不只正查，还**反求工作点**。关键工程点（§8.2 末段）：一级与上面级的优化
目标**不同**——上面级追求比冲最大，一级/助推追求**密度比冲最大**（一级瓶颈常是
体积与阻力，而非质量）。平台按级次采用不同目标函数，并允许用户显式覆盖。

- 平均推进剂密度：``ρ_avg = (1+O/F) / (1/ρ_ox + O/F/ρ_fuel)``，ρ 取
  ``aeroforge.params.propellants``（§5.9 同源，常温标称值、工程惯例）；
- 密度比冲：``Isp(s) × ρ_avg``；
- 一维搜索：``scipy.optimize.minimize_scalar``（有界），在表域内进行、不外推。
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field
from scipy.optimize import minimize_scalar

from aeroforge.errors import AeroForgeError
from aeroforge.params.propellants import PropellantCombination, properties
from aeroforge.perf import G0
from aeroforge.perf.cea_table import CeaTable, load_table

#: 求解目标。``isp`` = 真空比冲最大（上面级）；``density_impulse`` = 密度比冲最大（一级/助推）。
WorkpointGoal = Literal["isp", "density_impulse"]

#: 可用混合比区间的默认 Tc 上限（K）——避开烧蚀敏感的高 Tc 峰值邻域。
#: **工程惯例值，非权威**（§1.4-4）：不同推力室材料/冷却方案差异极大，可显式覆盖。
TC_MAX_DEFAULT_K = 3400.0

#: 可用混合比区间的默认 c* 允许跌落（相对峰值）——偏离 c* 峰 5% 以内视为"稳定燃烧可用区"。
#: **工程惯例值，非权威**；§8.2 工作点表"可用混合比区间"行的量化口径。
CSTAR_DROP_DEFAULT = 0.05

#: 表键 → ``params/propellants`` 物性枚举的映射（密度取唯一权威来源，不得另建第二份物性表）。
_DENSITY_BY_TABLE_KEY: dict[str, PropellantCombination] = {
    "lox_rp1": "LOX/RP-1",
    "lox_lh2": "LOX/LH2",
    "lox_ch4": "LOX/CH4",
    "n2o4_mmh": "N2O4/MMH",
}


class WorkPoint(BaseModel):
    """工作点求解结果（真空口径）。"""

    propellant: str
    goal: WorkpointGoal
    pc_bar: float = Field(description="室压（bar）")
    eps: float = Field(description="喷管面积比 ε")
    of_optimal: float = Field(description="最优混合比 O/F（表域内一维搜索的极值点）")
    at_domain_boundary: bool = Field(
        default=False,
        description=(
            "极值是否落在表 O/F 域边界上。LH2 等低密度燃料的密度比冲在富氧侧"
            "单调上升（ρ_avg 随 O/F 增大趋近 ρ_ox），表域内极值即边界——此时该"
            "度量更适合**组合间对比**，真实一级混合比由 ΔV-贮箱质量耦合决定"
            "（如 LOX/LH2 一级 RS-68 ≈6.0，高于其 Isp 峰 ≈4.8）"
        ),
    )
    isp_vacuum_m_s: float = Field(description="该工作点真空比冲（m/s，ADR-014 契约①）")
    isp_s: float = Field(description="该工作点真空比冲（s；= m/s ÷ G0）")
    rho_avg_kg_m3: float | None = Field(
        default=None, description="平均推进剂密度（kg/m³）；仅 goal=density_impulse 时给出"
    )
    density_impulse: float | None = Field(
        default=None,
        description="密度比冲 = Isp(s)·ρ_avg（kg·s/m³）；仅 goal=density_impulse 时给出",
    )


def average_density(of: float, density_ox_kg_m3: float, density_fuel_kg_m3: float) -> float:
    """平均推进剂密度（kg/m³，体积平均口径）。

    ``ρ_avg = (1+r) / (r/ρ_ox + 1/ρ_f)``，r = O/F（质量比，氧化剂/燃料）——
    Sutton《Rocket Propulsion Elements》体积平均密度标准式（由质量分数
    ``w_ox = r/(1+r)``、``w_f = 1/(1+r)`` 推得）。量级自检：LOX/LH2 @r=5.5
    ≈ 344 kg/m³、LOX/RP-1 @r=2.56 ≈ 1023 kg/m³。
    """
    return (1.0 + of) / (of / density_ox_kg_m3 + 1.0 / density_fuel_kg_m3)


def optimal_of(propellant: str, pc_bar: float, eps: float, goal: WorkpointGoal) -> WorkPoint:
    """在表域内一维搜索最优 O/F（§8.2 工作点表第 1、2 行）。

    - ``goal="isp"``：最大化真空 Isp —— 上面级；
    - ``goal="density_impulse"``：最大化 ``Isp(s) × ρ_avg`` —— 一级/助推。
      ρ 必须来自 ``params/propellants``；该表未收录的组合（本片为 ``n2o4_mmh``）
      显式报错，不臆造物性。
    """
    table = load_table(propellant)
    low, high = float(table.of_grid[0]), float(table.of_grid[-1])

    if goal == "isp":

        def objective(of: float) -> float:
            return -table.lookup(of, pc_bar, eps, 0.0).isp_vacuum_m_s

        combo: PropellantCombination | None = None
    elif goal == "density_impulse":
        combo = _DENSITY_BY_TABLE_KEY.get(propellant)
        if combo is None:
            msg = (
                f"推进剂 {propellant!r} 在 params/propellants 中没有物性条目，"
                "无法计算密度比冲（不得在 perf 层臆造物性）"
            )
            raise AeroForgeError(
                msg,
                suggestion=(
                    "改用 goal='isp'（上面级口径），或在 params/propellants 收录该组合的"
                    "标称密度后再用 goal='density_impulse'"
                ),
            )
        props = properties(combo)

        def objective(of: float) -> float:
            isp = table.lookup(of, pc_bar, eps, 0.0).isp_vacuum_m_s
            rho = average_density(of, props.density_ox_kg_m3, props.density_fuel_kg_m3)
            return -(isp / G0) * rho
    else:
        msg = f"未知工作点目标 {goal!r}；可选：'isp' | 'density_impulse'"
        raise AeroForgeError(msg, suggestion="按 §8.2 工作点表选择目标函数")

    result = minimize_scalar(objective, bounds=(low, high), method="bounded")
    of_opt = float(result.x)
    point = table.lookup(of_opt, pc_bar, eps, 0.0)
    isp_s = point.isp_vacuum_m_s / G0
    span = high - low
    at_boundary = (high - of_opt) <= 1e-6 * span or (of_opt - low) <= 1e-6 * span

    rho_avg: float | None = None
    density_impulse: float | None = None
    if combo is not None:
        props = properties(combo)
        rho_avg = average_density(of_opt, props.density_ox_kg_m3, props.density_fuel_kg_m3)
        density_impulse = isp_s * rho_avg

    return WorkPoint(
        propellant=propellant,
        goal=goal,
        pc_bar=pc_bar,
        eps=eps,
        of_optimal=of_opt,
        at_domain_boundary=at_boundary,
        isp_vacuum_m_s=point.isp_vacuum_m_s,
        isp_s=isp_s,
        rho_avg_kg_m3=rho_avg,
        density_impulse=density_impulse,
    )


def usable_of_range(
    propellant: str,
    pc_bar: float,
    eps: float,
    *,
    tc_max_k: float = TC_MAX_DEFAULT_K,
    cstar_drop: float = CSTAR_DROP_DEFAULT,
) -> tuple[float, float] | None:
    """可用 O/F 区间（§8.2 工作点表第 4 行）：Tc ≤ 上限 且 c* 相对峰值跌落 ≤ 阈值。

    阈值默认值均为**工程惯例、非权威**（见模块常量注释）；返回连续可行区间的
    ``(of_low, of_high)``，整个 O/F 网格都不可行时返回 ``None``。
    """
    table: CeaTable = load_table(propellant)
    of_grid = table.of_grid
    points = [table.lookup(float(x), pc_bar, eps, 0.0) for x in of_grid]
    cstar = np.array([p.c_star_m_s for p in points])
    tc = np.array([p.t_chamber_k for p in points])
    feasible = (tc <= tc_max_k) & (cstar >= (1.0 - cstar_drop) * cstar.max())
    indices = np.flatnonzero(feasible)
    if indices.size == 0:
        return None
    return float(of_grid[indices[0]]), float(of_grid[indices[-1]])


def mass_flow(thrust_n: float, isp_s: float) -> float:
    """给定推力求流量（§8.2 工作点表第 3 行）：``ṁ = F / (Isp · g₀)``。

    ``isp_s`` 单位为**秒**（若手头是 CEA 表的 m/s 值，先除以 :data:`aeroforge.perf.G0`）。
    """
    return thrust_n / (isp_s * G0)
