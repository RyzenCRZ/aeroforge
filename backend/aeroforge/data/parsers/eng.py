"""RASP ``.eng`` 固体发动机推力曲线解析器（规格 §7.5）。

方言要点（RASP/OpenRocket 社区惯例，无官方规范——R-21，以样例驱动）：

- ``;`` 开头为注释行；注释里的 units 指示（如 ``; units=lb,s`` 或 ``; units N,s``）
  对**其后数据行**生效：推力 lb → N 乘 4.4482216152605（N 原样），时间恒为 s。
- 首条非注释行是表头：``名称 延时(数字或P) 总冲(N·s，可带单位尾巴)
  [平均推力 燃烧时间 [总质量 推进剂质量]]``；质量按 RASP 惯例为 g，读入即 ÷1000 → kg。
- 其后每行两个数：时间（s）与推力（当前单位）。

异常口径：无法识别的行、缺表头、无数据点 → :class:`ParserError`（带行号与原文
片段）；负推力、时间非单调、首点≠0、积分总冲/末点时间与表头偏差超限 →
``warnings``（不静默、不修正原值）。
"""

from __future__ import annotations

import re
from typing import NamedTuple

from aeroforge.data.parsers import (
    MotorParseResult,
    ParserError,
    ThrustPoint,
    curve_warnings,
    trapezoid_impulse,
)

#: 1 lbf = 4.4482216152605 N（国际协议换算值，测试按此系数精确断言）。
LB_TO_N = 4.4482216152605

