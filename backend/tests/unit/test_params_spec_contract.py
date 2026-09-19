"""规格 ↔ 代码机检：单位表（§6.4）与诊断阈值表（§6.5）必须逐行同步。

由来（"表改了代码没跟"是本项目已经踩过的形态）
------------------------------------------------
§6.4 的「显示单位」表与 §6.5 的阈值表都以**规格**为唯一真理源。若有人只改文档、
或只改代码，两边会在无声中分叉：界面显示 bar、后端按 kPa 出数，或阈值悄悄从 20% 变 25%
而 UX 文案仍写 20%。故本文件不测行为，只做**逐行对照**——同 ``test_perf_budget`` 的口径。

⚠ 解析器自带自检（``test_parsers_find_the_expected_rows``）：规格排版一变（标题改名、
表格拆行），解析可能**静默返回空集**，那样本文件的全部断言都会"通过"——
这正是本项目最贵的一课（"没报错 ≠ 正确"），故空集必须先被挡住。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aeroforge.params.diagnostics import (
    DiagnosticThresholds,
    run_diagnostics,
)
from aeroforge.params.schema import Vehicle
from aeroforge.params.units import DISPLAY_UNITS
from aeroforge.paths import config_file

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_MD = REPO_ROOT / "AeroForge-Spec.md"

#: §6.4 / §6.5 的标题原文。改名即失败——这是"机检锚点"，不是随手取的字符串。
UNITS_HEADING = "### 6.4 单位系统"
DIAGNOSTICS_HEADING = "### 6.5 方案诊断与改进建议"

#: §6.5 表的行数（规则数）。与规格表逐行对应，增删规则必须同步本文件。
_SPEC_RULE_COUNT = 7
#: §6.4 表的行数（量数）。
_SPEC_QUANTITY_COUNT = 8

#: 规格 §6.5 表的「级别」列 → 本工程的 Level 取值。
_LEVEL_BY_SPEC_WORD = {"硬": "hard", "警": "warning"}

#: 阈值字段 → §6.5 表「规则」列原文。字段的**默认值**必须出现在该行的「判据」列里：
#: 百分量按 ``{:.0%}`` 写、其余按 ``{:g}`` 写（把关的是
#: ``test_threshold_default_appears_in_the_spec_row``）。
_THRESHOLD_ROWS: dict[str, str] = {
    "twr_liquid_min": "起飞推重比",
    "twr_solid_min": "起飞推重比",
    "structure_mass_ratio_min": "结构质量比",
    "structure_mass_ratio_max": "结构质量比",
    "length_to_diameter_min": "长径比",
    "length_to_diameter_max": "长径比",
    "min_tank_wall_thickness_m": "贮箱壁厚",
    "dv_allocation_deviation_max": "级间 ΔV 分配",
    "upper_stage_margin_min": "上面级余量",
    "upper_stage_margin_max": "上面级余量",
}

#: 以「占多少」表述的阈值（规格写 ``3%`` 而不是 ``0.03``）。
_PERCENT_FIELDS = frozenset(
    {
        "structure_mass_ratio_min",
        "structure_mass_ratio_max",
        "dv_allocation_deviation_max",
        "upper_stage_margin_min",
        "upper_stage_margin_max",
    }
)

#: 规格**没给数值**的阈值：此时默认值必须是 ``None``（不得凭空填一个下界，§1.4-4）。
_NO_SPEC_VALUE = frozenset({"min_tank_wall_thickness_m"})

#: §6.5 表里没有、但由 §6.3 相容约束举例而来的规则（本工程刻意补进规则集并留痕）。
_NOZZLE_TITLE = "喷管出口直径 < 级直径"


def _section_lines(heading: str) -> list[str]:
    """取规格中某标题下的正文行（到下一个同级或更高级标题为止）。"""
    lines = SPEC_MD.read_text(encoding="utf-8").splitlines()
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == heading),
        None,
    )
    assert start is not None, f"规格 {SPEC_MD} 里找不到标题 {heading!r}——标题被改名了？"
    level = len(heading) - len(heading.lstrip("#"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("#") and len(stripped) - len(stripped.lstrip("#")) <= level:
            break
        body.append(line)
    return body


def _table_rows(heading: str, *, columns: int) -> list[list[str]]:
    """取该标题下的 markdown 表格行（已去掉分隔行与列数不符的行）。"""
    rows: list[list[str]] = []
    for line in _section_lines(heading):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != columns or all(set(cell) <= set("-: ") for cell in cells):
            continue
        rows.append(cells)
    return rows


def _spec_quantities() -> dict[str, tuple[str, str]]:
    """§6.4 表 → ``{量: (显示单位, 内部 SI)}``（反引号已剥掉）。"""
    table = {}
    for cells in _table_rows(UNITS_HEADING, columns=3):
        if cells[0] == "量":
            continue
        quantity, display, si = (cell.strip("`") for cell in cells)
        table[quantity] = (display, si)
    return table


def _spec_rules() -> dict[str, tuple[str, str]]:
    """§6.5 表 → ``{规则: (判据, 级别)}``（级别按本工程取值翻译）。"""
    table = {}
    for cells in _table_rows(DIAGNOSTICS_HEADING, columns=5):
        if cells[0] == "规则":
            continue
        criterion, level = cells[1], cells[2]
        assert level in _LEVEL_BY_SPEC_WORD, f"§6.5 表出现了未知级别 {level!r}"
        table[cells[0]] = (criterion, _LEVEL_BY_SPEC_WORD[level])
    return table


def _numbers(text: str) -> list[str]:
    """把判据里的数字抠出来（``∈ [5, 30]`` → ``['5', '30']``）。

    用**完整匹配**而不是子串包含：``3`` 会被 ``30`` 吞掉，那样的守卫等于没有。
    """
    return re.findall(r"\d+(?:\.\d+)?%?", text)


def test_parsers_find_the_expected_rows() -> None:
    """解析器自检：空集或数字变了必须在此处响亮失败，而不是让后面的对照"全部通过"。"""
    assert SPEC_MD.is_file(), f"规格文件不存在：{SPEC_MD}"
    assert len(_spec_quantities()) == _SPEC_QUANTITY_COUNT
    assert len(_spec_rules()) == _SPEC_RULE_COUNT
    assert "### 6.5" not in "\n".join(_section_lines(UNITS_HEADING)), "§6.4 解析越界到了下一节"


def test_display_units_match_section_6_4_row_by_row() -> None:
    """§6.4 表与 :data:`DISPLAY_UNITS` 必须逐行一致（含符号写法，如 ``km·s⁻¹``）。"""
    spec = _spec_quantities()
    code = {unit.label: (unit.symbol, unit.si_symbol) for unit in DISPLAY_UNITS.values()}

    assert code == spec, (
        "§6.4 表与 aeroforge/params/units.py 的 DISPLAY_UNITS 不一致："
        f"仅在代码里 {sorted(set(code) - set(spec))}，仅在规格里 {sorted(set(spec) - set(code))}"
    )


def test_every_threshold_is_registered_against_a_spec_row() -> None:
    """新增阈值不登记规格行即失败——否则阈值可以在文档之外悄悄增加。"""
    assert set(_THRESHOLD_ROWS) == set(DiagnosticThresholds.model_fields)
    spec = _spec_rules()
    unknown = sorted(set(_THRESHOLD_ROWS.values()) - set(spec))
    assert not unknown, f"这些阈值挂在了规格不存在的规则上：{unknown}"


@pytest.mark.parametrize("field", sorted(_THRESHOLD_ROWS), ids=str)
def test_threshold_default_appears_in_the_spec_row(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阈值的**默认值**必须逐字出现在 §6.5 表对应行的判据里（OI-08：默认值即下表数值）。

    参数化到字段粒度：某一行的数字改了而代码没跟时，失败信息直接点名是哪个字段。
    """
    for name in DiagnosticThresholds.model_fields:
        monkeypatch.delenv(f"AEROFORGE_{name.upper()}", raising=False)
    assert not config_file().is_file(), (
        f"检出 {config_file()}：本用例必须读**默认值**，环境里存在配置文件会读到覆盖值"
    )

    value = getattr(DiagnosticThresholds(), field)
    criterion = _spec_rules()[_THRESHOLD_ROWS[field]][0]

    if field in _NO_SPEC_VALUE:
        assert value is None, f"{field} 在 §6.5 表里没有数值，默认值必须是 None"
        assert "最小工艺厚度" in criterion, f"§6.5 表的贮箱壁厚判据变了：{criterion}"
        return

    token = f"{value:.0%}" if field in _PERCENT_FIELDS else f"{value:g}"
    assert token in _numbers(criterion), (
        f"{field} 的默认值 {value}（规格里应写作 {token}）未出现在 §6.5 表的判据 {criterion!r} 中"
    )


