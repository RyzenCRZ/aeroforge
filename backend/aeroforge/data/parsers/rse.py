"""thrustcurve ``.rse`` 发动机数据解析器（规格 §7.5，XML）。

用标准库 ``xml.etree.ElementTree``（规格 §18：不引入额外 XML 依赖）。识别两个
方言根元素 ``<engine-directory>`` / ``<engine-list>``，取其中 ``<engine>``
（mfg / code / type / delays 属性 + 质量属性 massTotal / massProp，kg）与其下
``<data><point t f/>`` 点列；数值单位按 RSE 约定直接读取（推力 N、时间 s）。

异常口径与 ``eng`` 一致（同一套 :func:`curve_warnings`）：XML 解析失败带行号，
属性/点列问题带 XPath；表头对照项（总冲偏差、燃烧时间偏差）因 ``.rse`` 无表头
声明而自动跳过。``<comment>`` 为方言定义的说明元素，按约定忽略。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from aeroforge.data.parsers import (
    MotorParseResult,
    ParserError,
    ThrustPoint,
    curve_warnings,
    trapezoid_impulse,
)

_ROOT_TAGS = {"engine-directory", "engine-list"}
_ENGINE_CHILD_TAGS = {"data", "comment"}


def parse_rse(text: str) -> MotorParseResult:
    """解析 ``.rse`` 文本 → 发动机结果（纯函数，文本进、结果出）。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        line, column = exc.position
        raise ParserError(f"XML 解析失败（第 {line} 行第 {column} 列）：{exc}") from exc
    if root.tag not in _ROOT_TAGS:
        raise ParserError(
            f"根元素 <{root.tag}> 不是 RSE 的 <engine-directory>/<engine-list>，无法按 .rse 解析"
        )

    engines = root.findall(".//engine")
    if not engines:
        raise ParserError(
            f"根元素 <{root.tag}> 下未找到任何 <engine> 元素（XPath /{root.tag}/engine）"
        )
    engine = engines[0]
    warnings: list[str] = []
    if len(engines) > 1:
        warnings.append(
            f"文件包含 {len(engines)} 个 <engine>，仅解析第一个"
            f"（code={engine.get('code')!r}），其余已忽略"
        )
    for child in engine:
        if child.tag not in _ENGINE_CHILD_TAGS:
            warnings.append(
                f"未识别的 <engine> 子元素 <{child.tag}>"
                f"（XPath /{root.tag}/engine/{child.tag}），已忽略"
            )

    name = (engine.get("code") or "").strip()
    if not name:
        raise ParserError(f"<engine> 缺少 code 属性（XPath /{root.tag}/engine/@code）")
    delays = [d for d in re.split(r"[,;\s]+", engine.get("delays") or "") if d]

    points: list[ThrustPoint] = []
    refs: list[str] = []
    for index, point in enumerate(engine.findall("data/point"), start=1):
        xpath = f"/{root.tag}/engine/data/point[{index}]"
        points.append(
            ThrustPoint(
                time_s=_point_attr(point, "t", xpath), thrust_n=_point_attr(point, "f", xpath)
            )
        )
        refs.append(xpath)
    if not points:
        raise ParserError(
            f"<engine code={name!r}> 下没有 <data>/<point> 推力数据点"
            f"（XPath /{root.tag}/engine/data/point）"
        )

    impulse = trapezoid_impulse(points)
    burn_time = points[-1].time_s
    return MotorParseResult(
        format="rse",
        name=name,
        manufacturer=engine.get("mfg"),
        motor_type=engine.get("type"),
        delays=delays,
        total_impulse_ns=impulse,
        burn_time_s=burn_time,
        avg_thrust_n=impulse / burn_time if burn_time > 0 else 0.0,
        peak_thrust_n=max(point.thrust_n for point in points),
        propellant_mass_kg=_mass_attr(engine, "massProp"),
        total_mass_kg=_mass_attr(engine, "massTotal"),
        thrust_curve=points,
        warnings=warnings + curve_warnings(points, refs),
        unit_conversions=[
            "RSE 数据点按 RSE 约定的 SI 单位读取：推力 f 为 N、时间 t 为 s；质量属性为 kg"
        ],
    )


def _point_attr(point: ET.Element, attr: str, xpath: str) -> float:
    """读数据点的数值属性；缺失或写错都显式报错（R-21）。"""
    value = point.get(attr)
    if value is None:
        raise ParserError(f"{xpath} 缺少 {attr!r} 属性")
    try:
        return float(value)
    except ValueError as exc:
        raise ParserError(f"{xpath} 的 {attr}={value!r} 无法解析为数值") from exc


def _mass_attr(engine: ET.Element, attr: str) -> float | None:
    """读 ``<engine>`` 的质量属性（kg）；缺省返回 None，写错则显式报错（R-21）。"""
    value = engine.get(attr)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ParserError(f"<engine> 的 {attr}={value!r} 无法解析为数值（kg）") from exc
