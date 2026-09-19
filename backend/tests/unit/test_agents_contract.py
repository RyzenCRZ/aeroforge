"""AGENTS.md 契约门禁：文档里的命令必须真能跑，且两份副本不允许各改各的。

由来（教训 G 类：文档与实现漂移）
--------------------------------
M1 收尾盘查时发现 ``AGENTS.md`` 里写着 ``npm run test:nfr``，而
``frontend/package.json`` 根本没有这个脚本；同一次盘查还发现两条死命令
（``uv run python -m aeroforge.geometry.validate`` —— 该模块没有 CLI；
``uv run python tools/cea_tablegen.py`` —— 该文件不存在），并且规格附录 D
与 ``AGENTS.md`` 已经各改各的、不再一致。

本文件把三件事变成机器门禁：

1. ``AGENTS.md`` 与规格附录 D **逐行一致**（附录 D 是物化源，二者必须同步）；
2. 文档里的 ``npm run <script>`` 必须存在于 ``frontend/package.json``；
3. 文档里的 ``uv run python <文件>`` / ``-m <模块>`` / ``pyinstaller <spec>``
   与 console script 必须真实存在；``-m`` 的目标还必须有 ``__main__`` 入口
   ——否则 ``python -m`` 会**静默什么都不做**（这正是当初那条死命令的形态）。
"""

from __future__ import annotations

import difflib
import importlib
import importlib.util
import json
import re
import types
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENTS_MD = REPO_ROOT / "AGENTS.md"
SPEC_MD = REPO_ROOT / "AeroForge-Spec.md"
PACKAGE_JSON = REPO_ROOT / "frontend" / "package.json"

#: console script → 可导入的模块名（用于确认工具真的装在环境里）
CONSOLE_SCRIPTS = {
    "uvicorn": "uvicorn",
    "pytest": "pytest",
    "ruff": "ruff",
    "mypy": "mypy",
    "pyinstaller": "PyInstaller",
}

_NPM_RUN = re.compile(r"npm run ([A-Za-z0-9:_-]+)")
_PY_FILE = re.compile(r"uv run python ((?!-)[^\s`\"']+\.py)")
_PY_MODULE = re.compile(r"uv run python -m ([A-Za-z_][\w.]*)")
_PYINSTALLER = re.compile(r"uv run pyinstaller ([^\s`]+)")
_CONSOLE = re.compile(r"uv run (" + "|".join(CONSOLE_SCRIPTS) + r")\b")


def _appendix_d() -> list[str]:
    """取规格附录 D 中 fenced ``markdown`` 块的内容（即 ``AGENTS.md`` 的物化源）。"""
    lines = SPEC_MD.read_text(encoding="utf-8").splitlines()
    heading = next(index for index, line in enumerate(lines) if line.startswith("## 附录 D"))
    opens = next(
        index for index in range(heading, len(lines)) if lines[index].strip() == "```markdown"
    )
    closes = next(index for index in range(opens + 1, len(lines)) if lines[index].strip() == "```")
    return lines[opens + 1 : closes]


def _significant(lines: list[str]) -> list[str]:
    """比较用的规范化：去行尾空白、丢掉空行——只关心内容与顺序。"""
    return [line.rstrip() for line in lines if line.strip()]


def _agents_text() -> str:
    return AGENTS_MD.read_text(encoding="utf-8")


def _missing(referenced: set[str], exists: Callable[[str], bool]) -> list[str]:
    """把"引用集合"与一个存在性判定函数组合成缺失清单。"""
    return sorted(token for token in referenced if not exists(token))


def test_agents_md_is_verbatim_copy_of_spec_appendix_d() -> None:
    """两份副本必须逐行一致——附录 D 是源，``AGENTS.md`` 是物化产物。"""
    agents = _significant(_agents_text().splitlines())
    appendix = _significant(_appendix_d())
    diff = "\n".join(
        difflib.unified_diff(appendix, agents, "规格附录 D", "AGENTS.md", lineterm="", n=1)
    )
    assert agents == appendix, f"AGENTS.md 与规格附录 D 已漂移，请同步后再提交：\n{diff}"


def test_every_npm_run_reference_exists() -> None:
    scripts: set[str] = set(json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["scripts"])
    referenced = set(_NPM_RUN.findall(_agents_text()))
    assert referenced, "未从 AGENTS.md 解析到任何 npm run 引用——解析规则可能已失效"
    missing = sorted(referenced - scripts)
    assert not missing, f"AGENTS.md 引用了 frontend/package.json 中不存在的脚本：{missing}"


