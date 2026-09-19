"""测试隔离门禁：所有可写根必须落在会话临时区内。

由来（教训 E 类：夹具把产物写到临时区之外）
------------------------------------------
M1 期间 ``conftest`` 曾把数据根设为临时目录**本身**，而
``artifacts_root() = data_root().parent / "artifacts"``，于是产物落到
``%TEMP%\\artifacts`` 并**跨会话留存**。症状是"第二次跑测试时 ``cache_hit``
意外为 True"——看起来像产品缺陷，实际是夹具对路径语义的误解。

这个错误当时只被"看懂 ``parent`` 语义"消除，没有任何回归防护，
故在此把"写盘根必须留在临时区内"变成断言。
"""

from __future__ import annotations

from pathlib import Path

from aeroforge.paths import (
    ENV_DATA_DIR,
    artifacts_root,
    contours_root,
    data_root,
    ensure_dir,
)


def test_data_root_resolves_env_override(monkeypatch, tmp_path: Path) -> None:
    """``data_root()`` 对环境变量覆盖值必须返回**规范形**（resolve）。

    为什么值得单独钉住：GitHub Actions 的 Windows runner 里 ``%TEMP%`` 是 8.3
    短名（``C:\\Users\\RUNNER~1\\...``），而隔离夹具与隔离门禁都依赖
    "夹具写入的路径 == data_root() 返回的路径"。后者一旦不再 resolve，同一物理
    目录就有两种字符串形态，三个隔离门禁会在 CI 上集体误报——本机（用户名
    ``29659`` 无短名差异）永远复现不了。这里用 ``..`` 拼出一条"字面不同、
    物理相同"的路径，在**任何**环境都能等价复现该分叉。
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv(ENV_DATA_DIR, str(data_dir / ".." / "data"))
    assert data_root() == data_dir.resolve()


def test_writable_roots_live_inside_session_tmp(isolated_data_dir: Path) -> None:
    """三个根都必须落在会话临时区之内，否则会污染开发机、跨会话留存。"""
    tmp_root = isolated_data_dir.parent
    roots = {
        "data_root": data_root(),
        "artifacts_root": artifacts_root(),
        "contours_root": contours_root(),
    }
    escaped = {name: path for name, path in roots.items() if not path.is_relative_to(tmp_root)}
    assert not escaped, f"以下可写根逃出了会话临时区 {tmp_root}：{escaped}"


def test_artifacts_and_data_share_one_parent(isolated_data_dir: Path) -> None:
    """产物与数据必须同根——否则临时目录清理时只清掉一半，残留物会改变下次运行的结论。"""
    assert artifacts_root().parent == data_root().parent
    assert artifacts_root().parent == isolated_data_dir.parent


def test_ensure_dir_keeps_writes_inside_tmp(isolated_data_dir: Path) -> None:
    """路径判定之外再验证一次真实建目录的结果，防止"路径看着对、实际写别处"。"""
    created = ensure_dir(contours_root())
    assert created.is_relative_to(isolated_data_dir.parent)
    assert created.is_dir()
