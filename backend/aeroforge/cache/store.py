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
from collections.abc import Sequence
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
from aeroforge.perf.mass import MASS_MODEL_VERSION

# 产物逻辑名（对前端与 API 稳定；改文件名不得改变这些键）
ARTIFACT_STEP = "model.step"
ARTIFACT_LOD1 = "model_lod1.glb"
ARTIFACT_LOD2 = "model_lod2.glb"
ARTIFACT_METRICS = "metrics.json"
ARTIFACT_PROVENANCE = "provenance.json"
ARTIFACT_EVALUATE = "evaluate.json"
ARTIFACT_MC = "mc.json"
# 优化候选评估产物（M6 §14：优化过程必须复用缓存且每候选带 provenance）——
# 单个候选 Vehicle 的目标量（GLOW / 干重 / LEO 运力 / 约束轨道运力）缓存，
# 供 NSGA-II / 批量扫描 / 权衡研究在同参数候选上直接命中（§14 约束 1）。
ARTIFACT_OPTIMIZE = "optimize.json"

# 多格式导出产物（§5.8，M5 第三片）：几何格式落在构建键的 export/ 子目录，
# 报告类落在 report-<hash> 键的 export/ 子目录——同一逻辑名在两种键下都经
# ``GET /api/artifacts/{key}/{file}`` 取回（子目录解析见 :func:`_artifact_dir`）。
ARTIFACT_EXPORT_STEP = "export.step"
ARTIFACT_EXPORT_IGES = "export.iges"
ARTIFACT_EXPORT_STL = "export.stl"
ARTIFACT_EXPORT_GLB = "export.glb"
ARTIFACT_EXPORT_PARAMS = "export.params.json"
ARTIFACT_EXPORT_MASS = "export.mass.csv"
ARTIFACT_EXPORT_PERF = "export.perf.json"

#: 导出产物的统一子目录名（``artifacts/<key>/export/``）。
EXPORT_SUBDIR = "export"

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
        ARTIFACT_OPTIMIZE,
        ARTIFACT_EXPORT_STEP,
        ARTIFACT_EXPORT_IGES,
        ARTIFACT_EXPORT_STL,
        ARTIFACT_EXPORT_GLB,
        ARTIFACT_EXPORT_PARAMS,
        ARTIFACT_EXPORT_MASS,
        ARTIFACT_EXPORT_PERF,
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
    ARTIFACT_OPTIMIZE: "application/json",
    ARTIFACT_EXPORT_STEP: "application/step",
    ARTIFACT_EXPORT_IGES: "model/iges",
    ARTIFACT_EXPORT_STL: "model/stl",
    ARTIFACT_EXPORT_GLB: "model/gltf-binary",
    ARTIFACT_EXPORT_PARAMS: "application/json",
    ARTIFACT_EXPORT_MASS: "text/csv",
    ARTIFACT_EXPORT_PERF: "application/json",
}

#: 导出产物名集合（落在 ``export/`` 子目录；其余白名单产物在键目录根）。
EXPORT_ARTIFACTS: frozenset[str] = frozenset(
    {
        ARTIFACT_EXPORT_STEP,
        ARTIFACT_EXPORT_IGES,
        ARTIFACT_EXPORT_STL,
        ARTIFACT_EXPORT_GLB,
        ARTIFACT_EXPORT_PARAMS,
        ARTIFACT_EXPORT_MASS,
        ARTIFACT_EXPORT_PERF,
    }
)


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


def compute_vehicle_key(vehicle: Vehicle) -> CacheKey:
    """车辆形态构建（M5 装配树）的缓存键。

    与 :func:`compute_key` 同构（canonical + kernel + SPEC_VERSION，``\\x00`` 防拼接
    碰撞）；键输入 = **飞行器参数**的 canonical JSON（含 boosters / flatness /
    共底开关等几何语义字段），与剖面形态的键天然分离——两种形态同一输入各得其所，
    互不污染缓存。``profile_hash`` 字段此处承载 vehicle canonical 的 sha256
    （溯源口径一致，字段名沿用）。

    ⚠ 质量模型版本（M6 前置专项①）作为独立分量参与本键：``metrics.json`` 的
    ``assembly_tree`` 携带分部位干重账（贮箱壁/隔板/发动机/非贮箱分摊），该账
    随 :data:`aeroforge.perf.mass.MASS_MODEL_VERSION` 演进而变——GLB 字节不变时
    SPEC_VERSION 不递增（§9.2），但旧缓存命中旧质量账属陈旧产物，故以模型版本
    常量使几何产物键失效。evaluate / MC 键不纳入：其数值链消费 σ 干重账与几何
    推进剂账，两者均不随本模型变化。
    """
    vehicle_json = vehicle_canonical_json(vehicle)
    kernel = kernel_version()
    digest = hashlib.sha256(
        "\x00".join((vehicle_json, kernel, MASS_MODEL_VERSION, SPEC_VERSION)).encode("utf-8")
    ).hexdigest()
    return CacheKey(
        key=digest,
        profile_hash=hashlib.sha256(vehicle_json.encode("utf-8")).hexdigest(),
        kernel_version=kernel,
        spec_version=SPEC_VERSION,
    )


