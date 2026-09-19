"""推进剂物性表：§5.9 箱体比例派生的密度输入。

范围声明（重要）
----------------
本表是 **M2 落地箱体比例派生所需的最小内置集**，随完成时间早于 M3 数据层。
M3 引入 ``/api/catalog/propellants``（§10.1）后，**取值一律改由数据层快照提供**，
本表退化为内置默认值——不得演变成第二份互相矛盾的推进剂数据库。

数值口径：**公开资料常用值，标注为工程惯例**，不是权威来源；密度是**常温常压附近的
标称值**，不含低温收缩、蒸发与温度分层。故本表只用于**容积比与箱长的一阶估算**，
⚠ 严禁据此宣称性能精度（§1.4-4 溯源红线）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: 推进剂组合枚举。取值即 :data:`PROPELLANTS` 的键——两处必须同步（同文件内定义，无漂移空间）。
PropellantCombination = Literal["LOX/RP-1", "LOX/LH2", "LOX/CH4", "N2O4/UDMH"]

_SOURCE = "公开资料常用值（工程惯例，非权威来源）"


class PropellantProperties(BaseModel):
    """一组推进剂（氧化剂 + 燃料）的物性。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    combination: PropellantCombination
    oxidizer: str = Field(description="氧化剂名称")
    fuel: str = Field(description="燃料名称")
    density_ox_kg_m3: float = Field(gt=0.0, description="氧化剂标称密度（kg/m³）")
    density_fuel_kg_m3: float = Field(gt=0.0, description="燃料标称密度（kg/m³）")
    source: str = Field(default=_SOURCE, description="取值来源声明（§1.4-4 要求可溯源）")


PROPELLANTS: dict[PropellantCombination, PropellantProperties] = {
    "LOX/RP-1": PropellantProperties(
        combination="LOX/RP-1",
        oxidizer="LOX",
        fuel="RP-1",
        density_ox_kg_m3=1141.0,
        density_fuel_kg_m3=810.0,
    ),
    "LOX/LH2": PropellantProperties(
        combination="LOX/LH2",
        oxidizer="LOX",
        fuel="LH2",
        # 液氢密度极低，是"氧化剂箱体积通常更大"这条经验在氢氧级上**失效**的原因（§5.9 非铁律）
        density_ox_kg_m3=1141.0,
        density_fuel_kg_m3=71.0,
    ),
    "LOX/CH4": PropellantProperties(
        combination="LOX/CH4",
        oxidizer="LOX",
        fuel="CH4",
        density_ox_kg_m3=1141.0,
        density_fuel_kg_m3=422.6,
    ),
    "N2O4/UDMH": PropellantProperties(
        combination="N2O4/UDMH",
        oxidizer="N2O4",
        fuel="UDMH",
        density_ox_kg_m3=1443.0,
        density_fuel_kg_m3=791.0,
    ),
}


def properties(combination: PropellantCombination) -> PropellantProperties:
    """取物性；组合必须在枚举内（调用方已由 pydantic 保证，此处是兜底断言）。"""
    try:
        return PROPELLANTS[combination]
    except KeyError as exc:  # pragma: no cover - 枚举外取值只能来自绕过 Schema 的调用
        msg = f"未知的推进剂组合 {combination!r}；可选：{sorted(PROPELLANTS)}"
        raise KeyError(msg) from exc


def fuel_is_lh2(combination: PropellantCombination) -> bool:
    """燃料是否为液氢。

    §5.9「四项建模口径」第 2 条要求**共底且 LH₂ 侧强制加隔热层**
    （其沸点 20 K，共享隔板不做隔温就会把另一侧的推进剂冻住），
    故该判定必须来自物性表而不是散在实现里的字符串比较。
    """
    return properties(combination).fuel == "LH2"
