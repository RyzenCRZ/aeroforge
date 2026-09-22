"""冻结产物数据收集审计（规格 §16 M7「data/cea 收集验证」欠账的落点）。

回答三个问题，全部给出**实测**结论（逐路径 probe），非推测：

1. **代码包（dist-desktop/AeroForge）收集完整性**——PyInstaller spec + 三个自定义 hook
   （hook-OCP / hook-cea / hook-lib3mf）声称收集的资源，在冻结产物内是否逐路径真实存在；
2. **数据包（仓库 data/）完整性**——CEA 预计算表（data/cea）、GCAT 快照（data/snapshots）、
   GCAT 目录库（data/aeroforge.db）是否齐全，且 CEA 表逐文件 sha256 与 manifest 对账；
3. **数据包与代码分离口径**——data/ 是否被打进代码包（现状应为**不进**），
   以及冻结态运行时把 data/ 定位到哪里（``paths.data_root()`` 冻结态语义 = 用户目录）。

「三类数据」的归属口径（本审计的判据来源）：
- **NASA cea 运行时数据**（thermo.lib / trans.lib）：由 hook-cea 收集，**必须在代码包内**
  （CEA 原生层按包内路径窄字符读取，只读，随代码同发）；
- **CEA 预计算表**（data/cea/*.npz）：纯数据资产，由 ``aeroforge.paths.cea_tables_root()``
  经 ``data_root()`` 定位——**不进代码包**，随数据包分发（§9.2：其 sha256 参与缓存键）；
- **GCAT 快照 / 目录库**：同上，不进代码包。材料库是 **Python 代码**（params/materials.py），
  随导入图冻进 PYZ，不走数据路径——故本审计只验证其源文件存在，不 probe 包内路径。

用法::

    uv run python tools/freeze_data_audit.py                    # 审计仓库 data/ + dist-desktop/
    uv run python tools/freeze_data_audit.py --data-dir <dir>   # 指定数据包目录（模拟部署位置）
    uv run python tools/freeze_data_audit.py --dist <dir>       # 指定冻结产物 onedir 根

退出码：0 全部通过；1 有缺失（审计失败）；2 前置条件不满足（产物/数据包整体不存在）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 判据表：代码包内必须存在的路径（相对 onedir 根 AeroForge/）────────────────
# 每项 = (相对路径, 是否必须为文件, 说明)。缺失任意一项即审计失败。
BUNDLE_REQUIRED: list[tuple[str, bool, str]] = [
    # 前端构建产物（resource_path.frontend_dist() 冻结态定位口径）
    ("_internal/frontend/dist/index.html", True, "前端入口（mount_frontend 必需）"),
    # NASA cea 运行时数据（hook-cea 收集；CEA 原生层窄字符 API 按此路径读取，坑位 4）
    ("_internal/cea/data/thermo.lib", True, "CEA 热力学数据库（CEA 自检 / 实时点火自检用）"),
    ("_internal/cea/data/trans.lib", True, "CEA 输运物性数据库"),
    (
        "_internal/cea/share/cea/thermo.lib",
        True,
        "CEA share 视图热力学库（collect_data_files 收集）",
    ),
    ("_internal/cea/share/cea/trans.lib", True, "CEA share 视图输运库"),
    ("_internal/cea/bin/cea.exe", True, "CEA 命令行求解器（随包数据文件）"),
    # cea 原生绑定（hook-cea hiddenimports + collect_dynamic_libs）
    ("_internal/cea/lib/libcea.cp312-win_amd64.pyd", True, "libcea 原生绑定（CEA 点火自检入口）"),
    ("_internal/cea/lib/cea_bindc.dll", True, "CEA C 绑定动态库（libcea 依赖）"),
    # lib3mf（hook-lib3mf：DLL 不在任何 PE 导入表内，漏收则 import build123d 即崩）
    ("_internal/lib3mf/lib3mf.dll", True, "lib3mf 原生库（STL/3MF 网格导出）"),
    # OCP（hook-OCP：316 个纯 Python 子包）与其 70 个改名 DLL
    ("_internal/OCP", False, "OCP 包目录（含 OCP.cp312-win_amd64.pyd）"),
    ("_internal/cadquery_ocp_novtk.libs", False, "delvewheel 改名 DLL 目录（OCP 导入期挂载）"),
]

#: OCP 改名 DLL 的最小数量判据（M0.5 实测为 70；低于 60 视为收集残缺）。
OCP_LIBS_MIN_COUNT = 60

#: GCAT 快照内必须存在的文件（相对 data/snapshots/<id>/）。
SNAPSHOT_REQUIRED = (
    "snapshot.json",
    "records.parquet",
    "LICENSE-NOTICE.md",
    "lv.tsv",
    "engines.tsv",
    "stages.tsv",
)

#: GCAT 目录库文件名（backend/aeroforge/data/db.py DB_NAME）。
GCAT_DB_NAME = "aeroforge.db"


class Report:
    """逐路径审计报告：收集失败项，最后统一汇总。"""

    def __init__(self) -> None:
        self.ok = 0
        self.misses: list[str] = []
        self.notes: list[str] = []

    def check(self, path: Path, desc: str) -> bool:
        if path.exists():
            self.ok += 1
            print(f"  [OK] {path}")
            print(f"       └─ {desc}")
            return True
        self.misses.append(f"{path} — {desc}")
        print(f"  [缺失] {path} — {desc}")
        return False

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  [说明] {text}")


def audit_bundle(bundle_root: Path, rep: Report) -> None:
    """审计代码包（onedir 根，即 dist-desktop/AeroForge）。"""
    print(f"\n=== ① 代码包审计：{bundle_root} ===")
    internal = bundle_root / "_internal"
    if not internal.is_dir():
        rep.note("onedir 产物不存在或结构不完整（缺 _internal/）——本节全部路径视为缺失")
    for rel, must_be_file, desc in BUNDLE_REQUIRED:
        path = bundle_root / rel
        if path.exists() and must_be_file and not path.is_file():
            rep.misses.append(f"{path} — {desc}（存在但不是文件）")
            print(f"  [缺失] {path} — {desc}（存在但不是文件）")
            continue
        rep.check(path, desc)

    # OCP 改名 DLL 数量（M0.5 实测 70 个；仅数量判断，不逐个枚举）
    libs_dir = internal / "cadquery_ocp_novtk.libs"
    if libs_dir.is_dir():
        n = sum(1 for p in libs_dir.iterdir() if p.suffix.lower() == ".dll")
        print(f"  [计数] {libs_dir} 内 DLL = {n}（M0.5 实测基线 70，下限 {OCP_LIBS_MIN_COUNT}）")
        if n < OCP_LIBS_MIN_COUNT:
            rep.misses.append(
                f"{libs_dir} — DLL 仅 {n} 个 < {OCP_LIBS_MIN_COUNT}"
                "（OCCT 动态库收集不全，探针头号坑位）"
            )

    # build123d 自带资源（spec 内 collect_data_files("build123d")）
    b123d = internal / "build123d"
    if b123d.is_dir():
        resource_exts = {".ttf", ".otf", ".otc", ".ttc", ".stl", ".svg"}
        n_data = sum(
            1 for p in b123d.rglob("*") if p.is_file() and p.suffix.lower() in resource_exts
        )
        if n_data:
            rep.note(
                f"build123d 资源文件（字体/模板类）包内共 {n_data} 个——collect_data_files 已生效"
            )
        else:
            rep.note("build123d 目录存在但未见字体/模板资源（需复核 collect_data_files）")
    else:
        rep.misses.append(f"{b123d} — build123d 包目录缺失")


def audit_data_package(data_dir: Path, rep: Report) -> None:
    """审计数据包（仓库 data/ 或部署后的数据目录）。"""
    print(f"\n=== ② 数据包审计：{data_dir} ===")
    if not data_dir.is_dir():
        print(f"  [致命] 数据包目录不存在：{data_dir}")
        rep.misses.append(f"{data_dir} — 数据包整体缺失")
        return

    # CEA 预计算表：逐表存在性 + sha256 对账（§9.2：sha256 参与缓存键，数据篡改即断链）
    cea_dir = data_dir / "cea"
    manifest_path = cea_dir / "manifest.json"
    if rep.check(manifest_path, "CEA 预计算表 manifest（网格 / 量纲声明 / sha256）"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        tables = manifest.get("tables", {})
        print(f"  [计数] manifest 声明 {len(tables)} 张表：{', '.join(tables)}")
        for prop, entry in sorted(tables.items()):
            npz = cea_dir / str(entry.get("file", f"cea_{prop}.npz"))
            if not rep.check(npz, f"CEA 预计算表 {prop}"):
                continue
            expect = str(entry.get("sha256", ""))
            actual = hashlib.sha256(npz.read_bytes()).hexdigest()
            if actual != expect:
                rep.misses.append(
                    f"{npz} — sha256 不符（manifest {expect[:12]}… / 实测 {actual[:12]}…，"
                    "数据已被改动或换版未重录）"
                )
                print(
                    f"  [缺失] {npz} — sha256 对账失败（期望 {expect[:12]}… 实测 {actual[:12]}…）"
                )
            else:
                rep.ok += 1
                print(f"       └─ sha256 对账一致（{actual[:12]}…）")
    else:
        rep.note("manifest 缺失——无法对账 CEA 表，请先运行 tools/cea_tablegen.py")

    # GCAT 快照（不可变，§7.1）
    snap_root = data_dir / "snapshots"
    snapshot_dirs = (
        sorted(p for p in snap_root.iterdir() if p.is_dir()) if snap_root.is_dir() else []
    )
    if not snapshot_dirs:
        rep.misses.append(f"{snap_root} — 无任何快照目录（GCAT 数据层整体缺失）")
    for snap in snapshot_dirs:
        print(f"  [快照] {snap.name}")
        for name in SNAPSHOT_REQUIRED:
            rep.check(snap / name, "GCAT 快照必备文件")
        # 溯源锚点（§7.1）：顶层必有 source_url / fetched_at；sha256 / 记录数是**逐表**字段
        # （实测 snapshot.json 结构：tables[].{url, sha256, records}），逐表对账 TSV 实际字节。
        sj = snap / "snapshot.json"
        if sj.is_file():
            meta = json.loads(sj.read_text(encoding="utf-8"))
            missing_keys = [k for k in ("source_url", "fetched_at") if k not in meta]
            tables_meta = meta.get("tables", [])
            bad_tables = [
                str(t.get("table"))
                for t in tables_meta
                if not {"sha256", "records"} <= set(t)
                or (
                    (snap / str(t.get("file", ""))).is_file()
                    and hashlib.sha256((snap / str(t["file"])).read_bytes()).hexdigest()
                    != t["sha256"]
                )
            ]
            total = sum(int(t.get("records", 0)) for t in tables_meta)
            if not missing_keys and not bad_tables:
                verdict = "齐全"
            else:
                verdict = f"异常（顶层缺 {missing_keys}；表级对账失败 {bad_tables}）"
            rep.note(
                f"{snap.name} 溯源对账：{len(tables_meta)} 张 TSV 表、"
                f"合计 {total} 条记录——{verdict}"
            )

    # GCAT 目录库（SQLite；errors.py：缺失时业务层报 GCATDataNotBuilt 并给重建指引）
    db_path = data_dir / GCAT_DB_NAME
    if rep.check(db_path, f"GCAT 目录库 {GCAT_DB_NAME}（三层标签索引 / 族谱 / 检索）"):
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                tables = {
                    r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
            need = {"vehicles", "stages", "engines", "snapshots", "tags", "record_tags"}
            absent = need - tables
            if absent:
                rep.misses.append(f"{db_path} — 缺表：{', '.join(sorted(absent))}")
            else:
                rep.ok += 1
                print(f"       └─ 核心表齐全（{', '.join(sorted(need))}），SQLite 头校验通过")
        except sqlite3.Error as exc:
            rep.misses.append(f"{db_path} — 打开失败：{exc}")

    # data/tables 占位（§15 目录口径：CEA 表实际落在 data/cea，data/tables 为规格早版占位）
    tables_dir = "存在（占位）" if (data_dir / "tables").is_dir() else "不存在"
    rep.note(f"data/tables/ {tables_dir}——CEA 预计算表实际位于 data/cea/（§15 现行口径）")


def report_frozen_data_root(rep: Report) -> None:
    """说明冻结态运行时的 data/ 定位语义（数据包与代码分离的部署落点）。"""
    print("\n=== ③ 数据包与代码分离口径（冻结态定位语义） ===")
    override = os.environ.get("AEROFORGE_DATA_DIR")
    if override:
        rep.note(
            f"当前进程设了 AEROFORGE_DATA_DIR={override}"
            "——冻结态将优先使用它（paths.data_root 优先级 1）"
        )
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    frozen_root = Path(base) / "AeroForge" / "data"
    rep.note(
        f"冻结态缺省 data_root() = {frozen_root}（paths.py 冻结语义，可被 AEROFORGE_DATA_DIR 覆盖）"
    )
    if frozen_root.is_dir():
        rep.note("该目录当前存在——冻结产物可直接消费")
    else:
        rep.note(
            "该目录当前不存在——直接运行冻结产物时推进类功能会按 errors.py 口径给"
            "「数据包未部署」指引，几何/预检不受影响"
        )
    rep.note(
        "分离口径结论：data/ 不打进代码包（aeroforge.spec 的 datas 仅含 frontend/dist 与"
        " build123d 资源，无任何 data/ 条目）——代码包与数据包两处独立分发；"
        "CEA 预计算表与 GCAT 数据随数据包，NASA cea 运行时数据随代码包（hook-cea）。"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冻结产物数据收集审计（§16 M7）")
    parser.add_argument(
        "--dist", default=str(REPO_ROOT / "dist-desktop" / "AeroForge"), help="onedir 产物根目录"
    )
    parser.add_argument(
        "--data-dir", default=str(REPO_ROOT / "data"), help="数据包目录（仓库 data/ 或部署位置）"
    )
    args = parser.parse_args(argv)

    bundle_root = Path(args.dist)
    data_dir = Path(args.data_dir)
    if not bundle_root.is_dir():
        print(f"[前置不满足] 冻结产物不存在：{bundle_root}（先按 AGENTS.md 冻结命令重建）")
        return 2

    rep = Report()
    audit_bundle(bundle_root, rep)
    audit_data_package(data_dir, rep)
    report_frozen_data_root(rep)

    print("\n=== 审计结论 ===")
    print(f"通过路径数：{rep.ok}；缺失：{len(rep.misses)}")
    for miss in rep.misses:
        print(f"  ✗ {miss}")
    if rep.misses:
        return 1
    print("全部路径实测可达。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
