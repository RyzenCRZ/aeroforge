"""field_report.md 生成（规格 §7.1 快照规范：字段缺失率、异常值、单位疑点）。

这是**描述性报告**，不做任何修正——修正是 ETL（§7.5）的事，报告只负责把
"这份原始数据长什么样"摊开给下一道工序。生成是幂等的：同一快照重跑得到
同一份报告（fetched_at 取自 manifest，不取当前时刻）。
"""

from __future__ import annotations

from pathlib import Path

from aeroforge.data.snapshot import SNAPSHOT_TABLES, load_manifest, read_table

#: 缺失记号：GCAT 用 ``-`` / ``..`` 等表示"该字段无值"。``?`` 不算缺失
#: （是"值存在但未经证实"），它会作为非数值样本出现在单位疑点里。
MISSING_TOKENS = {"", "-", "..", "..."}

#: §7.1 登记的条数（写规格时点的官方计数）。快照实测与它的差值只登记、不判失败：
#: 目录是活的，数字会随 release 演进——但偏离过大（翻倍级）就该停下来查口径。
EXPECTED_RECORDS = {"lv": 1817, "engines": 1462, "stages": 1596}

#: 预期数值列（按 GCAT 实际表头名，抓取 gcat-2026Q3 后核对；只对存在的列生效）。
#: 每个数值列旁都有一个同源 Flag 列（LFlag/MFlag/…）标记数值可信度——那是 §7.6
#: 质量标签的天然来源。单位口径甄别在 ETL 逐字段声明（§7.5），这里只翻"长得不像数"的值。
NUMERIC_COLUMNS: dict[str, tuple[str, ...]] = {
    "lv": ("Length", "Diameter", "Launch_Mass", "LEO_Capacity", "GTO_Capacity", "TO_Thrust"),
    "stages": ("Length", "Diameter", "Launch_Mass", "Dry_Mass", "Thrust", "ThrustSL", "Duration"),
    "engines": ("Mass", "Impulse", "Thrust", "Isp", "Duration"),
}

#: 报告里每列最多展示的疑点样本数——样本是为了人工甄别，不是数据 dump。
MAX_SAMPLES = 5


def _numeric_clean(value: str) -> str:
    """GCAT 数值列常见的干扰：千分位逗号、尾部 '?'（未经证实标记）。"""
    return value.replace(",", "").rstrip("?").strip()


def _is_numeric(value: str) -> bool:
    try:
        float(_numeric_clean(value))
    except ValueError:
        return False
    return True


def _missing_count(values: list[str]) -> int:
    return sum(1 for value in values if value.strip() in MISSING_TOKENS)


def _suspects(values: list[str]) -> list[str]:
    return [
        value for value in values if value.strip() not in MISSING_TOKENS and not _is_numeric(value)
    ]


def build_report(snapshot_dir: Path) -> str:
    manifest = load_manifest(snapshot_dir)
    records = {record.table: record for record in manifest.tables}

    lines: list[str] = [
        "# GCAT 快照 field_report",
        "",
        f"- 快照目录：`{snapshot_dir.name}`",
        f"- GCAT release：{manifest.gcat_release or '（未登记）'}",
        f"- 抓取时间：{manifest.fetched_at}",
        f"- 总记录数：{manifest.total_records}",
        "",
    ]

    for table, filename in SNAPSHOT_TABLES.items():
        path = snapshot_dir / filename
        lines.append(f"## {table}（{filename}）")
        if not path.is_file():
            lines.append("⚠ 文件缺失（与 manifest 不符，先跑 verify_snapshot）。")
            continue

        text = path.read_text(encoding="utf-8", errors="replace")
        header, rows = read_table(text)
        record = records[table]
        lines.append("")
        lines.append(f"- 记录数：**{record.records}**")
        expected = EXPECTED_RECORDS.get(table)
        if expected is not None:
            lines.append(f"- 规格 §7.1 登记值：{expected}（差 {record.records - expected:+d}）")
        lines.append(f"- 列数：{len(header)}")
        lines.append("")
        lines.append("| 列 | 非缺失率 | 疑点（非数值） | 样本 |")
        lines.append("|---|---|---|---|")

        numeric = set(NUMERIC_COLUMNS.get(table, ()))
        for column in header:
            values = [row.get(column, "") for row in rows]
            total = len(values)
            kept = total - _missing_count(values)
            rate = f"{kept / total:.1%}" if total else "n/a"
            if column in numeric:
                bad = _suspects(values)
                samples = "、".join(repr(value) for value in bad[:MAX_SAMPLES])
                lines.append(f"| {column} | {rate} | {len(bad)} | {samples or '—'} |")
            else:
                lines.append(f"| {column} | {rate} | — | — |")
        lines.append("")

    lines.append("> 本报告由 `tools/gcat_snapshot.py report` 生成，随快照不可变；")
    lines.append("> 单位甄别与修正属 ETL（§7.5），此处只登记现象。")
    return "\n".join(lines) + "\n"


def write_report(snapshot_dir: Path) -> Path:
    out = snapshot_dir / "field_report.md"
    out.write_text(build_report(snapshot_dir), encoding="utf-8")
    return out