def test_every_referenced_python_file_exists() -> None:
    referenced = set(_PY_FILE.findall(_agents_text()))
    assert referenced, "未从 AGENTS.md 解析到任何 `uv run python <文件>` 引用"
    missing = _missing(referenced, lambda token: (REPO_ROOT / token).is_file())
    assert not missing, f"AGENTS.md 引用了不存在的文件：{missing}"


def _has_entry_point(spec: importlib.machinery.ModuleSpec, module: types.ModuleType) -> bool:
    """``python -m X`` 是否真的会执行到东西。

    包：需要 ``<pkg>/__main__.py``。模块：需要 ``__main__`` 守卫——否则
    ``python -m`` 只是把模块体导入一遍然后静默退出，属"沉默失效"。
    """
    if spec.submodule_search_locations:
        package_dir = Path(next(iter(spec.submodule_search_locations)))
        return (package_dir / "__main__.py").is_file()
    source_path = getattr(module, "__file__", None)
    if source_path is None:
        return False
    return "__main__" in Path(source_path).read_text(encoding="utf-8")


def test_every_referenced_python_module_is_runnable() -> None:
    """``uv run python -m <模块>`` 若出现，必须真有 ``__main__`` 入口。

    与 :func:`test_every_referenced_python_file_exists` 不同，这里**不**断言
    引用非空：文档当前完全不含 ``-m`` 形式，空集是合法状态。但空集会被显式
    报为 skip 而不是静默通过——按本工程"未验证必须与已验证同等显式"的口径，
    一个静默通过的守卫等于没有守卫。每次会话新增 ``-m`` 命令时该测试自动
    转为实跑（这正是 M1 那条死命令的拦截点）。
    """
    referenced = set(_PY_MODULE.findall(_agents_text()))
    if not referenced:
        pytest.skip("AGENTS.md 当前未使用 `uv run python -m` 形式，无可校验目标")
    for module_name in sorted(referenced):
        spec = importlib.util.find_spec(module_name)
        assert spec is not None, f"`uv run python -m {module_name}` 指向不存在的模块"
        module = importlib.import_module(module_name)
        assert _has_entry_point(spec, module), (
            f"`uv run python -m {module_name}` 没有 __main__ 入口，执行后什么都不会发生"
            "（这类「能跑但静默无事」的命令正是当初要拦的形态）"
        )


def test_reference_regexes_still_match_their_sample_forms() -> None:
    """解析规则自检。

    上一项在空集时跳过——若解析规则本身失效，它就会**永远**跳过而无人察觉。
    故用固定样例把五条正则钉住：规则改了但样例不匹配，这里立刻响。
    """
    sample = (
        "`npm run build` ; `uv run pytest -q` ; `uv run python tools/preflight.py` ; "
        "`uv run python -m aeroforge.selfcheck` ; "
        "`uv run pyinstaller desktop/packaging/aeroforge.spec`"
    )
    assert _NPM_RUN.findall(sample) == ["build"]
    assert _PY_FILE.findall(sample) == ["tools/preflight.py"]
    assert _PY_MODULE.findall(sample) == ["aeroforge.selfcheck"]
    assert _PYINSTALLER.findall(sample) == ["desktop/packaging/aeroforge.spec"]
    assert _CONSOLE.findall(sample) == ["pytest", "pyinstaller"]


def test_every_referenced_console_script_is_installed() -> None:
    referenced = set(_CONSOLE.findall(_agents_text()))
    assert referenced, "未从 AGENTS.md 解析到任何 `uv run <工具>` 引用"
    missing = sorted(
        name for name in referenced if importlib.util.find_spec(CONSOLE_SCRIPTS[name]) is None
    )
    assert not missing, f"AGENTS.md 引用的工具未安装在环境中：{missing}"


def test_every_referenced_pyinstaller_spec_exists() -> None:
    referenced = set(_PYINSTALLER.findall(_agents_text()))
    assert referenced, "未从 AGENTS.md 解析到 `uv run pyinstaller <spec>` 引用"
    missing = _missing(referenced, lambda token: (REPO_ROOT / token).is_file())
    assert not missing, f"AGENTS.md 引用了不存在的 PyInstaller spec：{missing}"
