"""§6.5 方案诊断规则集（含"未判定"的账目与阈值来源）。

本文件盯三件最容易做错的事：

1. **未判定必须与已验证同等可见**：``deferred_reason`` / ``uncovered`` 不得被省略，
   更不得把判不了的规则静默跳过——"空集通过"与"已验证"长得一模一样，
   而这正是本项目最贵的一课（"没报错 ≠ 正确"）。
2. **六字段**：``impact`` 按 OI-11 归 M4，M2 不得为凑验收而编造百分比。
3. **阈值可配置且带来源标注**：默认值即 §6.5 表内数值，可被环境变量 / ``config.toml`` 覆盖
   （§18.4），且每条阈值的说明都必须携带「工程惯例值，非权威来源」的标注。
"""

from __future__ import annotations

from typing import Any

import pytest

from aeroforge.params import diagnostics as diag
from aeroforge.params.diagnostics import (
    CODE_CAPACITY_GAP,
    CODE_DV_ALLOCATION,
    CODE_LENGTH_TO_DIAMETER,
    CODE_NOZZLE_DIAMETER,
    CODE_STRUCTURE_MASS_RATIO,
    CODE_TANK_WALL,
    CODE_TWR,
    CODE_UPPER_STAGE_MARGIN,
    SOURCE_CONVENTION,
    SOURCE_PROCESS_LIMIT,
    DiagnosticThresholds,
    RuleOutcome,
    run_diagnostics,
)
from aeroforge.params.schema import Vehicle
from aeroforge.paths import config_file
from tests.conftest import _legal_vehicle, make_stage

#: 让起飞推重比落在下界之上的推进剂质量（一级）。
_LIGHT_LOAD_KG = 450_000.0
#: 让起飞推重比落到 1.2 之下的推进剂质量（一级）。
_HEAVY_LOAD_KG = 700_000.0
#: 让起飞推重比落在**两档之间**（≈1.36）的推进剂质量：液体档通过、固体档不通过。
#: 两档的差异只有在这么一段载荷上才看得见——这正是 OI-30 要判出来的东西。
_MID_LOAD_KG = 520_000.0


def _rule(report: diag.DiagnosticsReport, code: str) -> RuleOutcome:
    matched = [rule for rule in report.rules if rule.code == code]
    assert len(matched) == 1, f"判定码 {code} 应恰好出现一次，实际 {len(matched)} 次"
    return matched[0]


# ---------------------------------------------------------------------------
# 账目完整性
# ---------------------------------------------------------------------------


def test_every_declared_code_is_accounted_for(single_stage_vehicle: Vehicle) -> None:
    """规则集必须把声明过的判定码**逐条**记账——少一条就是"悄悄不判"。"""
    declared = {name: value for name, value in vars(diag).items() if name.startswith("CODE_")}

    report = run_diagnostics(single_stage_vehicle)

    assert {rule.code for rule in report.rules} == set(declared.values())
    assert len(report.rules) == len(declared), "判定码重复会让前端渲染出两条同一缺陷"


def test_m4_dependent_rules_are_deferred_and_listed(single_stage_vehicle: Vehicle) -> None:
    """M2 判不了的规则必须进 ``deferred_rule_codes`` 并写明原因，且不得带裁定。

    ⚠ 判不了的成因有两类，**不得混为一谈**：① 能力缺口（§6.5 表的这几条要 M4 的
    Δv / 运力 / 喷管型面结果）；② 材料未知且未配置工艺下限（见壁厚专属用例）。
    本用例把①钉死，并要求**其余规则确实判过**——否则"全部 deferred"也能让集合断言通过。

    口径更新（§7.4 QA-3，规格演进非放宽）：夹具材料 al-2219 在库，贮箱壁厚规则
    经**材料库典型值**真判定，不再因「未配置」进 deferred。
    """
    report = run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _LIGHT_LOAD_KG})

    m4_gap = {
        CODE_DV_ALLOCATION,
        CODE_UPPER_STAGE_MARGIN,
        CODE_CAPACITY_GAP,
        CODE_NOZZLE_DIAMETER,
    }
    deferred = set(report.deferred_rule_codes)

    assert deferred == m4_gap
    assert {rule.code for rule in report.rules if not rule.deferred} == {
        CODE_TWR,
        CODE_STRUCTURE_MASS_RATIO,
        CODE_LENGTH_TO_DIAMETER,
        CODE_TANK_WALL,
    }
    for rule in report.rules:
        assert rule.threshold.strip(), f"{rule.code} 没给出本次生效的阈值"
        assert rule.source.strip(), f"{rule.code} 没标注阈值来源"
        if rule.deferred:
            assert rule.deferred_reason and rule.deferred_reason.strip()
            assert rule.diagnostics == (), f"{rule.code} 既判未判定又给裁定，自相矛盾"
        else:
            assert rule.deferred_reason is None


