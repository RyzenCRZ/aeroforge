"""PyInstaller hook：收集 `lib3mf` 的原生库。

为什么需要本 hook（规格 §16.2）：
- `import build123d` 会经 `build123d/mesher.py` 导入 `lib3mf`，故它必然在冻结的导入图内。
- `lib3mf/__init__.py` 用 `os.path.dirname(os.path.realpath(__file__))` 拼出 `lib3mf.dll`
  的路径，**存在性检查不过就抛 `ImportError`**；该 DLL 不在任何已收集二进制的 PE 导入表内，
  PyInstaller 的依赖扫描看不到它，hooks-contrib 亦无对应 hook —— 实测冻结后
  `import build123d` 直接崩在 `lib3mf/__init__.py:26`。
- 目标位置必须与包目录同级（`_internal/lib3mf/lib3mf.dll`）：冻结后 `__file__` 指向
  `_internal/lib3mf/__init__.pyc`，故 `collect_dynamic_libs` 的默认 destdir（包名）刚好吻合。
"""

from PyInstaller.utils.hooks import collect_dynamic_libs

binaries = collect_dynamic_libs("lib3mf")
