"""PyInstaller hook：收集 NASA `cea` 的热力学数据表与原生绑定。

为什么需要本 hook（规格 §16.2）：
- `cea` 无官方 hook，需自备。
- 其数据文件（`data/thermo.lib`、`data/trans.lib`、`share/cea/*.lib`）是**按运行时路径
  读取**的，静态分析看不见；漏收则直到真正求解时才报错（且报错信息不指向打包问题）。
- 原生绑定为 `cea.lib.libcea`（`.pyd`）加 `cea.lib.cea_bindc.dll`，同样属运行时加载。
- ⚠ 该库的静默失效契约见 ADR-014：打包后仍必须过健全性门禁，不能因"能跑通"就采信结果。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

datas = collect_data_files("cea")
binaries = collect_dynamic_libs("cea")
hiddenimports = ["cea.lib.libcea"]