def test_twr_joins_the_deferred_set_without_m4_mass(single_stage_vehicle: Vehicle) -> None:
    """缺 M4 的定尺结果时推重比算不出来——进 deferred，**不填默认值**。"""
    report = run_diagnostics(single_stage_vehicle)

    rule = _rule(report, CODE_TWR)
    assert rule.deferred
    assert "M4" in (rule.deferred_reason or "")
    assert CODE_TWR in report.deferred_rule_codes


def test_diagnostics_are_six_fields_in_json(single_stage_vehicle: Vehicle) -> None:
    """``impact`` 归 M4（OI-11）：M2 的裁定只能是六字段。"""
    report = run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _HEAVY_LOAD_KG})

    assert report.diagnostics, "用例本身没造出裁定"
    for item in report.diagnostics:
        payload = item.model_dump(mode="json")
        assert set(payload) == {"level", "code", "field_path", "message", "suggestion"}
        assert "impact" not in payload
        assert payload["suggestion"].strip()


def test_hard_failures_only_expose_hard_items(single_stage_vehicle: Vehicle) -> None:
    report = run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _HEAVY_LOAD_KG})

    assert report.hard_failures
    assert {item.level for item in report.hard_failures} == {"hard"}


# ---------------------------------------------------------------------------
# 起飞推重比（硬）
# ---------------------------------------------------------------------------


def test_twr_fires_below_the_threshold(single_stage_vehicle: Vehicle) -> None:
    report = run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _HEAVY_LOAD_KG})

    rule = _rule(report, CODE_TWR)
    assert not rule.deferred
    assert len(rule.diagnostics) == 1
    item = rule.diagnostics[0]
    assert item.level == "hard"
    assert item.code == CODE_TWR
    assert item.field_path == "vehicle"
    assert "低于下界 1.2" in item.message
    # 裁定本身携带阈值的来源声明（§6.5：不得以"规格规定"的语气呈现为硬性真理）
    assert "工程惯例值" in item.suggestion
    assert "非权威来源" in item.suggestion
    # 规则账目上则是完整标注（UI 可原样展示）
    assert rule.source == SOURCE_CONVENTION


def test_twr_passes_but_still_reports_the_uncovered_branch(single_stage_vehicle: Vehicle) -> None:
    """通过也要留痕：档位只按**第一级**相态取（§1.7.3 OI-30），其余级不参与本规则。

    §6.5 表只按「是否固体助推」给两档，未定义混合构型（液体芯级 + 固体助推分属不同级）
    的加权判据——这条分支**确实**未覆盖，故不得因为"档位判出来了"就省掉留痕。
    """
    report = run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _LIGHT_LOAD_KG})

    rule = _rule(report, CODE_TWR)
    assert rule.diagnostics == ()
    assert rule.uncovered, "通过且无留痕 = 把「未覆盖的分支」伪装成已验证"
    assert any("第一级" in note for note in rule.uncovered)


def _with_phase(vehicle: Vehicle, phase: str) -> Vehicle:
    """把**第一级**发动机的相态换掉（§1.7.3 OI-30 的判据所在）。"""
    first = vehicle.stages[0]
    engine = first.engine.model_copy(update={"propellant_phase": phase})
    stages = (first.model_copy(update={"engine": engine}), *vehicle.stages[1:])
    return vehicle.model_copy(update={"stages": stages})


