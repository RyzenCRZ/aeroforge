"""GCAT 快照固化门禁（§7.1）。全部离线：下载器注入假实现，绝不碰网络。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from aeroforge.data.field_report import build_report
from aeroforge.data.snapshot import (
    MANIFEST_NAME,
    SNAPSHOT_TABLES,
    count_records,
    fetch_snapshot,
    load_manifest,
    read_table,
    verify_snapshot,
)

#: 造一份小而真的 GCAT 形态：**表头行自带 ``#`` 前缀**（GCAT 方言）→
#: ``#`` 修订戳注释 → 数据（含空行与不齐的行）
FAKE_TSV = (
    "#LV_Name\tLV_Family\tLV_Length\n"
    "# Updated 2026 Sep 16\n"
    "Vanguard\tVanguard\t1.5\n"
    "\n"
    "Ariane\tAriane\t\n"  # 该行少一列，read_table 须补空串
    "R7\tR7\t28.0\n"
)


def fake_downloader(payloads: dict[str, bytes]) -> Callable[[str], bytes]:
    def download(url: str) -> bytes:
        for filename, payload in payloads.items():
            if url.endswith(f"/{filename}"):
                return payload
        raise AssertionError(f"未知下载地址：{url}")

    return download


@pytest.fixture()
def payload() -> dict[str, bytes]:
    return {filename: FAKE_TSV.encode("utf-8") for filename in SNAPSHOT_TABLES.values()}


def test_count_records_skips_comments_and_blank_lines() -> None:
    # 表头 1 行 + 注释 1 行（不计）+ 数据 3 行（空行不计）= 3 条记录
    assert count_records(FAKE_TSV) == 3


def test_read_table_pads_ragged_rows() -> None:
    header, rows = read_table(FAKE_TSV)
    assert header == ["LV_Name", "LV_Family", "LV_Length"]
    assert len(rows) == 3
    assert rows[1]["LV_Length"] == ""  # 少列补空串，不抛 IndexError
    assert rows[1]["LV_Name"] == "Ariane"


def test_fetch_writes_files_and_manifest(tmp_path: Path, payload: dict[str, bytes]) -> None:
    out = tmp_path / "gcat-2026Q3"
    manifest = fetch_snapshot(out, release="1.8.7", downloader=fake_downloader(payload))

    assert manifest.gcat_release == "1.8.7"
    assert manifest.total_records == 3 * len(SNAPSHOT_TABLES)
    assert len(manifest.tables) == len(SNAPSHOT_TABLES)
    for record in manifest.tables:
        raw = (out / record.file).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record.sha256
        assert record.records == 3
    stored = json.loads((out / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert stored["tables"][0]["sha256"] == manifest.tables[0].sha256
    assert verify_snapshot(out) == []


def test_fetch_rejects_nonconforming_snapshot_id(tmp_path: Path, payload: dict[str, bytes]) -> None:
    with pytest.raises(ValueError, match="gcat-"):
        fetch_snapshot(tmp_path / "my-data", downloader=fake_downloader(payload))


def test_verify_detects_tampering(tmp_path: Path, payload: dict[str, bytes]) -> None:
    out = tmp_path / "gcat-2026Q3"
    fetch_snapshot(out, downloader=fake_downloader(payload))

    victim = out / "lv.tsv"
    victim.write_bytes(victim.read_bytes() + b"Tampered\tX\t9\n")
    problems = verify_snapshot(out)
    assert any("lv.tsv" in problem and "sha256" in problem for problem in problems)
    assert any("lv.tsv" in problem and "记录数" in problem for problem in problems)

    victim.write_bytes(payload["lv.tsv"])  # 还原后必须复归干净
    assert verify_snapshot(out) == []


def test_manifest_roundtrip(tmp_path: Path, payload: dict[str, bytes]) -> None:
    out = tmp_path / "gcat-2026Q3"
    manifest = fetch_snapshot(out, downloader=fake_downloader(payload))
    assert load_manifest(out) == manifest


def test_field_report_missing_rates_and_suspects(tmp_path: Path, payload: dict[str, bytes]) -> None:
    out = tmp_path / "gcat-2026Q3"
    fetch_snapshot(out, release="1.8.7", downloader=fake_downloader(payload))

    report = build_report(out)
    # LV_Length：3 行中 1 行缺失 → 非缺失率 2/3；R7 一行是合法数值 → 疑点 0
    assert "非缺失率" in report
    assert "66.7%" in report
    assert "gcat-2026Q3" in report
    assert "1.8.7" in report
    # 报告是幂等的：重跑逐字节一致（fetched_at 取自 manifest）
    assert build_report(out) == report


def test_numeric_suspects_surface_unit_smells(tmp_path: Path) -> None:
    """单位疑点列：千分位逗号与 '?' 尾标不算数值异常，真正的英文单位混入必须现形。"""
    from aeroforge.data.field_report import _is_numeric

    assert _is_numeric("1,234")
    assert _is_numeric("28.0?")
    assert not _is_numeric("140 klb")  # 英制混入（§7.3 "需核验是否混入英制"）
    assert not _is_numeric("?")
