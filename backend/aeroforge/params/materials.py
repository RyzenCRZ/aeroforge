"""自建材料库（规格 §7.4「材料库数据源裁决」，M3 第五片）。

数据源裁决（R-10 同款门禁的核查结论）
------------------------------------
SPACEMATDB 经 2026-09-19 核查：站点**无明示开放许可**（仅 AS-IS 免责声明），
聚合上游含受版权保护的订阅库（MMPDS / MIL-HDBK-5 后继）与厂商 datasheet——
按「核不过即自建，不得带病开工」**不抓取**。材料库**自建**：Python 内嵌，
与模板（:mod:`aeroforge.params.templates`）同形态，冻结零风险。

溯源纪律（AGENTS.md：无来源不予合并）
------------------------------------
- 每条材料带 ``source``（公开手册 / 标准 / 厂商 datasheet 的出处文字）与
  ``heat_treatment``（试验条件——同一合金不同热处理态的性能差数倍，
  不写状态的数值无法复用）；
- ``quality`` 是质量标签：``literature`` = 实测手册值（出处可回查）；
  ``typical`` = 工程典型值（铺层强依赖、厂商典型等，**不得用于结论性导出**）；
- 比强度 / 比刚度是**派生量**（由 σ_y、E、ρ 计算，:func:`specific_strength` /
  :func:`specific_stiffness`），**不存储**（P1：同族错误是"同一物理量存两份
  可互相矛盾的数"）。

数值口径：均为公开手册 / 厂商 datasheet 的**室温典型值**（非设计许用值 A/B 基），
仅供方案级筛选与量级估算；温度区间是工程使用下限 / 上限的量级表述（低温侧
铝合金 / 不锈钢 / 钛合金按 cryogenic 应用惯例记 -270 °C）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MaterialCategory = Literal["铝合金", "不锈钢", "钛合金", "复材", "高温合金"]
"""材料类别（§7.4：箭体 / 贮箱 / 热环境常用材料的五类）。"""

MaterialQuality = Literal["literature", "typical"]
"""质量标签：literature = 实测手册值；typical = 工程典型值（不得用于结论性导出）。"""


@dataclass(frozen=True)
class MaterialEntry:
    """一条材料记录（§7.4 材料库表逐行对应）。

    ``yield_strength_pa`` 是室温典型屈服强度（非设计许用值）；壁厚下界估算
    用它时须另计安全系数（§8.4 的职责，不在本层）。
    """

    id: str
    """材料 id：Schema 三层 ``material`` 字段引用的值（值域校验见约束引擎）。"""
    name: str
    """牌号名（含热处理态缩写，如 2219-T87）。"""
    category: MaterialCategory
    density_kg_m3: float
    """密度（kg/m³）：质量估算（§8.4）的输入。"""
    elastic_modulus_pa: float
    """弹性模量（Pa，室温）。"""
    yield_strength_pa: float
    """屈服强度（Pa，室温典型值；复材为许用典型）。"""
    service_temp_min_c: float
    """工作温度下限（°C，量级表述）。"""
    service_temp_max_c: float
    """工作温度上限（°C，量级表述）。"""
    typical_min_wall_thickness_m: float
    """典型工艺壁厚下限（m）：工程惯例典型值（非强制工艺极限），
    作为 §6.5 壁厚判据未显式配置时的回落档（§7.4 QA-3）。"""
    heat_treatment: str
    """热处理 / 试验条件：同一合金不同状态性能差数倍，不写状态即不可复用。"""
    source: str | None
    """出处文字（公开手册 / 标准 / 厂商 datasheet）；typical 条目可为 None。"""
    quality: MaterialQuality
    """质量标签：literature（实测手册值）/ typical（工程典型值）。"""


MATERIALS: tuple[MaterialEntry, ...] = (
    MaterialEntry(
        id="al-2219",
        name="2219-T87",
        category="铝合金",
        density_kg_m3=2840.0,
        elastic_modulus_pa=73e9,
        yield_strength_pa=290e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=200.0,
        typical_min_wall_thickness_m=2.0e-3,
        heat_treatment="T87（固溶 + 冷作 + 人工时效）",
        source="MIL-HDBK-5J（2219-T87 板材室温典型值）",
        quality="literature",
    ),
    MaterialEntry(
        id="al-li-2198",
        name="2198-T8",
        category="铝合金",
        density_kg_m3=2700.0,
        elastic_modulus_pa=76e9,
        yield_strength_pa=450e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=150.0,
        typical_min_wall_thickness_m=2.0e-3,
        heat_treatment="T8（固溶 + 冷作 + 人工时效）",
        source="厂商 datasheet（2198-T8 典型值，非手册实测）",
        quality="typical",
    ),
    MaterialEntry(
        id="al-2195",
        name="2195-T8（Al-Li）",
        category="铝合金",
        density_kg_m3=2710.0,
        elastic_modulus_pa=76e9,
        yield_strength_pa=560e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=150.0,
        typical_min_wall_thickness_m=2.0e-3,
        heat_treatment="T8（固溶 + 冷作 + 人工时效）",
        source=(
            "NASA 超轻质贮箱报告与 MMPDS-02 典型值"
            "（2195-T8 板材，非本库实测；各向异性显著，取纵向典型值）"
        ),
        quality="typical",
    ),
    MaterialEntry(
        id="al-2090",
        name="2090-T83（Al-Li）",
        category="铝合金",
        density_kg_m3=2600.0,
        elastic_modulus_pa=77e9,
        yield_strength_pa=490e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=120.0,
        typical_min_wall_thickness_m=2.0e-3,
        heat_treatment="T83（固溶 + 冷作 + 人工时效）",
        source="公开文献典型值（2090-T83 板材，非手册实测；Al-Li 第一代工程合金）",
        quality="typical",
    ),
    MaterialEntry(
        id="al-8090",
        name="8090-T8771（Al-Li）",
        category="铝合金",
        density_kg_m3=2550.0,
        elastic_modulus_pa=78e9,
        yield_strength_pa=400e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=120.0,
        typical_min_wall_thickness_m=2.5e-3,
        heat_treatment="T8771（固溶 + 冷作 + 人工时效，板材）",
        source="公开文献典型值（8090-T8771 板材，非手册实测；低密度取向，刚度优先场景）",
        quality="typical",
    ),
    MaterialEntry(
        id="al-7075",
        name="7075-T6",
        category="铝合金",
        density_kg_m3=2810.0,
        elastic_modulus_pa=71.7e9,
        yield_strength_pa=503e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=120.0,
        typical_min_wall_thickness_m=2.0e-3,
        heat_treatment="T6（固溶 + 人工时效）",
        source="MIL-HDBK-5J（7075-T6 板材室温典型值）",
        quality="literature",
    ),
    MaterialEntry(
        id="al-2014",
        name="2014-T6",
        category="铝合金",
        density_kg_m3=2800.0,
        elastic_modulus_pa=73.4e9,
        yield_strength_pa=414e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=150.0,
        typical_min_wall_thickness_m=2.0e-3,
        heat_treatment="T6（固溶 + 人工时效）",
        source="MIL-HDBK-5J（2014-T6 板材室温典型值）",
        quality="literature",
    ),
    MaterialEntry(
        id="ss-301-fh",
        name="301 不锈钢（全硬态）",
        category="不锈钢",
        density_kg_m3=7920.0,
        elastic_modulus_pa=193e9,
        yield_strength_pa=1276e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=400.0,
        typical_min_wall_thickness_m=1.0e-3,
        heat_treatment="全硬态（Full Hard，冷轧）",
        source="厂商 datasheet（301 全硬态典型值，非手册实测）",
        quality="typical",
    ),
    MaterialEntry(
        id="ti-6al-4v",
        name="Ti-6Al-4V（STA）",
        category="钛合金",
        density_kg_m3=4430.0,
        elastic_modulus_pa=113.8e9,
        yield_strength_pa=880e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=400.0,
        typical_min_wall_thickness_m=1.5e-3,
        heat_treatment="STA（固溶 + 时效）",
        source="MIL-HDBK-5J（Ti-6Al-4V STA 退火板室温典型值）",
        quality="literature",
    ),
    MaterialEntry(
        id="cfrp-epoxy",
        name="碳纤维 / 环氧（准各向同性铺层）",
        category="复材",
        density_kg_m3=1600.0,
        elastic_modulus_pa=135e9,
        yield_strength_pa=800e6,
        service_temp_min_c=-150.0,
        service_temp_max_c=120.0,
        typical_min_wall_thickness_m=2.5e-3,
        heat_treatment="准各向同性铺层（0°/±45°/90° 均衡；性能随铺层比例强依赖）",
        source=None,
        quality="typical",
    ),
    MaterialEntry(
        id="inconel-718",
        name="Inconel 718（固溶 + 时效）",
        category="高温合金",
        density_kg_m3=8190.0,
        elastic_modulus_pa=200e9,
        yield_strength_pa=1030e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=650.0,
        typical_min_wall_thickness_m=1.5e-3,
        heat_treatment="固溶 + 双级时效",
        source="厂商 datasheet（Inconel 718 固溶 + 时效态室温值）",
        quality="literature",
    ),
    MaterialEntry(
        id="al-5083",
        name="5083-H116（焊接态）",
        category="铝合金",
        density_kg_m3=2660.0,
        elastic_modulus_pa=70.3e9,
        yield_strength_pa=228e6,
        service_temp_min_c=-270.0,
        service_temp_max_c=150.0,
        typical_min_wall_thickness_m=2.5e-3,
        heat_treatment="H116（加工硬化，适用于焊接结构）",
        source="MIL-HDBK-5J（5083-H116 板材室温典型值）",
        quality="literature",
    ),
)


def material_ids() -> tuple[str, ...]:
    """全部材料 id（值域清单：约束引擎据此判「未知材料」）。"""
    return tuple(entry.id for entry in MATERIALS)


def get_material(mid: str) -> MaterialEntry:
    """按 id 取材料；未知 id 抛 :class:`KeyError`（不猜、不静默回落）。"""
    for entry in MATERIALS:
        if entry.id == mid:
            return entry
    raise KeyError(f"未知材料 id：{mid!r}（可选值见 GET /api/catalog/materials）")


def specific_strength(yield_strength_pa: float, density_kg_m3: float) -> float:
    """比强度 σ_y / ρ（单位 m²/s²，即 N·m/kg）——派生量，不存储（P1）。"""
    return yield_strength_pa / density_kg_m3


def specific_stiffness(elastic_modulus_pa: float, density_kg_m3: float) -> float:
    """比刚度 E / ρ（单位 m²/s²，即 N·m/kg）——派生量，不存储（P1）。"""
    return elastic_modulus_pa / density_kg_m3
