"""契约版本与规格文档版本必须一致（规格 §16.3 / ``aeroforge/__init__.py`` 的承诺）。

``SPEC_VERSION`` 参与内容寻址缓存键：它一旦与规格文档脱节，缓存就会在**语义已变**
的情况下继续命中旧产物——这是最隐蔽的一类错误（结果看起来正常，实为陈旧）。
故用测试把"改规格必须同步改常量"变成硬门禁，而不是靠人记得。
"""

from __future__ import annotations

import re
from pathlib import Path

from aeroforge import SPEC_VERSION

_SPEC_PATH = Path(__file__).resolve().parents[3] / "AeroForge-Spec.md"

#: 规格文档头部的版本行：``| 文档版本 | v0.5.0（…） |``
_VERSION_ROW = re.compile(r"^\|\s*文档版本\s*\|\s*v(\d+\.\d+\.\d+)", re.MULTILINE)


def test_spec_version_matches_document() -> None:
    match = _VERSION_ROW.search(_SPEC_PATH.read_text(encoding="utf-8"))
    assert match is not None, f"未能在 {_SPEC_PATH} 中找到「文档版本」行"

    assert match.group(1) == SPEC_VERSION, (
        f"SPEC_VERSION={SPEC_VERSION!r} 与规格文档版本 v{match.group(1)} 不一致。"
        "几何语义变更必须同时递增二者，否则缓存键会在语义已变时继续命中旧产物。"
    )
