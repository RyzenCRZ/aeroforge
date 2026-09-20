"""几何作业执行器（规格 §9.1 / §9.3 / §16.3）。

M1 的进程模型
-------------
规格 §16.3 明确裁剪：§9.1 的进程池在 M1 降为**单个后台工作线程**，OCCT 调用在该线程内串行，
天然满足硬规则 3。四条硬规则全部保留：

1. **async handler 不得调用 OCCT**——本模块只暴露 ``submit()``（入队，微秒级）与
   ``get()``（读内存快照）；真正的建模发生在本模块的工作线程内。
2. **``TopoDS_Shape`` 不跨线程**——实体是工作线程的局部变量，离开线程前已落为 STEP/GLB 文件；
   跨线程传递的只有 :class:`JobRecord`（纯数据）与产物路径。
3. **OCCT 调用串行**——单 worker + 单队列，结构保证。
4. **可取消并清理半成品**——暂存目录在取消时整体删除（:class:`~aeroforge.cache.store.StagedArtifacts`
   的上下文管理保证，包括异常路径）。

升级触发点 = M4（Monte Carlo 与多目标优化这类真 CPU 密集作业到达时扩池），
届时只需替换本模块的执行部分，API 形状与缓存键不变。
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from aeroforge.cache.store import (
    ARTIFACT_LOD1,
    ARTIFACT_LOD2,
    ARTIFACT_METRICS,
    ARTIFACT_PROVENANCE,
    ARTIFACT_STEP,
    ArtifactStore,
    build_manifest,
    compute_key,
)
from aeroforge.errors import AeroForgeError, ErrorBody, to_error_body
from aeroforge.geometry.analytic import analyze
from aeroforge.geometry.bundle import BoosterSummary, booster_assembly, build_bundle
from aeroforge.geometry.meridian import MeridianProfile, resolve
from aeroforge.geometry.revolve import (
    LOD1_ANGULAR,
    LOD1_DEFLECTION,
    LOD2_ANGULAR,
    LOD2_DEFLECTION,
    build_solid,
    export_glb,
    export_step,
    measure,
)
from aeroforge.geometry.validate import validate_solid


class JobStatus(StrEnum):
    """§9.3 的作业生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(StrEnum):
    """§9.3 的进度阶段（M1 子集：几何链 ``meridian → solid → mesh → step → done``）。"""

    QUEUED = "queued"
    MERIDIAN = "meridian"
    SOLID = "solid"
    MESH = "mesh"
    STEP = "step"
    DONE = "done"


#: 阶段 → 进度（0~1），供前端进度条使用。
_STAGE_PROGRESS: dict[JobStage, float] = {
    JobStage.QUEUED: 0.0,
    JobStage.MERIDIAN: 0.1,
    JobStage.SOLID: 0.4,
    JobStage.MESH: 0.7,
    JobStage.STEP: 0.9,
    JobStage.DONE: 1.0,
}


