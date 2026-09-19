"""PyInstaller hook：收集 `cadquery-ocp-novtk`（OCP）的全部子包。

为什么需要本 hook（规格 §16.2）：
- PyInstaller 官方与 hooks-contrib **均无 OCP / cadquery 的 hook**，故必须自备。
- 该发行版把 OCCT 主体静态链接进单个约 89.4 MB 的 `OCP.cp312-win_amd64.pyd`，包目录内
  **没有任何 DLL**。真正的风险是 **316 个纯 Python 子包**：build123d 以
  `from OCP.<Sub> import ...` 分散引用，静态分析容易漏，冻结后表现为
  `ModuleNotFoundError: OCP.<Sub>`。故用 `collect_submodules` 整体收集。

⚠ 外部动态库（探针头号目标，勿据此文件误判）：OCP 的 70 个 OCCT/图像库 DLL 并不在包目录内，
而是由 delvewheel 改名后放在 **`site-packages/cadquery_ocp_novtk.libs/`**（合计约 61.9 MB），
`OCP/__init__.py` 在导入期用 `os.add_dll_directory()` 挂载。这些不在本 hook 的采集范围内——
它们由 PyInstaller 的 PE 依赖扫描（`OCP.pyd` 的导入表直接引用 5 个 `TK*.dll`，再递归解析）
收集到 `_internal/`。**唯一可信的判据是冻结产物跑 `AeroForge.exe --preflight`**（真实
`import build123d` + 几何内核自检），构建成功不等于收集完整。
"""

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# 当前发行版此处收集为空（见 docstring）；保留以兼容将来切回动态链接的发行版。
binaries = collect_dynamic_libs("OCP")
hiddenimports = collect_submodules("OCP")
