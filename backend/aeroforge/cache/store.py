"""内容寻址产物缓存（规格 §9.2 / §16.3）。

缓存键::

    key = sha256( canonical_json(profile) + kernel_version + spec_version )

M1 子集的取舍（§16.3）：``data_snapshot_hash`` 与 ``cea_table_id`` **预留未纳入**——
M1 的几何结果不依赖任何数据表，纳入只会无谓地使缓存分裂。M4 接入数据层时再加。

**写入是原子的**：先写临时目录，再整体改名到 ``artifacts/<key>``。这样并发的第二个
写入者要么看到完整产物，要么什么也看不到；绝不会读到写了一半的 GLB。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pydantic import BaseModel, Field

from aeroforge import SPEC_VERSION
from aeroforge.geometry.bundle import BoosterSummary
from aeroforge.geometry.bundle import canonical_json as booster_canonical_json
from aeroforge.geometry.meridian import MeridianProfile, canonical_json
from aeroforge.geometry.revolve import kernel_version
from aeroforge.paths import artifacts_root, ensure_dir

# 产物逻辑名（对前端与 API 稳定；改文件名不得改变这些键）
ARTIFACT_STEP = "model.step"
ARTIFACT_LOD1 = "model_lod1.glb"
ARTIFACT_LOD2 = "model_lod2.glb"
ARTIFACT_METRICS = "metrics.json"
ARTIFACT_PROVENANCE = "provenance.json"

#: 允许经 ``GET /api/artifacts/{key}/{file}`` 取出的文件名白名单。
#: 显式列举而非拼接，避免路径穿越与意外暴露临时文件。
ALLOWED_ARTIFACTS: frozenset[str] = frozenset(
    {ARTIFACT_STEP, ARTIFACT_LOD1, ARTIFACT_LOD2, ARTIFACT_METRICS, ARTIFACT_PROVENANCE}
)

#: MIME 类型；STEP 用 ``application/step``（RFC 无正式注册，业界通用写法）
ARTIFACT_MEDIA_TYPES: dict[str, str] = {
    ARTIFACT_STEP: "application/step",
    ARTIFACT_LOD1: "model/gltf-binary",
    ARTIFACT_LOD2: "model/gltf-binary",
    ARTIFACT_METRICS: "application/json",
    ARTIFACT_PROVENANCE: "application/json",
}


class CacheKey(BaseModel):
    """缓存键及其构成（构成部分一并保留，供溯源与排障）。"""

    key: str = Field(description="sha256 十六进制摘要")
    profile_hash: str = Field(description="canonical_json(profile) 的 sha256")
    kernel_version: str
    spec_version: str


class ArtifactManifest(BaseModel):
    """产物清单（落盘为 ``provenance.json`` 的主体）。"""

    key: str
    profile_hash: str
    kernel_version: str
    spec_version: str
    created_at: str = Field(description="ISO-8601 UTC 时间戳（记账用，不参与缓存键）")
    files: dict[str, str] = Field(description="逻辑名 → 文件名")
    timings_ms: dict[str, float] = Field(default_factory=dict, description="各阶段耗时（毫秒）")


def profile_hash(profile: MeridianProfile) -> str:
    """剖面 canonical JSON 的 sha256——键的第一分量。"""
    return hashlib.sha256(canonical_json(profile).encode("utf-8")).hexdigest()


def compute_key(profile: MeridianProfile, *, boosters: BoosterSummary | None = None) -> CacheKey:
    """计算完整缓存键。

    ⚠ 分隔符 ``\\x00`` 不可省略：没有它，``(a="x", b="yz")`` 与 ``(a="xy", b="z")``
    会拼出同一串而产生键碰撞。

    ⚠ 缓存纪律（§9.2，OI-36）：``boosters=None`` 时的键输入与既有实现**逐字节相同**
    ——无助推器的构建不得因 Schema 扩展而失效；带助推器时摘要的 canonical JSON
    作为独立分量参与，使不同捆绑构型不会共享同一份陈旧产物。
    """
    payload_parts = [canonical_json(profile)]
    if boosters is not None:
        payload_parts.append(booster_canonical_json(boosters))
    kernel = kernel_version()
    digest = hashlib.sha256(
        "\x00".join((*payload_parts, kernel, SPEC_VERSION)).encode("utf-8")
    ).hexdigest()
    return CacheKey(
        key=digest,
        profile_hash=profile_hash(profile),
        kernel_version=kernel,
        spec_version=SPEC_VERSION,
    )


class StagedArtifacts:
    """暂存区：把产物写进临时目录，:meth:`commit` 时整体改名。

    以上下文管理器使用；未 ``commit`` 就离开作用域会**自动清理**，
    因此作业失败或取消时不会留下半成品（规格 §9.1 硬规则 4）。
    """

    def __init__(self, store: ArtifactStore, key: str) -> None:
        self._store = store
        self._key = key
        self._dir = store.root / f".staging-{key}-{uuid.uuid4().hex[:8]}"
        ensure_dir(self._dir)
        self._files: dict[str, str] = {}
        self._committed = False

    @property
    def path(self) -> Path:
        return self._dir

    def write_bytes(self, logical_name: str, data: bytes) -> None:
        if logical_name not in ALLOWED_ARTIFACTS:
            msg = f"不允许的产物逻辑名：{logical_name}"
            raise ValueError(msg)
        target = self._dir / logical_name
        # 先写临时文件再改名：即便本进程此刻崩溃，也不会留下截断的目标文件
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
        self._files[logical_name] = logical_name

    def write_json(self, logical_name: str, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        self.write_bytes(logical_name, data)

    def adopt(self, logical_name: str, source: Path) -> None:
        """把外部已写好的文件移入暂存区（用于 OCCT 直接导出到磁盘的大文件）。"""
        if logical_name not in ALLOWED_ARTIFACTS:
            msg = f"不允许的产物逻辑名：{logical_name}"
            raise ValueError(msg)
        target = self._dir / logical_name
        source.replace(target)
        self._files[logical_name] = logical_name

    def register(self, logical_name: str) -> Path:
        """登记一个**已直接写入暂存目录**的文件，返回其路径。

        OCCT 导出接口只接受路径，因此调用方把文件直接导出到 :attr:`path`，
        再用本方法登记——避免导出后再整体读取一遍（GLB 可达数十 MB）。
        """
        if logical_name not in ALLOWED_ARTIFACTS:
            msg = f"不允许的产物逻辑名：{logical_name}"
            raise ValueError(msg)
        self._files[logical_name] = logical_name
        return self._dir / logical_name

    def commit(self, manifest: ArtifactManifest) -> Path:
        """把暂存目录整体改名为最终产物目录；已存在同名产物时丢弃本次写入。"""
        manifest.files = dict(self._files)
        payload = manifest.model_dump(mode="json")
        (self._dir / ARTIFACT_PROVENANCE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

        final = self._store.root / self._key
        try:
            self._dir.replace(final)
        except OSError:
            # 目标已存在（另一写入者先完成，或 Windows 上目录改名冲突）：本次产物等价，丢弃
            shutil.rmtree(self._dir, ignore_errors=True)
        self._committed = True
        return final

    def abort(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not self._committed:
            self.abort()


class ArtifactStore:
    """产物仓库：键计算与读写入口。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        if self._root is None:
            self._root = artifacts_root()
        return ensure_dir(self._root)

    def dir_for(self, key: str) -> Path:
        return self.root / key

    def is_cached(self, key: str) -> bool:
        """产物是否完整可用。

        判据是 ``metrics.json`` 与 ``provenance.json`` **都在**：只检查目录存在会把
        写到一半的产物当成命中（§9.2 的收益建立在"命中即可直接返回"之上）。
        """
        directory = self.dir_for(key)
        return (directory / ARTIFACT_METRICS).is_file() and (
            directory / ARTIFACT_PROVENANCE
        ).is_file()

    def load_metrics(self, key: str) -> dict[str, Any] | None:
        path = self.dir_for(key) / ARTIFACT_METRICS
        if not path.is_file():
            return None
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        return payload

    def load_manifest(self, key: str) -> ArtifactManifest | None:
        path = self.dir_for(key) / ARTIFACT_PROVENANCE
        if not path.is_file():
            return None
        try:
            return ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def file_path(self, key: str, logical_name: str) -> Path | None:
        """取产物文件的绝对路径；不在白名单或文件缺失时返回 ``None``。"""
        if logical_name not in ALLOWED_ARTIFACTS:
            return None
        path = self.dir_for(key) / logical_name
        return path if path.is_file() else None

    def stage(self, key: str) -> StagedArtifacts:
        return StagedArtifacts(self, key)


def build_manifest(
    cache_key: CacheKey,
    *,
    timings_ms: dict[str, float] | None = None,
) -> ArtifactManifest:
    """构造产物清单（``files`` 由暂存区在 commit 时回填）。"""
    return ArtifactManifest(
        key=cache_key.key,
        profile_hash=cache_key.profile_hash,
        kernel_version=cache_key.kernel_version,
        spec_version=cache_key.spec_version,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        files={},
        timings_ms=timings_ms or {},
    )