class JobRecord(BaseModel):
    """作业状态快照（跨线程传递的**纯数据**，不含任何几何对象）。"""

    job_id: str
    status: JobStatus
    stage: JobStage = JobStage.QUEUED
    progress: float = 0.0
    result_key: str | None = None
    metrics: dict[str, Any] | None = None
    error: ErrorBody | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class _Cancelled(Exception):
    """内部信号：作业被取消。"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class GeometryJobRunner:
    """单 worker 几何作业执行器。"""

    def __init__(self, store: ArtifactStore | None = None, *, queue_size: int = 32) -> None:
        self.store = store or ArtifactStore()
        self._records: dict[str, JobRecord] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, MeridianProfile, BoosterSummary | None] | None] = (
            queue.Queue(maxsize=queue_size)
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopped = False
        self._thread = threading.Thread(target=self._worker, name="aeroforge-geometry", daemon=True)
        self._thread.start()

    # ── 事件循环绑定与进度广播 ──

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记录事件循环，供工作线程跨线程投递 WS 消息。未绑定时不推送 WS。"""
        self._loop = loop

    def _publish(self, record: JobRecord) -> None:
        payload = record.model_dump(mode="json")
        with self._lock:
            targets = list(self._subscribers.get(record.job_id, []))
        loop = self._loop
        for target in targets:
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(target.put_nowait, payload)

    # ── 对外接口（非阻塞，可在 async handler 内安全调用） ──

    def submit(
        self, profile: MeridianProfile, *, boosters: BoosterSummary | None = None
    ) -> JobRecord:
        """入队一个几何构建作业并立即返回 ``queued`` 快照。

        ``boosters`` 是 M4 简化捆绑摘要（OI-36）；省略 = 无助推器，构建路径与
        既有实现逐字节一致（§9.2 缓存纪律）。

        ⚠ 本方法**不做几何计算**：只有键计算（JSON + sha256，微秒级）与入队。
        """
        record = JobRecord(job_id=uuid.uuid4().hex, status=JobStatus.QUEUED, created_at=_now())
        with self._lock:
            self._records[record.job_id] = record
            self._cancels[record.job_id] = threading.Event()
        self._queue.put((record.job_id, profile, boosters))
        return record.model_copy(deep=True)

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            record = self._records.get(job_id)
        return record.model_copy(deep=True) if record else None

    def cancel(self, job_id: str) -> bool:
        """请求取消。作业不存在或已进入终态时返回 ``False``。"""
        with self._lock:
            record = self._records.get(job_id)
            event = self._cancels.get(job_id)
        if record is None or event is None or record.terminal:
            return False
        event.set()
        return True

    def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]] | None:
        """订阅某作业的进度流；作业不存在返回 ``None``。"""
        with self._lock:
            if job_id not in self._records:
                return None
            target: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
            self._subscribers.setdefault(job_id, []).append(target)
        return target

    def unsubscribe(self, job_id: str, target: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            queues = self._subscribers.get(job_id)
            if queues and target in queues:
                queues.remove(target)
                if not queues:
                    self._subscribers.pop(job_id, None)

    def shutdown(self, timeout_s: float = 10.0) -> None:
        """停止工作线程（进程退出时调用，保证无线程残留）。"""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout_s)

    # ── 工作线程 ──

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            job_id, profile, boosters = item
            try:
                self._run(job_id, profile, boosters)
            except BaseException as exc:
                self._settle_failure(job_id, exc)

    def _update(self, job_id: str, **changes: Any) -> JobRecord:
        """就地修改记录并广播，返回快照。"""
        with self._lock:
            record = self._records[job_id]
            for key, value in changes.items():
                setattr(record, key, value)
            snapshot = record.model_copy(deep=True)
        self._publish(snapshot)
        return snapshot

    def _stage(
        self, job_id: str, stage: JobStage, started: float, timings: dict[str, float]
    ) -> None:
        timings[stage.value] = round((time.perf_counter() - started) * 1000.0, 2)
        self._update(
            job_id,
            stage=stage,
            progress=_STAGE_PROGRESS[stage],
            timings_ms=dict(timings),
        )
        self._raise_if_cancelled(job_id)

    def _raise_if_cancelled(self, job_id: str) -> None:
        with self._lock:
            event = self._cancels.get(job_id)
        if event is not None and event.is_set():
            raise _Cancelled

    def _run(self, job_id: str, profile: MeridianProfile, boosters: BoosterSummary | None) -> None:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        self._update(job_id, status=JobStatus.RUNNING, started_at=_now())
        self._raise_if_cancelled(job_id)

        # ── 阶段 1：母线推算 + 解析解（纯 Python，无 OCCT） ──
        self._stage(job_id, JobStage.MERIDIAN, started, timings)
        resolved = resolve(profile)
        analytic = analyze(resolved)

        # ── 阶段 2：回转实体 + 内核量测 + §5.7 五项校验 ──
        #    权威量测与解析对照**仍取芯级整体体**（OI-36：助推器体积在 metrics 的
        #    boosters 块单独成账，不得静默并入核心体积，否则解析对照自证失效）。
        self._stage(job_id, JobStage.SOLID, started, timings)
        part = build_solid(profile)
        kernel = measure(part)
        report = validate_solid(profile, kernel, analytic)

        cache_key = compute_key(profile, boosters=boosters)
        manifest = build_manifest(cache_key)
        written: list[str] = []

        # 捆绑构型（OI-36）：STEP 与 GLB 共用同一批助推器实体，产品名 / 节点名一致；
        # 无助推器时 step_shape 即芯级整体体，路径与现状完全一致（§9.2）
        step_shape, booster_solids = booster_assembly(profile, boosters, part)

        with self.store.stage(cache_key.key) as staged:
            # ── 阶段 3：LOD 网格导出（**逐段/逐枚具名**场景图，OI-33 ② / OI-36） ──
            #    量测与解析对照一律仍取上面的芯级整体体 `part`；分段体与助推器体只
            #    用于 GLB / STEP 导出与显隐。boosters=None 时 build_bundle 原样委托
            #    build_segments——既有输入的产物字节路径不变（§9.2）。
            self._stage(job_id, JobStage.MESH, started, timings)
            segments = build_bundle(profile, boosters)
            export_glb(
                segments,
                staged.register(ARTIFACT_LOD1),
                deflection=LOD1_DEFLECTION,
                angular=LOD1_ANGULAR,
            )
            written.append(ARTIFACT_LOD1)
            export_glb(
                segments,
                staged.register(ARTIFACT_LOD2),
                deflection=LOD2_DEFLECTION,
                angular=LOD2_ANGULAR,
            )
            written.append(ARTIFACT_LOD2)

            # ── 阶段 4：STEP 导出（权威格式；§5.8 规则 4：校验未通过则拒绝） ──
            self._stage(job_id, JobStage.STEP, started, timings)
            if report.ok:
                export_step(step_shape, staged.register(ARTIFACT_STEP))
                written.append(ARTIFACT_STEP)

            metrics: dict[str, Any] = {
                "key": cache_key.key,
                "volume": kernel.volume,
                "surface_area": kernel.surface_area,
                "centroid_z": kernel.centroid_z,
                "is_valid": kernel.is_valid,
                "solid_count": kernel.solid_count,
                "bbox_size": list(kernel.bbox_size),
                "max_radius": analytic.max_radius,
                "total_length": analytic.total_length,
                "envelope": list(analytic.envelope),
                "analytic_volume": analytic.volume,
                "analytic_surface_area": analytic.surface_area,
                "volume_relative_error": (
                    abs(kernel.volume - analytic.volume) / analytic.volume
                    if analytic.volume > 0
                    else None
                ),
                "validation": report.model_dump(mode="json"),
                "artifacts": [*written, ARTIFACT_METRICS, ARTIFACT_PROVENANCE],
                "kernel_version": cache_key.kernel_version,
                "spec_version": cache_key.spec_version,
            }
            if boosters is not None and booster_solids:
                # OI-36：助推器体积单独成账——BREP 精确值（圆柱 = π r² L），不并入 volume
                volumes = [solid.volume for solid in booster_solids]
                metrics["boosters"] = {
                    "count": boosters.count,
                    "per_booster_volume_m3": volumes[0],
                    "total_booster_volume_m3": sum(volumes),
                }
            staged.write_json(ARTIFACT_METRICS, metrics)

            timings[JobStage.DONE.value] = round((time.perf_counter() - started) * 1000.0, 2)
            manifest.timings_ms = dict(timings)
            staged.commit(manifest)

        self._update(
            job_id,
            status=JobStatus.SUCCEEDED,
            stage=JobStage.DONE,
            progress=1.0,
            result_key=cache_key.key,
            metrics=metrics,
            finished_at=_now(),
            timings_ms=timings,
        )

    def _settle_failure(self, job_id: str, exc: BaseException) -> None:
        """把异常收敛为作业终态（取消是其中一种）。"""
        with self._lock:
            stage = self._records[job_id].stage
        if isinstance(exc, _Cancelled):
            error = ErrorBody(
                code="JOB_CANCELLED",
                stage=stage.value,
                message="作业已按请求取消",
                suggestion="如需重试，请重新提交构建请求",
            )
            status = JobStatus.CANCELLED
        else:
            error = _geometry_error_body(exc, stage.value)
            status = JobStatus.FAILED
        self._update(
            job_id,
            status=status,
            error=error,
            progress=1.0,
            finished_at=_now(),
        )


def _geometry_error_body(exc: BaseException, stage: str) -> ErrorBody:
    """为失败作业生成 §10.3 错误体。几何域错误给针对性建议，其余走统一兜底。"""
    if isinstance(exc, AeroForgeError):
        body = to_error_body(exc)
        return body.model_copy(update={"stage": stage})
    if isinstance(exc, (ValueError, ArithmeticError)):
        return ErrorBody(
            code="GEOMETRY_INVALID",
            stage=stage,
            message=f"{type(exc).__name__}: {exc}",
            details={"exception": type(exc).__name__},
            suggestion=(
                "请检查母线剖面的段参数：穹顶段（arc / ellipse）两端必须**恰有一端**落在轴线上"
                "（半径 0），且每段 length 为正。可先调用 POST /api/geometry/validate 定位问题。"
            ),
        )
    body = to_error_body(exc)
    return body.model_copy(update={"stage": stage})
