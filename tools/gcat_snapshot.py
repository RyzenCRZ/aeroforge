"""GCAT 快照 CLI（规格 §7.1 / §7.5：抓取与固化是**离线一次性动作**，不在服务端与 CI 内）。

    uv run python tools/gcat_snapshot.py fetch --out data/snapshots/gcat-2026Q3 --release 1.8.7
    uv run python tools/gcat_snapshot.py verify --out data/snapshots/gcat-2026Q3
    uv run python tools/gcat_snapshot.py report --out data/snapshots/gcat-2026Q3

逻辑本体在 ``aeroforge.data``（可测、mypy 覆盖）；本文件只做参数解析与退出码。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aeroforge.data.field_report import write_report
from aeroforge.data.snapshot import fetch_snapshot, verify_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="GCAT 快照：抓取 / 对账 / 字段报告")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_out(target: argparse.ArgumentParser) -> None:
        target.add_argument("--out", type=Path, required=True, help="快照目录（gcat-<年>Q<1-4>）")

    p_fetch = sub.add_parser("fetch", help="抓取全部表并落 manifest（许可核实须已落档）")
    add_out(p_fetch)
    p_fetch.add_argument("--release", default=None, help="GCAT release 号，如 1.8.7")

    p_verify = sub.add_parser("verify", help="重算 sha256 与记录数，对账 manifest")
    add_out(p_verify)

    p_report = sub.add_parser("report", help="生成 field_report.md（缺失率 / 单位疑点）")
    add_out(p_report)

    args = parser.parse_args()
    if args.command == "fetch":
        manifest = fetch_snapshot(args.out, release=args.release)
        print(f"已固化 {len(manifest.tables)} 张表，共 {manifest.total_records} 条记录")
        return 0
    if args.command == "verify":
        problems = verify_snapshot(args.out)
        if problems:
            for problem in problems:
                print(f"不一致：{problem}")
            return 1
        print("对账通过：全部文件与 manifest 一致")
        return 0
    report = write_report(args.out)
    print(f"已生成 {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
