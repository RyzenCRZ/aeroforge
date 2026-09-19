"""GCAT 快照固化（规格 §7.1）。

快照是 M3 全部产出的溯源锚点（R-10 的对策落点），三条硬规矩：

1. **许可先于抓取**：``data/snapshots/<id>/LICENSE-NOTICE.md`` 必须先于本模块的
   任何下载落盘（gcat-2026Q3 的核实结论 = CC-BY，已入库）。
2. **快照不可变**：一次抓取一个目录，落盘后只读；``verify_snapshot`` 重算每份
   文件的 sha256 对账，任何篡改都必须现形。
3. **sha256 参与缓存键**（§9.2）：``snapshot.json`` 是 manifest，不是装饰——
   下游 ETL 与 provenance（§7.8 ``data_snapshot`` 字段）都引用它。

下载器做成可注入的 seam：单测不碰网络，只喂字节。
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

#: 官方 TSV 根（LICENSE-NOTICE 的结论只覆盖官方源，镜像不采信）
GCAT_TSV_BASE = "https://planet4589.org/space/gcat/tsv/tables"
GCAT_HOME = "https://planet4589.org/space/gcat/"

#: 快照固化的表集合 = §7.3 字段映射的最小闭包：
#: ``lv`` 运载火箭 / ``stages`` 级 / ``engines`` 发动机 / ``lvs`` 箭-级关联 /
#: ``family`` 族（标签第二层）/ ``orgs`` 机构（标签第三层）。
#: 键 = 表的逻辑名（内部引用用它，与文件名解耦）；值 = 官方 TSV 文件名。
SNAPSHOT_TABLES: dict[str, str] = {
    "family": "family.tsv",
    "orgs": "orgs.tsv",
    "lv": "lv.tsv",
    "lvs": "lvs.tsv",
    "stages": "stages.tsv",
    "engines": "engines.tsv",
}

MANIFEST_NAME = "snapshot.json"

#: 快照 id 命名约定（§7.1 示例 ``gcat-2026Q3``）：季度一批，不可变。
_SNAPSHOT_ID_RE = re.compile(r"^gcat-\d{4}Q[1-4]$")

#: 下载器 seam：url → 原始字节。默认实现走 urllib；测试注入假实现。
Downloader = Callable[[str], bytes]


def _default_downloader(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "aeroforge-snapshot/0.1 (https://github.com/RyzenCRZ/aeroforge)"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return bytes(response.read())


def is_comment(line: str) -> bool:
    """GCAT TSV 的注释行（如修订戳 ``# Updated …``）。

    ⚠ 方言事实：**表头行也带 ``#`` 前缀**（``#LV_Name\\t…``），但它恒为首行——
    解析时先取首行剥 ``#`` 作表头，其余 ``#`` 行才是注释。曾因把表头当注释吃掉，
    首条数据行顶替表头导致整表错位（"没报错 ≠ 正确"的又一例）。
    """
    return line.startswith("#")


def _data_lines(text: str) -> list[str]:
    """非空有效行（行尾兼容 CRLF），保持原始顺序。"""
    return [line.strip("\r") for line in text.splitlines() if line.strip("\r")]


def count_records(text: str) -> int:
    """记录数 = 非空行 − 首行（表头）− 其余注释行。"""
    lines = _data_lines(text)
    if not lines:
        return 0
    return sum(1 for line in lines[1:] if not is_comment(line))


def read_table(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """把 TSV 文本读成 ``(表头, 行字典列表)``。列数不齐的行按空串补齐。

    刻意用标准库 csv 而不引 pandas：本函数只服务快照核对与 field_report，
    ETL（§7.5）才轮到 DataFrame。
    """
    import csv

    lines = _data_lines(text)
    if not lines:
        return [], []
    header = next(csv.reader([lines[0].lstrip("#").strip()], delimiter="\t"))
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if is_comment(line):
            continue
        cells = next(csv.reader([line], delimiter="\t"))
        padded = cells + [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, padded, strict=False)))
    return header, rows


class TableRecord(BaseModel):
    """单张表的固化凭据。"""

    table: str
    file: str
    url: str
    sha256: str
    size_bytes: int
    records: int


class SnapshotManifest(BaseModel):
    """``snapshot.json`` 的模型——§7.1 要求字段的超集（逐表拆分 sha256/记录数）。"""

    source: str = "GCAT"
    source_url: str = GCAT_HOME
    gcat_release: str | None = None
    fetched_at: str
    tables: list[TableRecord]

    @property
    def total_records(self) -> int:
        return sum(table.records for table in self.tables)


def fetch_snapshot(
    out_dir: Path,
    release: str | None = None,
    downloader: Downloader = _default_downloader,
) -> SnapshotManifest:
    """抓取全部表 → 写入 ``out_dir`` → 落 manifest。目录已存在时逐表覆盖。

    快照 id 必须符合命名约定——这是防"抓到任意目录、事后没人知道这是哪批数据"的
    第一道闸；许可门禁（LICENSE-NOTICE 先于抓取）由调用方与入库流程保证。
    """
    if not _SNAPSHOT_ID_RE.match(out_dir.name):
        raise ValueError(f"快照目录名须为 gcat-<年>Q<1-4>（§7.1 约定），得到：{out_dir.name}")

    out_dir.mkdir(parents=True, exist_ok=True)
    tables: list[TableRecord] = []
    for table, filename in SNAPSHOT_TABLES.items():
        url = f"{GCAT_TSV_BASE}/{filename}"
        payload = downloader(url)
        (out_dir / filename).write_bytes(payload)
        tables.append(
            TableRecord(
                table=table,
                file=filename,
                url=url,
                sha256=hashlib.sha256(payload).hexdigest(),
                size_bytes=len(payload),
                records=count_records(payload.decode("utf-8", errors="replace")),
            )
        )

    manifest = SnapshotManifest(
        gcat_release=release,
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        tables=tables,
    )
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def load_manifest(out_dir: Path) -> SnapshotManifest:
    return SnapshotManifest.model_validate_json(
        (out_dir / MANIFEST_NAME).read_text(encoding="utf-8")
    )


def verify_snapshot(out_dir: Path) -> list[str]:
    """对账：重算每份文件的 sha256 与记录数，与 manifest 比对。

    返回问题清单（空 = 干净）。快照不可变（§7.1），任何不符都按错误处置，
    绝不静默重抓——重抓就等于换了数据还留着旧结论。
    """
    manifest = load_manifest(out_dir)
    problems: list[str] = []
    for record in manifest.tables:
        path = out_dir / record.file
        if not path.is_file():
            problems.append(f"{record.file}: 文件缺失")
            continue
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != record.sha256:
            problems.append(f"{record.file}: sha256 与 manifest 不符（快照被改动）")
        if count_records(payload.decode("utf-8", errors="replace")) != record.records:
            problems.append(f"{record.file}: 记录数与 manifest 不符")
    return problems
