"""GCAT 建库 CLI（§7.7：records.parquet → SQLite ``data/aeroforge.db``）。

    uv run python tools/gcat_db.py --snapshot data/snapshots/gcat-2026Q3

逻辑本体在 ``aeroforge.data.db``；快照未跑 ETL 时以明确报错退出（指路
``tools/gcat_etl.py``），绝不静默建出空库。整体重建、原子替换——可重复执行。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from aeroforge.data.db import DB_NAME, build_database
from aeroforge.paths import data_root


def main() -> int:
    parser = argparse.ArgumentParser(description="GCAT 目录 → SQLite（§7.7 / ADR-007）")
    parser.add_argument("--snapshot", type=Path, required=True, help="快照目录（gcat-<年>Q<1-4>）")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=f"库文件路径（默认 data_root()/{DB_NAME}）",
    )
    args = parser.parse_args()

    db_path = args.db or data_root() / DB_NAME
    stats = build_database(args.snapshot, db_path)
    for table in sorted(stats.rows):
        print(f"{table}: {stats.rows[table]} 行")
    print(f"标签枚举 {stats.tags} 个，关联 {stats.associations} 条")
    print(f"快照 {stats.snapshot} → {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