def evaluate_cache_key(vehicle: Vehicle, dv_supply: str = "anchored") -> str:
    """``POST /api/perf/evaluate`` 的缓存键（§9.2 同构：canonical + spec_version）。

    - 键输入 = 飞行器参数的 canonical JSON（键序固定、浮点定量、缺省省略——
      同一输入跨 Schema 扩展字节稳定，OI-36/OI-37 缓存纪律）+ ``SPEC_VERSION``；
      分隔符 ``\\x00`` 防拼接碰撞（同 :func:`compute_key`）。
    - ``perf-`` 前缀把性能评估缓存与几何产物键（sha256 裸十六进制）区分开——
      两类键语义不同（Vehicle 输入 vs 剖面输入），前缀使目录混用一眼可辨。
    - ⚠ 本键**不含**几何 kernel 版本：evaluate 是纯数值链（无 OCCT），几何语义
      未变时不得因无关版本递增而全量失效缓存。
    - ⚠ ``dv_supply``（M6 收官片双供给模式）**仅非缺省时进键**——缺省
      ``anchored`` 的键与历史字节逐位一致（既有缓存不失效）；``l2`` 是另一份
      数字（含弹道积分损失），与锚定结果必须分键，混键会让两种模式互相覆盖。
    """
    parts = [vehicle_canonical_json(vehicle)]
    if dv_supply != "anchored":
        parts.append(f"dv_supply={dv_supply}")
    parts.append(SPEC_VERSION)
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
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


def optimize_cache_key(
    vehicle: Vehicle,
    *,
    dv_supply: str = "anchored",
    extra_orbits: Sequence[str] = (),
) -> str:
    """优化候选评估（§14 M6）的缓存键：``perf-optimize-<sha256>``。

    与 :func:`evaluate_cache_key` 同构（canonical + SPEC_VERSION，``\\x00`` 防拼接
    碰撞；``perf-`` 前缀与几何键区分；不含几何 kernel 版本——纯数值链）。键输入
    在 Vehicle canonical 之外追加**候选评估的口径分量**：

    - ``dv_supply`` 仅非缺省时进键（同 :func:`evaluate_cache_key` 的字节稳定纪律——
      缺省 ``anchored`` 的键与历史形态一致）；
    - ``extra_orbits``（逆向设计的约束轨道）排序后进键——LEO 运力恒算并存储，
      约束轨道 ≠ LEO 时追加该行，同构型的 NSGA-II 与逆向设计共享同一份候选缓存。
    """
    parts = [vehicle_canonical_json(vehicle)]
    if dv_supply != "anchored":
        parts.append(f"dv_supply={dv_supply}")
    if extra_orbits:
        parts.append("orbits=" + ",".join(sorted(set(extra_orbits))))
    parts.append(SPEC_VERSION)
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"perf-optimize-{digest}"


def report_cache_key(vehicle: Vehicle) -> str:
    """报告类导出（§5.8：参数 JSON / 质量预算 CSV / 性能 JSON）的缓存键。

    与 :func:`evaluate_cache_key` 同构（canonical + SPEC_VERSION，不含几何
    kernel 版本——报告不走 OCCT，几何语义未变时不得因无关版本递增而失效）；
    ``report-`` 前缀把报告产物与几何构建键（sha256 裸十六进制）区分开。
    报告是**参数的衍生**：同参数 ⟹ 同报告键 ⟹ 同一份产物目录（覆盖写）。
    """
    digest = hashlib.sha256(
        "\x00".join((vehicle_canonical_json(vehicle), SPEC_VERSION)).encode("utf-8")
    ).hexdigest()
    return f"report-{digest}"


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
        """取产物文件的绝对路径；不在白名单或文件缺失时返回 ``None``。

        导出产物（§5.8）解析到 ``artifacts/<key>/export/<name>``——导出是构建
        产物的衍生，与既有构建产物同键共存但不混放（其余产物在键目录根）。
        """
        if logical_name not in ALLOWED_ARTIFACTS:
            return None
        directory = self.dir_for(key)
        if logical_name in EXPORT_ARTIFACTS:
            directory = directory / EXPORT_SUBDIR
        path = directory / logical_name
        return path if path.is_file() else None

    def export_dir(self, key: str) -> Path:
        """导出产物目录（``artifacts/<key>/export/``），不存在则创建。

        OCCT 导出接口只接受路径，几何 writer 直接写入该目录下的 ``.part``
        临时文件再原子改名（经 :meth:`register_export`，同 ``.part`` + 改名纪律）。
        """
        return ensure_dir(self.dir_for(key) / EXPORT_SUBDIR)

    def register_export(self, key: str, logical_name: str, source: Path) -> Path:
        """把 writer 已写好的 ``.part`` 文件原子改名为最终导出产物，返回目标路径。

        改名撞车（并发导出同一键）时丢弃本次——两份产物等价（内容寻址键相同）。
        """
        target = self.export_dir(key) / logical_name
        try:
            source.replace(target)
        except OSError:
            source.unlink(missing_ok=True)  # Windows 并发改名撞车：另一写入者已完成
        return target

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

    def load_optimize(self, key: str) -> dict[str, Any] | None:
        """读取优化候选评估缓存（``optimize.json``，§14）；未缓存 / 损坏返回 ``None``。

        损坏按未命中处理并重算覆盖（同 :meth:`load_evaluate` 哲学：候选评估是
        毫秒级纯数值，重算代价远低于把损坏 JSON 的 ``None`` 当成有效目标值）。
        命中语义：同一候选 Vehicle（canonical JSON 同键）的目标量直接复用，
        NSGA-II / 权衡 / 扫描 / 逆向在同参数候选上不重算（§14 约束 1）。
        """
        return self._load_json_atomic_cached(key, ARTIFACT_OPTIMIZE)

    def save_optimize(self, key: str, payload: dict[str, Any]) -> None:
        """写入优化候选评估缓存（原子写，同 :meth:`save_evaluate` 哲学）。"""
        self._save_json_atomic(key, ARTIFACT_OPTIMIZE, payload)

    def _load_json_atomic_cached(self, key: str, logical_name: str) -> dict[str, Any] | None:
        """``load_evaluate`` / ``load_mc`` / ``load_optimize`` 共用的损坏容错读取。"""
        path = self.dir_for(key) / logical_name
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
