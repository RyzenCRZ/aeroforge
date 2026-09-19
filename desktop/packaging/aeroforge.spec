# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 配置：AeroForge 桌面端打包（ADR-015，规格 §16.2）。

构建：
    uv run pyinstaller desktop/packaging/aeroforge.spec

**必须用 onedir，禁止 onefile**（规格 §16.2）：onefile 每次启动都要把 OCCT 的
近百 MB 扩展解压到临时目录，启动慢且极易被杀毒软件拦截。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPECPATH 为 desktop/packaging，上溯两级即仓库根。
REPO_ROOT = Path(SPECPATH).parent.parent  # noqa: F821 - SPECPATH 由 PyInstaller 注入

datas = [
    # 前端构建产物。**必须保持 frontend/dist 层级**：desktop/resource_path.py 在
    # 冻结态下按 <资源根>/frontend/dist 定位，路径结构一变就找不到。
    (str(REPO_ROOT / "frontend" / "dist"), "frontend/dist"),
]

hiddenimports = [
    # uvicorn 以字符串动态导入协议与事件循环实现，静态分析看不见。
    *collect_submodules("uvicorn"),
]

# build123d 自带字体与模板资源（R-30 涉及系统字体扫描，其内置字体用于文本/浮雕特征）。
datas += collect_data_files("build123d")

a = Analysis(  # noqa: F821 - Analysis 由 PyInstaller 注入
    [str(REPO_ROOT / "desktop" / "launcher.py")],
    # 入口脚本直接运行时应能 import desktop.*；静态分析期同样需要仓库根在路径上。
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(REPO_ROOT / "desktop" / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    # 仅排除确定不会被运行时导入的开发期依赖，避免无谓增大体积。
    excludes=[
        "pytest",
        "hypothesis",
        "mypy",
        "ruff",
        "tkinter",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821 - PYZ 由 PyInstaller 注入

exe = EXE(  # noqa: F821 - EXE 由 PyInstaller 注入
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AeroForge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 会破坏 .pyd 与 OCCT 二进制，明确关闭。
    upx=False,
    # M0.5 阶段保留控制台：探针报告需可见，且便于排查后端启动失败。
    # ⚠ M7 发布改为 False（届时报告落日志文件，见规格 §16.2）。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(  # noqa: F821 - COLLECT 由 PyInstaller 注入
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AeroForge",
)
