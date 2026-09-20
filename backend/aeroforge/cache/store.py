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
from aeroforge.params.schema import Vehicle
from aeroforge.params.schema import canonical_json as vehicle_canonical_json
from aeroforge.paths import artifacts_root, ensure_dir

# 产物逻辑名（对前端与 API 稳定；改文件名不得改变这些键）
ARTIFACT_STEP = "model.step"
ARTIFACT_LOD1 = "model_lod1.glb"
ARTIFACT_LOD2 = "model_lod2.glb"
ARTIFACT_METRICS = "metrics.json"
ARTIFACT_PROVENANCE = "provenance.json"
ARTIFACT_EVALUATE = "evaluate.json"
ARTIFACT_MC = "mc.json"

#: 允许经 ``GET /api/artifacts/{key}/{file}`` 取出的文件名白名单。
#: 显式列举而非拼接，避免路径穿越与意外暴露临时文件。
ALLOWED_ARTIFACTS: frozenset[str] = frozenset(
    {
        ARTIFACT_STEP,
        ARTIFACT_LOD1,
        ARTIFACT_LOD2,
        ARTIFACT_METRICS,
        ARTIFACT_PROVENANCE,
        ARTIFACT_EVALUATE,
        ARTIFACT_MC,
    }
)

#: MIME 类型；STEP 用 ``application/step``（RFC 无正式注册，业界通用写法）
ARTIFACT_MEDIA_TYPES: dict[str, str] = {
    ARTIFACT_STEP: "application/step",
    ARTIFACT_LOD1: "model/gltf-binary",
    ARTIFACT_LOD2: "model/gltf-binary",
    ARTIFACT_METRICS: "application/json",
    ARTIFACT_PROVENANCE: "application/json",
    ARTIFACT_EVALUATE: "application/json",
    ARTIFACT_MC: "application/json",
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


def evaluate_cache_key(vehicle: Vehicle) -> str:
    """``POST /api/perf/evaluate`` 的缓存键（§9.2 同构：canonical + spec_version）。

    - 键输入 = 飞行器参数的 canonical JSON（键序固定、浮点定量、缺省省略——
      同一输入跨 Schema 扩展字节稳定，OI-36/OI-37 缓存纪律）+ ``SPEC_VERSION``；
      分隔符 ``\\x00`` 防拼接碰撞（同 :func:`compute_key`）。
    - ``perf-`` 前缀把性能评估缓存与几何产物键（sha256 裸十六进制）区分开——
      两类键语义不同（Vehicle 输入 vs 剖面输入），前缀使目录混用一眼可辨。
    - ⚠ 本键**不含**几何 kernel 版本：evaluate 是纯数值链（无 OCCT），几何语义
      未变时不得因无关版本递增而全量失效缓存。
    """
    digest = hashlib.sha256(
        "\x00".join((vehicle_canonical_json(vehicle), SPEC_VERSION)).encode("utf-8")
    ).hexdigest()
    return f"perf-evaluate-{digest}"


def mc_cache_key(vehicle: Vehicle, samples: int, seed: int) -> str:
    """``/api/uncertainty/mc``（与 evaluate 自动投递）的 MC 结果缓存键。

    与 :func:`evaluate_cache_key` 同构，追加 ``samples`` 与**实际** seed——
    同输入 + 同种子 + 同样本数 ⟹ 同一份区间（LHS 确定性采样）；evaluate 的
    自动投递用飞行器哈希派生的**确定性种子**，故重复评估同一构型直接命中，
    不重跑 10 000 样本。用户未给种子的独立触发每次随机——如实不命中。
    """
    digest = hashlib.sha256(
        "\x00".join(
            (vehicle_canonical_json(vehicle), str(samples), str(seed), SPEC_VERSION)
        ).encode("utf-8")
    ).hexdigest()
    return f"perf-mc-{digest}"


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

    def load_evaluate(self, key: str) -> dict[str, Any] | None:
        """读取性能评估缓存（``evaluate.json``）；未缓存 / 损坏返回 ``None``。

        损坏（半写截断等）按未命中处理并重算覆盖——evaluate 是毫秒级纯数值，
        重算代价远低于错误结果流入界面的代价（"没报错 ≠ 正确"）。
        """
        path = self.dir_for(key) / ARTIFACT_EVALUATE
        if not path.is_file():
            return None
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return payload

    def save_evaluate(self, key: str, payload: dict[str, Any]) -> None:
        """写入性能评估缓存：先写 ``.part`` 临时文件再原子改名（与暂存区同哲学）。

        并发的第二个写入者要么看到完整 JSON，要么什么也看不到。始终覆盖写：
        :meth:`load_evaluate` 把损坏文件按未命中处理后须能重写修复，不得被
        「已存在即跳过」挡住（否则半写截断的缓存永远修不好）。
        """
        self._save_json_atomic(key, ARTIFACT_EVALUATE, payload)

    def load_mc(self, key: str) -> dict[str, Any] | None:
        """读取 MC 结果缓存（``mc.json``）；未缓存 / 损坏返回 ``None``。

        损坏按未命中处理并重算覆盖（同 :meth:`load_evaluate` 哲学：错误区间
        流入界面的代价远高于重算一次秒级 MC 的代价）。
        """
        path = self.dir_for(key) / ARTIFACT_MC
        if not path.is_file():
            return None
        try:
            payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return payload

    def save_mc(self, key: str, payload: dict[str, Any]) -> None:
        """写入 MC 结果缓存（原子写，同 :meth:`save_evaluate` 哲学）。"""
        self._save_json_atomic(key, ARTIFACT_MC, payload)

    def _save_json_atomic(self, key: str, logical_name: str, payload: dict[str, Any]) -> None:
        """JSON 产物的原子写：``.part`` 临时文件 + 改名（并发写者不读到半写）。"""
        directory = ensure_dir(self.dir_for(key))
        target = directory / logical_name
        tmp = directory / (logical_name + ".part")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        try:
            tmp.replace(target)
        except OSError:
            # Windows 上并发改名可能撞车：另一写入者已完成等价写入，丢弃本次
            tmp.unlink(missing_ok=True)


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
