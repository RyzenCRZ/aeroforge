"""单位系统（规格 §6.4 / OI-07 / 红线 §1.4-3）。

本文件钉三件事：
1. **往返恒等**（M2 验收项之一）；
2. **换算方向**固定为 ``SI = 显示值 × factor``——方向写反是本表最容易犯、且后果最隐蔽的错
   （1 kN 与 1000 kN 都能算出一个"数"）；
3. SI 字段必须把 ``unit`` / ``display_unit`` 随契约下发，否则前端只能自建换算表，
   §6.4「换算只在 API 边界发生」随即失效。

⚠ 与规格 §6.4 表格的**逐行**对照在 ``test_params_spec_contract.py``：那个文件管
"表与代码是否同步"，本文件管"换算本身是否正确"。
"""

from __future__ import annotations

from typing import get_args

import pytest

from aeroforge.params.schema import Stage, Vehicle
from aeroforge.params.units import DISPLAY_UNITS, Quantity, to_display, to_si

#: 量 → （1 显示单位 = 多少 SI）。刻意**手写**而不是从 DISPLAY_UNITS 读：
#: 若从表里读，表里的 factor 写错时本用例会跟着一起错（同义反复）。
_ONE_DISPLAY_IN_SI: dict[Quantity, float] = {
    "force": 1_000.0,
    "mass": 1_000.0,
    "length": 1.0,
    "isp": 1.0,
    "velocity": 1_000.0,
    "pressure": 100_000.0,
    "temperature": 1.0,
    "density": 1.0,
}


def test_display_units_cover_every_declared_quantity() -> None:
    assert set(DISPLAY_UNITS) == set(get_args(Quantity))


@pytest.mark.parametrize("quantity", sorted(DISPLAY_UNITS), ids=str)
def test_conversion_direction_is_si_equals_display_times_factor(quantity: Quantity) -> None:
    """1 个显示单位必须恰好等于手写期望的 SI 值——方向与量级一并钉住。"""
    expected = _ONE_DISPLAY_IN_SI[quantity]

    assert to_si(quantity, 1.0) == pytest.approx(expected)
    assert to_display(quantity, expected) == pytest.approx(1.0)


@pytest.mark.parametrize("quantity", sorted(DISPLAY_UNITS), ids=str)
def test_round_trip_is_identity(quantity: Quantity) -> None:
    """单位往返恒等（M2 验收项）：``to_display(to_si(x)) == x`` 且反向亦然。"""
    for value in (0.0, 1.0, 2.5, 1234.5, -17.25):
        assert to_display(quantity, to_si(quantity, value)) == pytest.approx(value, rel=1e-12)
        assert to_si(quantity, to_display(quantity, value)) == pytest.approx(value, rel=1e-12)


def test_factor_matches_the_display_and_si_symbols() -> None:
    """factor 的量级必须与符号自洽：符号是纯换算单位（长度 / 比冲 / 温度 / 密度）时 factor = 1。

    符号不一致的换算（例如把 bar 写成 kPa 却留 1e5）会让界面上显示的数字**看起来合理**，
    这正是"没报错 ≠ 正确"的典型形态。
    """
    for quantity, unit in DISPLAY_UNITS.items():
        if unit.symbol == unit.si_symbol:
            assert unit.factor == pytest.approx(1.0), f"{quantity} 的显示单位与 SI 同名却带换算系数"


def test_si_field_publishes_both_symbols() -> None:
    """字段元数据必须同时给出 SI 符号与显示符号（前端只做格式化，不做换算）。"""
    engine = Vehicle.model_json_schema()["$defs"]["Engine"]["properties"]

    # 推力与压力刻意各取一种**非同名**换算，覆盖 factor ≠ 1 的形态
    assert engine["thrust_sea_level_n"]["unit"] == "N"
    assert engine["thrust_sea_level_n"]["display_unit"] == "kN"
    assert engine["chamber_pressure_pa"]["unit"] == "Pa"
    assert engine["chamber_pressure_pa"]["display_unit"] == "bar"
    # 比冲的显示单位与 SI 同名，此时两者必须都下发（前端仍不该自己判"要不要换算"）
    assert engine["isp_vacuum_s"]["unit"] == "s"
    assert engine["isp_vacuum_s"]["display_unit"] == "s"


def test_si_field_defaults_decide_requiredness() -> None:
    """``si_field`` 不传 default 即必填、传 ``default=None`` 即可省略（实测缺陷的防回归）。

    pydantic 对 ``float | None = Field(...)`` 判为**必填**——``| None`` 只说明值可为 null。
    此处从契约侧再钉一次：可省略的字段不得出现在 ``required`` 里。
    """
    required = set(Stage.model_json_schema().get("required", []))

    assert "diameter_m" in required
    assert "max_fill_mass_kg" not in required
    assert "engine_height_nozzle_excluded_m" not in required