def test_twr_uses_the_solid_tier_for_a_solid_first_stage(single_stage_vehicle: Vehicle) -> None:
    """固体第一级走 1.5 档：**同一个 T/W** 液体档通过、固体档被拒（§1.7.3 OI-30）。

    这是"固体档判不了"的正面反证——若档位仍写死 1.2，本用例的固体档会假通过
    （而那正是 M2 留白第 3 项的原形态）。
    """
    solid = _with_phase(single_stage_vehicle, "solid")

    liquid_rule = _rule(
        run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _MID_LOAD_KG}), CODE_TWR
    )
    solid_rule = _rule(run_diagnostics(solid, propellant_mass_kg={1: _MID_LOAD_KG}), CODE_TWR)

    assert liquid_rule.diagnostics == (), "本例的 T/W 应高于液体档下界 1.2"
    assert len(solid_rule.diagnostics) == 1, "同一个 T/W 应低于固体档下界 1.5"
    assert "低于下界 1.5（固体助推档）" in solid_rule.diagnostics[0].message
    assert solid_rule.diagnostics[0].level == "hard"

    # 档位与依据必须随报告下发：档位选择不得隐式，否则"用了哪一档"无从复核
    assert "固体助推" in solid_rule.threshold
    assert "propellant_phase = 'solid'" in solid_rule.threshold
    assert "液体档" in liquid_rule.threshold
    assert "propellant_phase = 'liquid'" in liquid_rule.threshold


@pytest.mark.parametrize("phase", ["liquid", "hybrid"])
def test_twr_hybrid_and_liquid_share_the_liquid_tier(
    single_stage_vehicle: Vehicle, phase: str
) -> None:
    """规格 §6.5 只给两档：``hybrid`` 与 ``liquid`` 同走 1.2 档（§1.7.3 OI-30）。

    与上一条互为对照：同样的载荷，``solid`` 被拒而 ``hybrid`` 通过——档位确实由相态决定，
    而不是"凡非液体即固体"这类想当然的映射。
    """
    vehicle = _with_phase(single_stage_vehicle, phase)

    rule = _rule(run_diagnostics(vehicle, propellant_mass_kg={1: _MID_LOAD_KG}), CODE_TWR)

    assert rule.diagnostics == ()
    assert f"propellant_phase = {phase!r}" in rule.threshold


def test_twr_solid_tier_is_also_recorded_when_the_rule_is_deferred(
    single_stage_vehicle: Vehicle,
) -> None:
    """缺 M4 的定尺结果时规则整体 deferred，但**档位仍须先算出来并记下**。

    否则一旦 M4 补上载荷，账目里的档位会"突然出现"，那时再回头核对就晚了。
    """
    solid = _with_phase(single_stage_vehicle, "solid")

    rule = _rule(run_diagnostics(solid), CODE_TWR)

    assert rule.deferred
    assert "1.5" in rule.threshold
    assert "固体助推" in rule.threshold
    assert rule.diagnostics == ()


# ---------------------------------------------------------------------------
# 结构质量比（警）
# ---------------------------------------------------------------------------


def test_structure_mass_ratio_passes_in_range(single_stage_vehicle: Vehicle) -> None:
    report = run_diagnostics(single_stage_vehicle)

    rule = _rule(report, CODE_STRUCTURE_MASS_RATIO)
    assert rule.diagnostics == ()
    assert not rule.deferred
    assert rule.uncovered, "§6.5 表只判一级，其余级未判——必须留痕"


@pytest.mark.parametrize(
    ("coefficient", "direction"),
    [(0.20, "偏高"), (0.02, "偏低")],
    ids=["偏高", "偏低"],
)
def test_structure_mass_ratio_warns_out_of_range(coefficient: float, direction: str) -> None:
    vehicle = _legal_vehicle(
        f"结构系数 {coefficient}",
        (make_stage(1, length_m=41.2, structure_coefficient=coefficient),),
    )

    report = run_diagnostics(vehicle)

    rule = _rule(report, CODE_STRUCTURE_MASS_RATIO)
    assert len(rule.diagnostics) == 1
    item = rule.diagnostics[0]
    assert item.level == "warning"
    assert item.field_path == "stages[0].structure_coefficient"
    assert direction in item.message


# ---------------------------------------------------------------------------
# 贮箱壁厚（硬，但数值须显式配置；未配置时回落材料库典型值，§7.4 QA-3）
# ---------------------------------------------------------------------------


