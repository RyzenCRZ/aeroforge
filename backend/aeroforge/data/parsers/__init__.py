"""外部格式解析器（规格 §7.5 / §15：``.eng`` / ``.rse`` / ``.ork`` → 推进参数）。

本包只做**纯解析**：文本进、结果出，不做任何 IO（读文件与解码由端点 / CLI 负责），
且只依赖 Python 标准库（规格 §18：XML 用 ``xml.etree.ElementTree``，不引入额外
XML 依赖）。

失败处理（R-21）：解析失败一律抛 :class:`ParserError`，错误信息必须带行号或
XPath 与原文/元素片段；可解析但可疑的内容（负推力、时间非单调、与表头声明偏差
超限等）计入结果的 ``warnings``——**禁止静默容错**。

- ``eng``——RASP ``.eng`` 固体发动机推力曲线（文本）
- ``rse``——thrustcurve ``.rse`` 发动机数据（XML）
- ``ork``——OpenRocket ``.ork`` 火箭定义（XML，结果一律标注 ``derived=True``）
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Sequence
from itertools import pairwise
from pathlib import PurePath
from typing import Literal

from pydantic import BaseModel, Field

from aeroforge.errors import AeroForgeError

MotorFormat = Literal["eng", "rse"]


class ParserError(AeroForgeError):
    """导入解析失败（R-21：显式报错并降级为手动录入，禁止静默容错）。

    ``message`` 必须携带定位信息（文本格式的行号 / XML 的 XPath）与原文或元素片段。
    """

    code = "IMPORT_PARSE_FAILED"
    stage = "import"

    def __init__(self, message: str, *, suggestion: str | None = None) -> None:
        super().__init__(
            message,
            suggestion=suggestion
            or "请按错误信息中的行号/元素定位核对文件内容；无法修复时改用手动录入（R-21 降级路径）",
        )


class UnsupportedFormatError(ParserError):
    """文件扩展名不在支持列表（``.eng`` / ``.rse`` / ``.ork``）内。"""

    code = "IMPORT_FORMAT_UNSUPPORTED"

    def __init__(self, filename: str) -> None:
        super().__init__(
            f"不支持的导入格式：{filename!r}（按扩展名识别，仅支持 .eng / .rse / .ork）",
            suggestion=(
                "确认文件扩展名正确：.eng 为 RASP 推力曲线、.rse 为 thrustcurve XML、"
                ".ork 为 OpenRocket 工程"
            ),
        )


class ThrustPoint(BaseModel):
    """推力曲线上的一个点（已统一为 SI 单位）。"""

    time_s: float = Field(description="时间（s）")
    thrust_n: float = Field(description="推力（N）")


class MotorParseResult(BaseModel):
    """发动机解析结果（``.eng`` / ``.rse`` 同构）。字段名带 SI 单位后缀。"""

    format: MotorFormat
    name: str = Field(description="发动机型号（.eng 表头首列 / .rse 的 code 属性）")
    manufacturer: str | None = Field(default=None, description="制造商（仅 .rse 提供）")
    motor_type: str | None = Field(default=None, description="发动机类型（仅 .rse 的 type 属性）")
    delays: list[str] = Field(default_factory=list, description="可用延时（数字或 P=堵死）")
    total_impulse_ns: float = Field(description="总冲（N·s，曲线梯形积分）")
    burn_time_s: float = Field(description="燃烧时间（s，曲线末点时间）")
    avg_thrust_n: float = Field(description="平均推力（N，总冲 ÷ 燃烧时间）")
    peak_thrust_n: float = Field(description="峰值推力（N）")
    propellant_mass_kg: float | None = Field(default=None, description="推进剂质量（kg，有则给出）")
    total_mass_kg: float | None = Field(default=None, description="总质量（kg，有则给出）")
    thrust_curve: list[ThrustPoint] = Field(description="推力曲线（已统一为 SI）")
    warnings: list[str] = Field(default_factory=list, description="可疑内容清单（R-21：不静默）")
    unit_conversions: list[str] = Field(
        default_factory=list, description="逐条单位换算声明（§7.5：显式声明源单位）"
    )
    derived: bool = Field(default=False, description="是否为推导值（.eng/.rse 为 False）")


class RocketStage(BaseModel):
    """``.ork`` 中的一级（字段仅在该级可用时给出，§7.5：不保证全字段保真）。"""

    name: str
    length_m: float | None = None
    diameter_m: float | None = None
    mass_kg: float | None = None
    engines: list[str] = Field(default_factory=list, description="该级发动机型号列表")


class RocketParseResult(BaseModel):
    """``.ork`` 火箭解析结果。**一切字段均为 derived**（规格 §7.5）。"""

    format: Literal["ork"]
    name: str
    derived: bool = Field(default=True, description="恒为 True：.ork 结果不保证全字段保真")
    stage_count: int
    stages: list[RocketStage]
    warnings: list[str] = Field(default_factory=list, description="未识别元素等报告（R-21）")


#: 曲线积分总冲与表头声明的偏差上限（.eng 表头交叉校验）。
_IMPULSE_DEVIATION = 0.02
#: 曲线末点时间与表头燃烧时间的偏差上限。
_BURN_TIME_DEVIATION = 0.05


def trapezoid_impulse(points: Sequence[ThrustPoint]) -> float:
    """推力曲线的梯形积分（N·s）。"""
    return sum((b.thrust_n + a.thrust_n) * 0.5 * (b.time_s - a.time_s) for a, b in pairwise(points))


def curve_warnings(
    points: Sequence[ThrustPoint],
    refs: Sequence[str],
    *,
    header_impulse_ns: float | None = None,
    header_burn_time_s: float | None = None,
) -> list[str]:
    """对已统一为 SI 的推力曲线做异常检测（``.eng`` 与 ``.rse`` 同一口径）。

    ``refs`` 与 ``points`` 等长，是每个点的来源定位（.eng 行号 / .rse XPath），
    保证每条警告都能指回原文（R-21）。表头对照项（总冲偏差、燃烧时间偏差）仅在
    声明值存在时检查——``.rse`` 无表头，自动跳过。
    """
    warnings: list[str] = []
    negative = [ref for ref, point in zip(refs, points, strict=True) if point.thrust_n < 0]
    if negative:
        warnings.append(f"出现负推力（保留原值未修正）：{'、'.join(negative)}")
    for index in range(1, len(points)):
        a, b = points[index - 1], points[index]
        ref_a, ref_b = refs[index - 1], refs[index]
        if b.time_s < a.time_s:
            warnings.append(
                f"时间非单调：{ref_b} 的 t={b.time_s:g} s 早于 {ref_a} 的 t={a.time_s:g} s"
            )
            break
    if points[0].time_s != 0:
        warnings.append(f"首点时间 {points[0].time_s:g} s ≠ 0（{refs[0]}）")
    if max(point.thrust_n for point in points) <= 0:
        warnings.append("空曲线：所有数据点推力 ≤ 0")

    impulse = trapezoid_impulse(points)
    if header_impulse_ns is not None and header_impulse_ns > 0:
        deviation = abs(impulse - header_impulse_ns) / header_impulse_ns
        if deviation > _IMPULSE_DEVIATION:
            warnings.append(
                f"曲线积分总冲 {impulse:.3f} N·s 与表头 {header_impulse_ns:.3f} N·s "
                f"偏差 {deviation:.1%}，超过 2%"
            )
    if header_burn_time_s is not None and header_burn_time_s > 0:
        end = points[-1].time_s
        deviation = abs(end - header_burn_time_s) / header_burn_time_s
        if deviation > _BURN_TIME_DEVIATION:
            warnings.append(
                f"曲线末点时间 {end:.3f} s 与表头燃烧时间 {header_burn_time_s:.3f} s "
                f"偏差 {deviation:.1%}，超过 5%"
            )
    return warnings


_GZIP_MAGIC = b"\x1f\x8b"


def decode_import_bytes(data: bytes, filename: str) -> str:
    """把上传/读入的字节还原为文本（纯函数，bytes → str）。

    真实 ``.ork`` 是 gzip 压缩的 XML，按魔数识别解压；空内容、坏 gzip、非 UTF-8
    一律显式报错（R-21）。BOM 透明剥离（``utf-8-sig``）。
    """
    if data[:2] == _GZIP_MAGIC:
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError, zlib.error) as exc:
            raise ParserError(f"{filename}: gzip 解压失败：{exc}") from exc
    if not data.strip():
        raise ParserError(f"{filename}: 文件为空，无内容可解析", suggestion="确认选择了正确的文件")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParserError(f"{filename}: 内容不是有效的 UTF-8 文本（{exc}）") from exc


def parse_import_text(text: str, filename: str) -> MotorParseResult | RocketParseResult:
    """按扩展名（大小写不敏感）分派到具体解析器。"""
    # 子模块在函数内导入：包初始化时先定义共享模型与错误类，解析器模块再回引它们，
    # 避免形成模块级循环导入。
    from aeroforge.data.parsers import eng, ork, rse

    suffix = PurePath(filename).suffix.lower()
    if suffix == ".eng":
        return eng.parse_eng(text)
    if suffix == ".rse":
        return rse.parse_rse(text)
    if suffix == ".ork":
        return ork.parse_ork(text)
    raise UnsupportedFormatError(filename)
