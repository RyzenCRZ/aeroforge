"""可写数据目录解析（规格 §15 目录约定）。

与 ``desktop/resource_path.py`` 的分工：
- 那边解析**只读资源**（前端产物、CEA 数据表）——必须落在安装目录内。
- 这边解析**可写数据**（母线、内容寻址产物）——不能假设安装目录可写
  （安装到 ``C:\\Program Files`` 时不可写），故冻结态默认落到用户目录。

⚠ 与 R-34 的区别：本模块产出的路径**允许**含非 ASCII 字符——读写制品的是 Python 自身，
处理的正是 Unicode 路径。只有 NASA ``cea`` 的 C 扩展（窄字符 API）不能接受非 ASCII 路径，
而那只涉及**只读数据表**，由 ``desktop/resource_path.ascii_root()`` 在启动时把关。
不要把这两件事混为一谈而误把制品目录也限制成 ASCII。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 覆盖数据目录的环境变量。桌面壳可在冻结态显式指定（§16.2）。
ENV_DATA_DIR = "AEROFORGE_DATA_DIR"


def _source_repo_root() -> Path | None:
    """源码运行时的仓库根：自本文件上溯寻找 ``pyproject.toml``。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return None


def data_root() -> Path:
    """可写数据根目录（母线、快照索引等）。

    优先级：``AEROFORGE_DATA_DIR`` → 源码态仓库根 ``data/`` → 冻结态用户目录。
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).resolve()

    repo_root = _source_repo_root()
    if repo_root is not None:
        return repo_root / "data"

    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "AeroForge" / "data"


def artifacts_root() -> Path:
    """内容寻址产物根目录（规格 §15：与 ``data/`` 同级）。"""
    return data_root().parent / "artifacts"


def contours_root() -> Path:
    """母线剖面存放目录（规格 §16.3：``data/contours/<id>.json``）。"""
    return data_root() / "contours"


def ensure_dir(path: Path) -> Path:
    """确保目录存在并返回它。"""
    path.mkdir(parents=True, exist_ok=True)
    return path
