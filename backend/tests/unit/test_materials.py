"""自建材料库（§7.4「材料库数据源裁决」，M3 第五片）。

盯三件事：

1. **溯源纪律的机检**（AGENTS.md：无来源不予合并）：每条材料要么带 ``source``
   出处文字、要么显式标 ``typical``——两者皆无的数值不得入库；``literature``
   条目必须给出出处。
2. **派生量不存储**（P1）：比强度 / 比刚度只能由 :func:`specific_strength` /
   :func:`specific_stiffness` 现算，``MaterialEntry`` 上不得出现这两个字段。
3. **九条锚点齐全且 id 唯一**：值域校验（HARD_MATERIAL_UNKNOWN）与模板 / 夹具
   引用的合法性全部押在「库内 id 恰是这九个」之上。
"""

from __future__ import annotations

from dataclasses import fields as dataclass_fields

import pytest

from aeroforge.params.materials import (
    MATERIALS,
    MaterialEntry,
    get_material,
    material_ids,
    specific_stiffness,
    specific_strength,
)

#: 十二条材料的 id 锚点（§7.4 点名收录 + 铝锂系 2195/2090/8090；顺序即库内呈现顺序）。
_EXPECTED_IDS = (
    "al-2219",
    "al-li-2198",
    "al-2195",
    "al-2090",
    "al-8090",
    "al-7075",
    "al-2014",
    "ss-301-fh",
    "ti-6al-4v",
    "cfrp-epoxy",
    "inconel-718",
    "al-5083",
)


def test_library_contains_exactly_the_anchored_entries() -> None:
    assert material_ids() == _EXPECTED_IDS
    assert len(MATERIALS) == 12


def test_ids_are_unique() -> None:
    ids = [entry.id for entry in MATERIALS]
    assert len(ids) == len(set(ids)), f"材料 id 重复：{ids}"


def test_every_entry_is_frozen() -> None:
    """与模板同形态：内嵌资产 frozen——防止运行期被就地改写形成第二份真相。"""
    entry = get_material("al-2219")
    with pytest.raises(AttributeError):
        entry.density_kg_m3 = 9999.0  # type: ignore[misc]


def test_every_entry_has_source_or_is_marked_typical() -> None:
    """AGENTS.md 纪律机检：无来源的数值不得入库（literature 必带出处文字）。"""
    for entry in MATERIALS:
        assert entry.quality in ("literature", "typical"), f"{entry.id} 的质量标签非法"
        if entry.quality == "literature":
            assert entry.source and entry.source.strip(), (
                f"{entry.id} 标为 literature 却没有出处文字（溯源红线 §1.4-4）"
            )
        else:  # typical：source 可空，但「不确定」这一状态必须显式标出
            assert entry.heat_treatment.strip(), f"{entry.id} 缺试验条件（数值不可复用）"


def test_derived_quantities_are_not_stored_on_entries() -> None:
    """P1：比强度 / 比刚度是派生量——出现成字段即形成两份可矛盾的真相。"""
    names = {field.name for field in dataclass_fields(MaterialEntry)}
    assert "specific_strength" not in names
    assert "specific_stiffness" not in names
    assert "specific_strength_m2_s2" not in names
    assert "specific_stiffness_m2_s2" not in names


def test_specific_strength_and_stiffness_computations() -> None:
    al = get_material("al-2219")
    assert specific_strength(al.yield_strength_pa, al.density_kg_m3) == pytest.approx(
        290e6 / 2840.0
    )
    assert specific_stiffness(al.elastic_modulus_pa, al.density_kg_m3) == pytest.approx(
        73e9 / 2840.0
    )
    # 派生函数是纯函数：不依赖库、不读库
    assert specific_strength(1.0e6, 2.0) == pytest.approx(5.0e5)
    assert specific_stiffness(1.0e9, 4.0) == pytest.approx(2.5e8)


def test_unknown_id_raises_key_error() -> None:
    with pytest.raises(KeyError, match="未知材料"):
        get_material("unobtanium-3000")
    with pytest.raises(KeyError):
        get_material("Al-2219")  # 旧写法（非库 id）必须拒绝——值域是精确 id


def test_anchored_values_for_the_two_reference_alloys() -> None:
    """模板 / 夹具引用的两条铝合金：数值锚点逐项对表（防手滑改库）。"""
    al2219 = get_material("al-2219")
    assert al2219.name == "2219-T87"
    assert al2219.density_kg_m3 == pytest.approx(2840.0)
    assert al2219.elastic_modulus_pa == pytest.approx(73e9)
    assert al2219.yield_strength_pa == pytest.approx(290e6)
    assert al2219.service_temp_min_c == pytest.approx(-270.0)
    assert al2219.service_temp_max_c == pytest.approx(200.0)
    assert al2219.typical_min_wall_thickness_m == pytest.approx(2.0e-3)
    assert al2219.quality == "literature"

    alli2198 = get_material("al-li-2198")
    assert alli2198.density_kg_m3 == pytest.approx(2700.0)
    assert alli2198.elastic_modulus_pa == pytest.approx(76e9)
    assert alli2198.yield_strength_pa == pytest.approx(450e6)
    assert alli2198.typical_min_wall_thickness_m == pytest.approx(2.0e-3)
    assert alli2198.quality == "typical"  # 厂商 datasheet 典型值，非手册实测