def test_rule_titles_and_levels_match_the_spec_table(single_stage_vehicle: Vehicle) -> None:
    """规则集的标题与级别必须与 §6.5 表一致（标题是前端分组渲染的键，不能各写一套）。"""
    spec = _spec_rules()
    report = run_diagnostics(single_stage_vehicle)
    code = {rule.title: rule.level for rule in report.rules}

    assert set(code) - set(spec) == {_NOZZLE_TITLE}, "出现了规格表之外的规则名"
    assert set(spec) - set(code) == set(), f"§6.5 的规则没落进实现：{sorted(set(spec) - set(code))}"
    for title, (_criterion, level) in spec.items():
        assert code[title] == level, f"{title} 的级别与 §6.5 表不一致"


def test_section_6_5_still_defers_impact_to_m4() -> None:
    """§6.5 的 OI-11 归属是本轮"不交付 impact"的**依据**：该段被改写即失败。

    代码侧由 ``test_diagnostics_are_six_fields_in_json`` 与
    ``test_diagnostic_shape_is_six_fields`` 把守；这里守的是同一决定的文档侧。
    """
    body = "\n".join(_section_lines(DIAGNOSTICS_HEADING))

    assert "六字段" in body, "§6.5 不再声明 M2 输出六字段"
    assert "编造" in body, "§6.5 不再禁止为凑验收而编造 impact"