def _with_tank_material(vehicle: Vehicle, mid: str) -> Vehicle:
    """把夹具的**两箱**材料换掉（级层与箭体层一并换，保持三层一致）。"""
    first = vehicle.stages[0]
    geometry = first.geometry
    tanks = {
        "oxidizer_tank": geometry.oxidizer_tank.model_copy(update={"material": mid}),
        "fuel_tank": geometry.fuel_tank.model_copy(update={"material": mid}),
    }
    stage = first.model_copy(
        update={"geometry": geometry.model_copy(update=tanks), "material": mid}
    )
    return vehicle.model_copy(update={"stages": (stage, *vehicle.stages[1:]), "material": mid})


def _with_tank_wall(vehicle: Vehicle, wall: float) -> Vehicle:
    """把夹具的**两箱**壁厚换掉（其余不动——负例只改一处）。"""
    first = vehicle.stages[0]
    geometry = first.geometry
    tanks = {
        "oxidizer_tank": geometry.oxidizer_tank.model_copy(update={"wall_thickness_m": wall}),
        "fuel_tank": geometry.fuel_tank.model_copy(update={"wall_thickness_m": wall}),
    }
    stage = first.model_copy(update={"geometry": geometry.model_copy(update=tanks)})
    return vehicle.model_copy(update={"stages": (stage, *vehicle.stages[1:])})


def _with_fuel_tank_material(vehicle: Vehicle, mid: str) -> Vehicle:
    """只换**燃料箱**的材料（混合材料构型：同级两箱不同料）。"""
    first = vehicle.stages[0]
    geometry = first.geometry
    tanks = {"fuel_tank": geometry.fuel_tank.model_copy(update={"material": mid})}
    stage = first.model_copy(update={"geometry": geometry.model_copy(update=tanks)})
    return vehicle.model_copy(update={"stages": (stage, *vehicle.stages[1:])})


def test_tank_wall_stays_deferred_for_an_unknown_material_without_config(
    single_stage_vehicle: Vehicle,
) -> None:
    """§7.4 口径（规格演进）：「未配置即不判定」只保留给**材料未知**的情况。

    材料不在库 ⇒ 无典型工艺下限可回落，维持 OI-31 的 deferred 留痕（不猜）。
    API 通路里未知材料先被 ``HARD_MATERIAL_UNKNOWN`` 拒为 422，到不了本分支；
    这里钉的是规则层被直调（如批量离线诊断）时的行为。
    """
    report = run_diagnostics(_with_tank_material(single_stage_vehicle, "unobtanium"))

    rule = _rule(report, CODE_TANK_WALL)
    assert rule.deferred
    assert "未配置" in (rule.deferred_reason or "")
    assert "不在材料库" in (rule.deferred_reason or "")
    assert "未配置" in rule.threshold
    assert rule.source == SOURCE_PROCESS_LIMIT


def test_tank_wall_falls_back_to_the_material_typical_minimum(
    single_stage_vehicle: Vehicle,
) -> None:
    """未配置全局下限 → 逐箱回落材料典型工艺下限（§7.4 QA-3）。

    夹具壁厚 0.005 ≥ al-2219 典型下限 0.002：真判定且通过，账目照实标注
    「材料库典型值」——留痕语义不变（OI-31：来源与生效值随结果下发）。
    """
    report = run_diagnostics(single_stage_vehicle)

    rule = _rule(report, CODE_TANK_WALL)
    assert not rule.deferred
    assert rule.diagnostics == ()
    assert "0.002" in rule.threshold
    assert "材料库典型值" in rule.threshold
    assert rule.source == "材料库典型值（材料：al-2219）"


def test_tank_wall_warns_below_the_material_typical_minimum(single_stage_vehicle: Vehicle) -> None:
    """壁厚低于材料典型下限 → **警告**（工程惯例典型值，非强制工艺极限，§7.4）。"""
    report = run_diagnostics(_with_tank_wall(single_stage_vehicle, 0.001))

    rule = _rule(report, CODE_TANK_WALL)
    assert not rule.deferred
    assert [item.field_path for item in rule.diagnostics] == [
        "stages[0].geometry.oxidizer_tank.wall_thickness_m",
        "stages[0].geometry.fuel_tank.wall_thickness_m",
    ]
    assert {item.level for item in rule.diagnostics} == {"warning"}
    assert "al-2219" in rule.source
    assert "非强制工艺极限" in rule.diagnostics[0].suggestion


