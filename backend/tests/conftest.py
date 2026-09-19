"""测试夹具：把可写数据目录重定向到临时目录（规格 §15 / §16.3）。

必须**重定向**而非写入仓库 ``data/``：产物目录是内容寻址的，测试若不隔离就会污染
开发机的缓存，使"新构建"与"缓存命中"两条路径随运行顺序而变，测试结论不再可信。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from aeroforge.paths import ENV_DATA_DIR


@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir() -> Iterator[Path]:
    """整个测试会话共用一个临时数据根，退出时清理。

    ⚠ 数据根必须是临时目录**内的一层子目录**（``<tmp>/data``），不能直接用 ``<tmp>``：
    ``artifacts_root()`` 取 ``data_root().parent / "artifacts"``，若数据根就是
    ``<tmp>``，产物目录会落到**系统临时目录**里（``%TEMP%\\artifacts``）并跨会话留存，
    使"新构建"与"缓存命中"两条路径随历史运行而变（实测症状：第二次跑测试时
    ``cache_hit`` 意外为 True）。
    """
    with tempfile.TemporaryDirectory(prefix="aeroforge-tests-") as tmp:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir()
        os.environ[ENV_DATA_DIR] = str(data_dir)
        from aeroforge.api import deps

        deps.reset_singletons()
        try:
            yield data_dir
        finally:
            deps.reset_singletons()
            os.environ.pop(ENV_DATA_DIR, None)
