"""资源定位：统一源码运行与冻结运行两种形态（规格 §16.2）。

PyInstaller 冻结后 ``__file__`` 的相对语义发生变化，散落的相对路径必然失效，
故所有资源访问必须经本模块解析。
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结产物中。"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读资源根目录。

    - 冻结运行：``sys._MEIPASS``（PyInstaller 解包目录，onedir 下即 exe 同级）
    - 源码运行：仓库根目录

    Returns:
        资源根目录的绝对路径。
    """
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]  # 冻结态由 PyInstaller 注入
    # 本文件位于 <repo>/desktop/resource_path.py，故上溯两级为仓库根。
    return Path(__file__).resolve().parent.parent


def frontend_dist() -> Path:
    """前端构建产物目录（``frontend/dist``）。

    冻结与源码两种形态下的相对位置一致：均挂在资源根下的 ``frontend/dist``。
    """
    return resource_root() / "frontend" / "dist"


def ascii_root() -> bool:
    """资源根路径是否能被 CEA 原生层接受（规格 §16.2 坑位 4）。

    NASA ``cea`` 的 C 扩展用窄字符 API 打开数据表（``_internal/cea/data/thermo.lib``），
    路径含非 ASCII 字符时**必然失败**，且报错只说"文件找不到"——文件其实在。
    实测对照：同一份数据表放 ``C:\\ProgramData\\...`` 通过、放 ``D:\\测试 目录\\...`` 失败。

    Returns:
        ``True`` 表示路径安全；``False`` 表示必须拒绝启动并提示换目录。
    """
    try:
        str(resource_root()).encode("ascii")
    except UnicodeEncodeError:
        return False
    return True
