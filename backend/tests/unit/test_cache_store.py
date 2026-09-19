"""内容寻址缓存：键稳定性、原子写入、命中判据（规格 §9.2 / §16.3）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aeroforge.cache.store import (
    ARTIFACT_LOD1,
    ARTIFACT_METRICS,
    ARTIFACT_PROVENANCE,
    ArtifactStore,
    StagedArtifacts,
    build_manifest,
    compute_key,
)
from aeroforge.geometry.meridian import LineSegment, MeridianProfile


def _profile(length: float = 3.0) -> MeridianProfile:
    return MeridianProfile(
        name="cyl", base_radius=1.0, segments=(LineSegment(length=length, end_radius=1.0),)
    )


def test_key_is_stable_and_sensitive_to_geometry() -> None:
    """同几何同键、异几何异键——缓存正确性的全部依据。"""
    assert compute_key(_profile()).key == compute_key(_profile()).key
    assert compute_key(_profile()).key != compute_key(_profile(length=3.5)).key


def test_key_includes_spec_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """规格版本变化必须使全部缓存失效（几何语义已变，旧产物不再有效）。"""
    before = compute_key(_profile()).key
    monkeypatch.setattr("aeroforge.cache.store.SPEC_VERSION", "0.0.0-test")
    after = compute_key(_profile()).key

    assert before != after


def test_key_includes_kernel_version(monkeypatch: pytest.MonkeyPatch) -> None:
    before = compute_key(_profile()).key
    monkeypatch.setattr("aeroforge.cache.store.kernel_version", lambda: "occt=0.0.0")
    after = compute_key(_profile()).key

    assert before != after


def test_separator_prevents_key_collision() -> None:
    """键分量之间必须有分隔符，否则不同组合会拼出同一串（键碰撞）。"""
    key = compute_key(_profile())
    assert "\x00" not in key.key  # 摘要本身不含分隔符
    assert key.profile_hash != key.key


def test_is_cached_requires_both_metrics_and_provenance(tmp_path: Path) -> None:
    """只检查目录存在会把写到一半的产物当成命中（§9.2 的收益建立在命中即可返回之上）。"""
    store = ArtifactStore(tmp_path)
    key = compute_key(_profile()).key

    assert not store.is_cached(key)

    (tmp_path / key).mkdir()
    (tmp_path / key / ARTIFACT_METRICS).write_text("{}", encoding="utf-8")
    assert not store.is_cached(key)

    (tmp_path / key / ARTIFACT_PROVENANCE).write_text("{}", encoding="utf-8")
    assert store.is_cached(key)


def test_staging_commit_moves_atomically(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    cache_key = compute_key(_profile())
    manifest = build_manifest(cache_key)

    with store.stage(cache_key.key) as staged:
        assert not store.is_cached(cache_key.key)  # 提交前对读者不可见
        staged.write_json(ARTIFACT_METRICS, {"volume": 1.0})
        staged.register(ARTIFACT_LOD1).write_bytes(b"glTF")
        staged.commit(manifest)

    assert store.is_cached(cache_key.key)
    assert store.load_metrics(cache_key.key) == {"volume": 1.0}

    loaded = store.load_manifest(cache_key.key)
    assert loaded is not None
    assert loaded.key == cache_key.key
    assert set(loaded.files) == {ARTIFACT_METRICS, ARTIFACT_LOD1}
    assert not list(tmp_path.glob(".staging-*"))


def test_staging_abort_leaves_nothing(tmp_path: Path) -> None:
    """取消/失败路径：未 commit 就离开作用域必须自动清理（§9.1 硬规则 4）。"""
    store = ArtifactStore(tmp_path)
    key = compute_key(_profile()).key

    with pytest.raises(RuntimeError), store.stage(key) as staged:
        staged.write_json(ARTIFACT_METRICS, {"volume": 1.0})
        raise RuntimeError("作业失败")

    assert not store.is_cached(key)
    assert not list(tmp_path.glob(".staging-*"))
    assert not (tmp_path / key).exists()


def test_rejects_non_whitelisted_artifact_name(tmp_path: Path) -> None:
    """产物逻辑名走白名单——避免路径穿越与意外暴露临时文件。"""
    staged = StagedArtifacts(ArtifactStore(tmp_path), "deadbeef")

    with pytest.raises(ValueError, match="不允许的产物逻辑名"):
        staged.write_bytes("../escape", b"x")

    staged.abort()


def test_file_path_rejects_unknown_name(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)

    assert store.file_path("a" * 64, "secrets.txt") is None
