"""OpenRocket ``.ork`` 火箭定义解析器（规格 §7.5——基线导入，**不保证全字段保真**）。

只读取导入器关心的最小子集：火箭名、级（``<axialstage>``，兼容旧方言
``<stage>`` 与连字符变体 ``<axial-stage>``）、各级可用时的长度/直径/质量与
``<motor code=...>`` 发动机型号列表。结果一律 ``derived=True``；遇到未识别的
顶层/一级元素计入 ``warnings``（行文含元素名），不中断解析（R-21 不静默）。

真实 ``.ork`` 是 gzip 压缩的 XML——解压由 :func:`decode_import_bytes` 按魔数
统一处理，本模块仍是纯文本进、结果出。XML 解析用标准库
``xml.etree.ElementTree``（规格 §18：不引入额外 XML 依赖）。
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from aeroforge.data.parsers import ParserError, RocketParseResult, RocketStage

_STAGE_TAGS = {"axialstage", "axial-stage", "stage"}
_STAGE_NUMBER_TAGS = ("length", "diameter", "mass")


def parse_ork(text: str) -> RocketParseResult:
    """解析 ``.ork`` 文本 → 火箭结果（纯函数，文本进、结果出，derived=True）。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        line, column = exc.position
        raise ParserError(f"XML 解析失败（第 {line} 行第 {column} 列）：{exc}") from exc
    if root.tag != "openrocket":
        raise ParserError(f"根元素 <{root.tag}> 不是 OpenRocket 的 <openrocket>，无法按 .ork 解析")

    warnings: list[str] = []
    for child in root:
        if child.tag != "rocket":
            warnings.append(f"未识别的顶层元素 <{child.tag}>（已忽略）")
    rocket = root.find("rocket")
    if rocket is None:
        raise ParserError("<openrocket> 下未找到 <rocket> 元素（XPath /openrocket/rocket）")
    for child in rocket:
        if child.tag != "name" and child.tag not in _STAGE_TAGS:
            warnings.append(f"<rocket> 下未识别的一级元素 <{child.tag}>（已忽略）")

    stage_els = [child for child in rocket if child.tag in _STAGE_TAGS]
    if not stage_els:
        raise ParserError(
            "<rocket> 下未找到任何级元素（<axialstage>/<stage>，"
            "XPath /openrocket/rocket/axialstage）"
        )
    stages = [_parse_stage(el, index, warnings) for index, el in enumerate(stage_els, start=1)]
    name = (rocket.findtext("name") or "").strip()
    return RocketParseResult(
        format="ork",
        name=name or "未命名火箭",
        stage_count=len(stages),
        stages=stages,
        warnings=warnings,
    )


def _parse_stage(stage_el: ET.Element, index: int, warnings: list[str]) -> RocketStage:
    """抽取一级的可用字段（缺元素 → None；文本写错 → 显式报错，R-21）。"""
    stage_path = f"/openrocket/rocket/axialstage[{index}]"
    name = (stage_el.findtext("name") or "").strip() or f"第 {index} 级"
    values = {tag: _stage_number(stage_el, tag, stage_path) for tag in _STAGE_NUMBER_TAGS}
    engines: list[str] = []
    for motor in stage_el.iter("motor"):
        code = motor.get("code")
        if code and code.strip():
            engines.append(code.strip())
        else:
            warnings.append(f"{stage_path} 下有 <motor> 缺少 code 属性，未计入发动机列表")
    return RocketStage(
        name=name,
        length_m=values["length"],
        diameter_m=values["diameter"],
        mass_kg=values["mass"],
        engines=engines,
    )


def _stage_number(stage_el: ET.Element, tag: str, stage_path: str) -> float | None:
    """读级的数值子元素（长度 m / 直径 m / 质量 kg）；元素缺失返回 None。"""
    el = stage_el.find(tag)
    if el is None or el.text is None or not el.text.strip():
        return None
    try:
        return float(el.text.strip())
    except ValueError as exc:
        raise ParserError(f"{stage_path}/{tag} 的文本 {el.text.strip()!r} 无法解析为数值") from exc
