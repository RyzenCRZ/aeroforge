"""效率因子（规格 §8.3）。

``Isp_actual = η_c* × η_Cf × Isp_ideal``；本模块的 η 语义即 §8.3 表中的
**组合效率**（η_c* 与 η_Cf 的乘积），不再拆分两个因子——拆分需要逐因子的实测
数据，当前只有组合口径的工程惯例区间。

- 表值为各循环的**区间中值**，属**工程惯例、非权威**（§1.4-4 溯源红线）；
- 默认值记录在 provenance 中（§8.3 要求）；
- ``Engine.efficiency_factor`` 在 §6.1 Schema 中是**必填字段**（无缺省路径），
  因此"显式 η 优先、缺省按循环中值"中的缺省路径只服务于尚无 Engine 实例的
  估算调用方（:func:`resolve_efficiency`）；
- §8.3 表没有 ``pressure_fed`` 行——该循环不臆造中值，要求调用方显式给 η。
"""

from __future__ import annotations

from aeroforge.errors import AeroForgeError

#: 循环方式 → 组合效率典型值（§8.3 表区间中值，工程惯例、非权威）。
#: 注意键含 ``solid``（推进剂相态口径）而不含 ``pressure_fed``——后者 §8.3 未给中值。
CYCLE_EFFICIENCY: dict[str, float] = {
    "gas_generator": 0.93,  # η_c* 0.94–0.97 × η_Cf 0.96–0.98
    "staged_combustion": 0.96,  # η_c* 0.96–0.99 × η_Cf 0.97–0.99
    "expander": 0.96,  # η_c* 0.96–0.98 × η_Cf 0.97–0.99
    "solid": 0.90,  # η_c* 0.92–0.95 × η_Cf 0.94–0.97
}


def isp_actual(isp_ideal: float, efficiency_factor: float) -> float:
    """理想比冲 × 组合效率（§8.3）。

    ``isp_ideal`` 可为 m/s（CEA 表口径）或 s——η 是无量纲乘子，两种口径各自成立；
    调用方须保证 ``efficiency_factor`` 与 ``isp_ideal`` 对应同一组合效率语义。
    """
    return isp_ideal * efficiency_factor


def resolve_efficiency(efficiency_factor: float | None = None, cycle: str | None = None) -> float:
    """η 取值口径：**显式值优先，缺省按循环方式中值**。

    - ``efficiency_factor`` 非 None → 原样返回（Engine.efficiency_factor 显式参数，
      可调参并纳入不确定度采样，§8.3）；
    - 否则按 ``cycle`` 查 :data:`CYCLE_EFFICIENCY`（区间中值）；
    - ``pressure_fed`` 等表中未收录的循环 → 显式报错要求给出 η，不臆造数值。
    """
    if efficiency_factor is not None:
        return float(efficiency_factor)
    if cycle is None:
        msg = "未提供效率因子：既无显式 efficiency_factor，也无 cycle 可查中值"
        raise AeroForgeError(
            msg,
            suggestion="显式传入 efficiency_factor（§8.3：显式参数优先），或给出循环方式",
        )
    value = CYCLE_EFFICIENCY.get(cycle)
    if value is None:
        msg = (
            f"循环方式 {cycle!r} 在 §8.3 效率表中没有中值行"
            f"（已收录：{', '.join(sorted(CYCLE_EFFICIENCY))}）"
        )
        raise AeroForgeError(
            msg,
            suggestion=(
                "§8.3 未给该循环的组合效率区间（如 pressure_fed）；"
                "请显式传入 efficiency_factor，不使用臆造的中值"
            ),
        )
    return value
