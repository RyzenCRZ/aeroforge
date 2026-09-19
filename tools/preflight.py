"""AeroForge 环境预检 CLI（规格 §3.2 / ADR-014 / R-29 / R-30）。

**实现不在本脚本内**：唯一实现在 ``aeroforge.selfcheck``，此处仅转调。冻结交付物
``AeroForge.exe --preflight`` 调用同一实现（§16.2「预检保留」）。详见该模块文档字符串。

用法::

    uv run python tools/preflight.py
"""

from __future__ import annotations

from aeroforge.selfcheck import main

if __name__ == "__main__":
    raise SystemExit(main())