_THRUST_UNITS: dict[str, float] = {"n": 1.0, "lb": LB_TO_N, "lbs": LB_TO_N, "lbf": LB_TO_N}
_TIME_UNITS: dict[str, float] = {"s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0}

#: 注释里的 units 指示：``units=lb,s`` / ``units N,s`` 两种写法均可。
_UNITS_LINE_RE = re.compile(r"^;\s*units\s*=?\s*([A-Za-z]+)\s*[,;\s]\s*([A-Za-z]+)\s*$")

#: 表头总冲字段的数值前缀（其余部分视作单位尾巴）。
_NUMBER_PREFIX_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")

#: 总冲字段允许的单位尾巴（忽略大小写与空白）。
_IMPULSE_TAILS = {"", "ns", "n.s", "n-s", "n·s"}


class _Header(NamedTuple):
    """``.eng`` 表头（第 3 列起均为可选，成对出现）。"""

    lineno: int
    name: str
    delay: str
    impulse_ns: float
    burn_time_s: float | None
    total_mass_kg: float | None
    propellant_mass_kg: float | None


def parse_eng(text: str) -> MotorParseResult:
    """解析 ``.eng`` 文本 → 发动机结果（纯函数，文本进、结果出）。

    ``total_impulse_ns`` 一律取**曲线梯形积分**；表头总冲仅用于交叉校验
    （偏差 > 2% 记 warning）。燃烧时间取曲线末点时间，表头燃烧时间仅用于校验。
    """
    notes: list[str] = []  # 单位换算声明（逐条，§7.5 显式声明源单位）
    scan_warnings: list[str] = []  # 扫描期发现（行尾注释、未识别尾巴等）
    thrust_factor = 1.0
    time_factor = 1.0
    header: _Header | None = None
    header_lineno = 0
    last_lineno = 0
    points: list[ThrustPoint] = []
    refs: list[str] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        last_lineno = lineno
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            matched = _UNITS_LINE_RE.match(line)
            if matched:
                thrust_group = matched.group(1)
                time_group = matched.group(2)
                thrust_factor, time_factor = _resolve_units(thrust_group, time_group, lineno)
                notes.append(
                    f"第 {lineno} 行 units 指示：推力 {thrust_group} ×{thrust_factor!r} → N，"
                    f"时间 {time_group} → s（对其后数据行生效）"
                )
            continue
        if ";" in line:  # 行尾注释：剥离但报告，不静默
            body, _, tail = line.partition(";")
            scan_warnings.append(f"第 {lineno} 行行尾注释已忽略：{tail!r}")
            line = body.strip()
            if not line:
                continue
        if header is None:
            header = _parse_header(line, lineno, notes, scan_warnings)
            header_lineno = lineno
            continue
        t_raw, f_raw = _parse_data_row(line, raw, lineno)
        points.append(ThrustPoint(time_s=t_raw * time_factor, thrust_n=f_raw * thrust_factor))
        refs.append(f"第 {lineno} 行")

    if header is None:
        raise ParserError(
            f"第 {last_lineno} 行（文件末行）之后仍未找到有效表头行：.eng 首条非注释行"
            "应为「名称 延时 总冲 [平均推力 燃烧时间 [总质量 推进剂质量]]」"
        )
    if not points:
        raise ParserError(f"第 {header_lineno} 行表头之后没有任何推力数据点（空曲线）")

    warnings = scan_warnings + curve_warnings(
        points,
        refs,
        header_impulse_ns=header.impulse_ns,
        header_burn_time_s=header.burn_time_s,
    )
    impulse = trapezoid_impulse(points)
    burn_time = points[-1].time_s
    return MotorParseResult(
        format="eng",
        name=header.name,
        delays=[header.delay],
        total_impulse_ns=impulse,
        burn_time_s=burn_time,
        avg_thrust_n=impulse / burn_time if burn_time > 0 else 0.0,
        peak_thrust_n=max(point.thrust_n for point in points),
        propellant_mass_kg=header.propellant_mass_kg,
        total_mass_kg=header.total_mass_kg,
        thrust_curve=points,
        warnings=warnings,
        unit_conversions=notes,
    )


def _resolve_units(thrust: str, time: str, lineno: int) -> tuple[float, float]:
    """解析 units 指示的两个单位 →（推力因子，时间因子）；未知单位显式报错。"""
    thrust_factor = _THRUST_UNITS.get(thrust.lower())
    if thrust_factor is None:
        raise ParserError(
            f"第 {lineno} 行 units 指示含未识别的推力单位 {thrust!r}（支持 N / lb / lbs / lbf）"
        )
    time_factor = _TIME_UNITS.get(time.lower())
    if time_factor is None:
        raise ParserError(f"第 {lineno} 行 units 指示含未识别的时间单位 {time!r}（支持 s / sec）")
    return thrust_factor, time_factor


def _parse_header(line: str, lineno: int, notes: list[str], warnings: list[str]) -> _Header:
    """解析表头行；第 3 列起可选但必须成对，多余字段报告不中断。"""
    parts = line.split()
    if len(parts) < 3:
        raise ParserError(
            f"第 {lineno} 行表头字段不足（至少需要 名称 延时 总冲，实得 {len(parts)} 列）：{line!r}"
        )
    delay = parts[1]
    if delay.upper() != "P":
        _require_float(parts[1], lineno, "延时", line)

    match = _NUMBER_PREFIX_RE.match(parts[2])
    if match is None:
        raise ParserError(f"第 {lineno} 行表头总冲 {parts[2]!r} 无法解析为数值：{line!r}")
    impulse = float(match.group(0))
    tail = parts[2][match.end() :].strip().lower().replace(" ", "")
    if tail not in _IMPULSE_TAILS:
        warnings.append(
            f"第 {lineno} 行表头总冲带未识别的单位尾巴 {parts[2][match.end() :]!r}，按 N·s 处理"
        )

    burn_time_s: float | None = None
    total_mass_kg: float | None = None
    propellant_mass_kg: float | None = None
    if len(parts) == 4:
        raise ParserError(f"第 {lineno} 行表头的平均推力与燃烧时间必须成对出现：{line!r}")
    if len(parts) >= 5:
        _require_float(parts[3], lineno, "平均推力", line)  # 表头均值不采用（结果用曲线积分算）
        burn_time_s = _require_float(parts[4], lineno, "燃烧时间", line)
    if len(parts) == 6:
        raise ParserError(f"第 {lineno} 行表头的总质量与推进剂质量必须成对出现：{line!r}")
    if len(parts) >= 7:
        total_mass_kg = _require_float(parts[5], lineno, "总质量", line) / 1000.0
        propellant_mass_kg = _require_float(parts[6], lineno, "推进剂质量", line) / 1000.0
        notes.append(f"第 {lineno} 行表头质量按 RASP 惯例为 g，已 ÷1000 → kg")
    if len(parts) > 7:
        warnings.append(f"第 {lineno} 行表头有 {len(parts) - 7} 个未识别的多余字段：{parts[7:]!r}")
    return _Header(lineno, parts[0], delay, impulse, burn_time_s, total_mass_kg, propellant_mass_kg)


def _parse_data_row(line: str, raw: str, lineno: int) -> tuple[float, float]:
    """数据行 = 「时间 推力」两列；无法识别的行显式报错（§7.5 / R-21）。"""
    parts = line.split()
    if len(parts) != 2:
        raise ParserError(
            f"第 {lineno} 行数据行应为「时间 推力」两列，实得 {len(parts)} 列：{raw.strip()!r}"
        )
    try:
        return float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ParserError(f"第 {lineno} 行数据行存在无法解析的数值：{raw.strip()!r}") from exc


def _require_float(token: str, lineno: int, field: str, line: str) -> float:
    try:
        return float(token)
    except ValueError as exc:
        raise ParserError(f"第 {lineno} 行表头{field} {token!r} 无法解析为数值：{line!r}") from exc