def test_tank_wall_accounts_each_material_separately(single_stage_vehicle: Vehicle) -> None:
    """混合材料（同级两箱不同料）：阈值逐材料列出、来源聚齐两个 id、判定逐箱独立。

    氧化剂箱 al-2219（典型 2.0 mm）壁厚 2.0 mm 恰好通过；燃料箱 cfrp-epoxy
    （典型 2.5 mm）同壁厚则警告——「逐箱回落」不是「取最严的一刀切」。
    """
    mixed = _with_fuel_tank_material(_with_tank_wall(single_stage_vehicle, 0.002), "cfrp-epoxy")

    rule = _rule(run_diagnostics(mixed), CODE_TANK_WALL)

    assert not rule.deferred
    assert [item.field_path for item in rule.diagnostics] == [
        "stages[0].geometry.fuel_tank.wall_thickness_m"
    ]
    assert rule.source.startswith("材料库典型值（材料：")
    assert "al-2219" in rule.source and "cfrp-epoxy" in rule.source
    assert "0.0025" in rule.threshold and "0.002 m" in rule.threshold


def test_tank_wall_fires_hard_once_the_minimum_is_configured(
    single_stage_vehicle: Vehicle,
) -> None:
    """显式配置（§18.4）优先于材料库典型值：违反回到**硬**级（§6.5 表级别）。"""
    thresholds = DiagnosticThresholds(min_tank_wall_thickness_m=0.01)

    report = run_diagnostics(single_stage_vehicle, thresholds=thresholds)

    rule = _rule(report, CODE_TANK_WALL)
    assert not rule.deferred
    assert [item.field_path for item in rule.diagnostics] == [
        "stages[0].geometry.oxidizer_tank.wall_thickness_m",
        "stages[0].geometry.fuel_tank.wall_thickness_m",
    ]
    assert {item.level for item in rule.diagnostics} == {"hard"}
    # 上界（壁厚 < 直径/2）由 §6.3 的安全边界判定，同一违反不得有两个判定码
    assert all("SAFETY_TANK_WALL_TOO_THICK" in note for note in rule.uncovered)


