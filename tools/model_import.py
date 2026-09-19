"""模型导入 CLI（规格 §7.5 / §15：``.eng`` / ``.rse`` / ``.ork`` → 解析摘要）。

    uv run python tools/model_import.py testdata/ref_models/sample_solid.eng

薄壳：逻辑本体在 ``aeroforge.data.parsers``（纯解析，不在此复制）。解析失败
（R-21）把错误信息（含行号/XPath）与修复建议打到 stderr 并以非零码退出，
绝不静默吞掉。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aeroforge.data.parsers import (
    MotorParseResult,
    RocketParseResult,
    decode_import_bytes,
    parse_import_text,
)
from aeroforge.errors import AeroForgeError


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；返回进程退出码（0 成功，1 失败）。"""
    parser = argparse.ArgumentParser(description=".eng/.rse/.ork 模型导入器（§7.5 / R-21）")
    parser.add_argument("file", type=Path, help="待导入的发动机/火箭文件")
    args = parser.parse_args(argv)

    try:
        text = decode_import_bytes(args.file.read_bytes(), args.file.name)
        result = parse_import_text(text, args.file.name)
    except AeroForgeError as exc:
        print(f"导入失败：{exc.message}", file=sys.stderr)
        print(f"建议：{exc.suggestion}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"导入失败：无法读取 {args.file}：{exc}", file=sys.stderr)
        print("建议：确认文件路径存在且可读", file=sys.stderr)
        return 1

    if isinstance(result, MotorParseResult):
        _print_motor(result)
    else:
        _print_rocket(result)
    return 0


def _print_motor(result: MotorParseResult) -> None:
    """打印发动机解析摘要（数值一律 SI，字段名带单位后缀同 API 契约）。"""
    print(f"格式: {result.format}（derived={result.derived}）")
    print(
        f"名称: {result.name}  制造商: {result.manufacturer or '-'}  "
        f"类型: {result.motor_type or '-'}  延时: {','.join(result.delays) or '-'}"
    )
    print(f"总冲: {result.total_impulse_ns:.3f} N·s（曲线积分）")
    print(
        f"燃烧时间: {result.burn_time_s:.3f} s | 平均推力: {result.avg_thrust_n:.3f} N | "
        f"峰值推力: {result.peak_thrust_n:.3f} N"
    )
    if result.total_mass_kg is not None:
        print(
            f"总质量: {result.total_mass_kg:.4f} kg | "
            f"推进剂质量: {(result.propellant_mass_kg or 0.0):.4f} kg"
        )
    print(f"数据点: {len(result.thrust_curve)} 个")
    for note in result.unit_conversions:
        print(f"单位换算: {note}")
    _print_warnings(result.warnings)


def _print_rocket(result: RocketParseResult) -> None:
    """打印火箭解析摘要（一切字段均为 derived，不保证全字段保真）。"""
    print(f"格式: {result.format}（derived={result.derived} —— 不保证全字段保真）")
    print(f"火箭名: {result.name}  级数: {result.stage_count}")
    for index, stage in enumerate(result.stages, start=1):
        parts = [f"[{index}] {stage.name}"]
        if stage.length_m is not None:
            parts.append(f"长 {stage.length_m:.3f} m")
        if stage.diameter_m is not None:
            parts.append(f"径 {stage.diameter_m:.3f} m")
        if stage.mass_kg is not None:
            parts.append(f"质量 {stage.mass_kg:.4f} kg")
        parts.append(f"发动机: {','.join(stage.engines) or '-'}")
        print("  " + " | ".join(parts))
    _print_warnings(result.warnings)


def _print_warnings(warnings: list[str]) -> None:
    print(f"警告: {len(warnings)} 条")
    for item in warnings:
        print(f"  - {item}")


if __name__ == "__main__":
    raise SystemExit(main())
