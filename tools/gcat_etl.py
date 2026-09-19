"""GCAT ETL CLI（规格 §7.5 点名的 ``tools/gcat_etl.py``，离线运行）。

    uv run python tools/gcat_etl.py --out data/snapshots/gcat-2026Q3

逻辑本体在 ``aeroforge.data.etl``；行数与 manifest 不一致时非零退出。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aeroforge.data.etl import run_etl


def main() -> int:
    parser = argparse.ArgumentParser(description="GCAT 快照 → records.parquet（§7.5）")
    parser.add_argument("--out", type=Path, required=True, help="快照目录（gcat-<年>Q<1-4>）")
    args = parser.parse_args()

    stats = run_etl(args.out)
    for table in stats.rows:
        print(f"{table}: {stats.rows[table]} 行（unit_uncertain {stats.uncertain[table]}）")
    print(f"合计 {stats.total_records} 行 → {args.out / 'records.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