def test_tank_wall_configured_source_reports_the_winning_layer(
    single_stage_vehicle: Vehicle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """来源标注随生效层走：``环境变量 > config.toml``（§18.4 优先级不得写反）。"""
    monkeypatch.delenv("AEROFORGE_MIN_TANK_WALL_THICKNESS_M", raising=False)
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("min_tank_wall_thickness_m = 0.01\n", encoding="utf-8")
    try:
        report = run_diagnostics(single_stage_vehicle)
        rule = _rule(report, CODE_TANK_WALL)
        assert not rule.deferred
        assert {item.level for item in rule.diagnostics} == {"hard"}
        assert "0.01" in rule.threshold
        assert rule.source == "config.toml"

        monkeypatch.setenv("AEROFORGE_MIN_TANK_WALL_THICKNESS_M", "0.008")
        env_report = run_diagnostics(single_stage_vehicle)
        env_rule = _rule(env_report, CODE_TANK_WALL)
        assert env_rule.source == "环境变量"
        assert "0.008" in env_rule.threshold
    finally:
        path.unlink()
    assert not path.exists(), "配置文件未清理会让同会话的后续用例读到它"


# ---------------------------------------------------------------------------
# 长径比（警）
# ---------------------------------------------------------------------------


def test_length_to_diameter_warns_out_of_range() -> None:
    vehicle = _legal_vehicle("细长箭", (make_stage(1, length_m=200.0),))

    report = run_diagnostics(vehicle)

    rule = _rule(report, CODE_LENGTH_TO_DIAMETER)
    assert len(rule.diagnostics) == 1
    assert rule.diagnostics[0].level == "warning"
    assert rule.diagnostics[0].field_path == "stages[0].length_m"
    assert "超出区间 [5.0, 30.0]" in rule.diagnostics[0].message


def test_length_to_diameter_covers_every_stage(two_stage_vehicle: Vehicle) -> None:
    """**每级**都要判，不是只看一级：二级（12.5 m / 3.7 m → 3.38）低于下界必须被点名。

    ⚠ 短上面级落在下界之下是**真实构型**，这条裁定是"提示复核"而非缺陷——
    但它必须出现，否则前端会以为二级没被检查过。
    """
    report = run_diagnostics(two_stage_vehicle)

    rule = _rule(report, CODE_LENGTH_TO_DIAMETER)
    assert [item.field_path for item in rule.diagnostics] == ["stages[1].length_m"]
    assert "3.38" in rule.diagnostics[0].message
    # 一级（41.2 m / 3.7 m → 11.14）在区间内，不得被误报
    assert all("第 1 级" not in item.message for item in rule.diagnostics)


# ---------------------------------------------------------------------------
# 阈值来源与可配置性（OI-08 / §18.4）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(DiagnosticThresholds.model_fields), ids=str)
def test_every_threshold_description_carries_the_source_disclaimer(field: str) -> None:
    """§6.5 强制：每条阈值在代码与 UI 中都必须携带来源标注，不得当作"规格规定"。"""
    description = DiagnosticThresholds.model_fields[field].description or ""

    assert SOURCE_CONVENTION in description or SOURCE_PROCESS_LIMIT in description, (
        f"阈值 {field} 的说明缺少来源标注：{description}"
    )


def test_thresholds_come_from_the_environment(
    single_stage_vehicle: Vehicle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§18.4：环境变量覆盖默认值，且**真的改变裁定**（不只是改了个字符串）。"""
    baseline = run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _LIGHT_LOAD_KG})
    assert _rule(baseline, CODE_TWR).diagnostics == ()

    monkeypatch.setenv("AEROFORGE_TWR_LIQUID_MIN", "2.0")
    assert DiagnosticThresholds().twr_liquid_min == pytest.approx(2.0)

    tightened = run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _LIGHT_LOAD_KG})
    rule = _rule(tightened, CODE_TWR)
    assert len(rule.diagnostics) == 1
    assert "低于下界 2.0" in rule.diagnostics[0].message
    assert "2.0" in rule.threshold


def test_thresholds_come_from_the_config_file(single_stage_vehicle: Vehicle) -> None:
    """§18.4：``config.toml`` 覆盖默认值；文件不存在即只有环境变量与默认值（正常状态）。"""
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("twr_liquid_min = 2.0\n", encoding="utf-8")
    try:
        assert DiagnosticThresholds().twr_liquid_min == pytest.approx(2.0)

        report = run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _LIGHT_LOAD_KG})
        rule = _rule(report, CODE_TWR)
        assert len(rule.diagnostics) == 1
        assert "低于下界 2.0" in rule.diagnostics[0].message
    finally:
        path.unlink()
    assert not path.exists(), "配置文件未清理会让同会话的后续用例读到它"


def test_environment_beats_the_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """优先级 ``环境变量 > config.toml > 默认值``（§18.4）——顺序写反了这里就会红。"""
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("twr_liquid_min = 2.0\n", encoding="utf-8")
    try:
        monkeypatch.setenv("AEROFORGE_TWR_LIQUID_MIN", "3.0")
        assert DiagnosticThresholds().twr_liquid_min == pytest.approx(3.0)
    finally:
        path.unlink()


def test_thresholds_reject_non_positive_values() -> None:
    """阈值本身也要过校验：0 或负数会让判据永远通过（静默失效）。"""
    with pytest.raises(ValueError):  # pydantic 的 ValidationError 是 ValueError 子类
        DiagnosticThresholds(twr_liquid_min=0.0)


def test_unknown_config_keys_are_ignored() -> None:
    """``extra="ignore"``：``config.toml`` 里可能有别的域的键，不得因此崩掉诊断。"""
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("twr_liquid_min = 1.3\nsome_future_option = 7\n", encoding="utf-8")
    try:
        thresholds = DiagnosticThresholds()
        assert thresholds.twr_liquid_min == pytest.approx(1.3)
    finally:
        path.unlink()


def test_run_diagnostics_is_pure_with_respect_to_the_vehicle(single_stage_vehicle: Vehicle) -> None:
    """诊断不得改动入参（``Vehicle`` 是 frozen，但派生与聚合仍可能"就地改"）。"""
    before: dict[str, Any] = single_stage_vehicle.model_dump(mode="json")

    run_diagnostics(single_stage_vehicle, propellant_mass_kg={1: _LIGHT_LOAD_KG})
    run_diagnostics(single_stage_vehicle, thresholds=DiagnosticThresholds(twr_liquid_min=9.9))

    assert single_stage_vehicle.model_dump(mode="json") == before
