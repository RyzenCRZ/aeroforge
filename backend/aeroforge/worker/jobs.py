"""作业执行器（规格 §9.1 / §9.3 / §16.3）——M4 起为**两级执行器**。

两级结构（§9.1「M4 扩池」的兑现）
----------------------------------
- **几何侧**（:class:`GeometryJobRunner`）：OCCT 构建作业保持**专用单线程**
  （硬规则 3：内核内串行，避免 OCCT 内部状态竞争）——与 M1 形态一致。
- **计算侧**（:class:`ComputeJobRunner`）：MC / 数值作业跑 ``ProcessPoolExecutor``
  （``min(4, cpu_count)`` 进程）——纯 Python 数值循环不释放 GIL，线程池无加速；
  进程池把样本块分派到独立进程，事件循环与几何线程都不被独占。工作函数是
  模块级纯函数（:func:`aeroforge.perf.mc.evaluate_chunk`），参数为 JSON 字符串
  与数值行——**可 pickle、不跨进程传几何对象**（硬规则 2 的计算侧同构）。

共享**作业簿**（:class:`JobBoard`）：记录 / 取消事件 / WS 订阅 / 进度广播的
线程安全簿记，两级执行器各持同一份——于是 ``GET /api/jobs/{id}`` 与
``/ws/jobs/{id}`` **无需改动**即可同时服务几何与计算作业。

四条硬规则（§9.1）逐条保持
---------------------------
1. **async handler 内禁止 OCCT / >10 ms CPU**——两级执行器都只暴露 ``submit()``
   （入队 + 键计算，微秒级）；重活全部在执行线程 / 进程池。
2. **几何对象不跨进程**——几何线程内实体落盘后才出线程；计算侧只传参数 JSON
   与数值（MC 的 evaluate 链路本身不碰 OCCT）。
3. **OCCT 串行**——几何侧单线程 + 单队列，结构保证（未动）。
4. **可取消 + 清理半成品**——取消事件在块边界生效；MC 结果只在**全部样本完成
   后**才原子落盘（``mc.json`` 的 ``.part`` + 改名），取消即无半成品。

进程池生命周期：模块级惰性单例（首个计算作业时创建），随进程退出收线——
不随 TestClient 的 lifespan 逐测试重建（Windows spawn 每次几秒，逐测试重建
会把测试时间烧在进程导入上）；执行器停机只收协调线程，池内空闲 worker 无状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar

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
    compute_vehicle_key,
    mc_cache_key,
)
from aeroforge.errors import AeroForgeError, ErrorBody, to_error_body
from aeroforge.geometry.analytic import analyze
from aeroforge.geometry.assembly import build_assembly
from aeroforge.geometry.bundle import BoosterSummary, booster_assembly, build_bundle
from aeroforge.geometry.exportmod import run_export
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
from aeroforge.optimize import OptimizeCancelled
from aeroforge.optimize.inverse import InverseRequest, run_inverse
from aeroforge.optimize.nsga2 import Nsga2Request, run_nsga2
from aeroforge.optimize.sweep import SweepRequest, run_sweep
from aeroforge.optimize.trade_study import TradeStudyRequest, run_trade_study
from aeroforge.params.schema import Vehicle
from aeroforge.perf import mc as mc_module
from aeroforge.perf.mc import DEFAULT_SAMPLES, MAX_SAMPLES, MIN_SAMPLES, MCCancelled


class JobStatus(StrEnum):
    """§9.3 的作业生命周期状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobStage(StrEnum):
    """§9.3 的进度阶段。

    几何链 ``meridian → solid → mesh → step → done``；MC 计算链
    ``sampling → evaluating → summarizing → done``（M4 第四片补入，前端按字符串
    消费、新增枚举值为增量变更）；导出链 ``solid → export → done``（M5 第三片，
    §5.8——报告类导出无几何构建，直接 ``export``）；L2 弹道积分链
    ``integrating → done``（M6 第二片，§8.6——单发积分无分阶段，仅 running/done）。
    """

    QUEUED = "queued"
    MERIDIAN = "meridian"
    SOLID = "solid"
    MESH = "mesh"
    STEP = "step"
    EXPORT = "export"
    SAMPLING = "sampling"
    EVALUATING = "evaluating"
    SUMMARIZING = "summarizing"
    INTEGRATING = "integrating"
    OPTIMIZING = "optimizing"
    DONE = "done"


#: 阶段 → 进度（0~1），供前端进度条使用。
_STAGE_PROGRESS: dict[JobStage, float] = {
    JobStage.QUEUED: 0.0,
    JobStage.MERIDIAN: 0.1,
    JobStage.SOLID: 0.4,
    JobStage.MESH: 0.7,
    JobStage.STEP: 0.9,
    JobStage.EXPORT: 0.9,
    JobStage.SAMPLING: 0.05,
    JobStage.EVALUATING: 0.1,
    JobStage.SUMMARIZING: 0.95,
    JobStage.INTEGRATING: 0.1,
    JobStage.OPTIMIZING: 0.1,
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


# ---------------------------------------------------------------------------
# 作业簿（两级执行器共用的线程安全簿记）
# ---------------------------------------------------------------------------


class JobBoard:
    """作业记录 / 取消事件 / WS 订阅 / 进度广播的共享簿记。

    从 M1 单执行器里抽出（**纯重构**，几何执行器行为不变）：两级执行器各持
    同一份簿，``GET /api/jobs/{id}`` / WS / cancel 因此天然覆盖全部作业类型。
    """

    def __init__(self) -> None:
        self._records: dict[str, JobRecord] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._subscribers: dict[str, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """记录事件循环，供工作线程跨线程投递 WS 消息。未绑定时不推送 WS。"""
        self._loop = loop

    def publish(self, record: JobRecord) -> None:
        payload = record.model_dump(mode="json")
        with self._lock:
            targets = list(self._subscribers.get(record.job_id, []))
        loop = self._loop
        for target in targets:
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(target.put_nowait, payload)

    def create(self) -> JobRecord:
        """登记一个新作业（QUEUED），返回可安全外传的深拷贝快照。"""
        record = JobRecord(job_id=uuid.uuid4().hex, status=JobStatus.QUEUED, created_at=_now())
        with self._lock:
            self._records[record.job_id] = record
            self._cancels[record.job_id] = threading.Event()
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

    def cancel_event(self, job_id: str) -> threading.Event | None:
        """该作业的取消事件（执行器轮询用）。"""
        with self._lock:
            return self._cancels.get(job_id)

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

    def update(self, job_id: str, **changes: Any) -> JobRecord:
        """就地修改记录并广播，返回快照。"""
        with self._lock:
            record = self._records[job_id]
            for key, value in changes.items():
                setattr(record, key, value)
            snapshot = record.model_copy(deep=True)
        self.publish(snapshot)
        return snapshot

    def stage_of(self, job_id: str) -> JobStage:
        with self._lock:
            return self._records[job_id].stage


# ---------------------------------------------------------------------------
# 几何侧执行器（M1 形态保持：专用单线程，OCCT 串行）
# ---------------------------------------------------------------------------


class _GeometryWork:
    """几何线程的一个工作项（构建 / 导出共用队列，§9.1 硬规则 3：OCCT 串行）。

    三种形态互斥：剖面构建（``profile``）· 车辆构建（``vehicle``）· 多格式
    导出（``vehicle`` + ``export_formats``，§5.8 / M5 第三片）。
    """

    __slots__ = ("boosters", "export_formats", "job_id", "profile", "vehicle")

    def __init__(
        self,
        job_id: str,
        *,
        profile: MeridianProfile | None = None,
        boosters: BoosterSummary | None = None,
        vehicle: Vehicle | None = None,
        export_formats: tuple[str, ...] | None = None,
    ) -> None:
        self.job_id = job_id
        self.profile = profile
        self.boosters = boosters
        self.vehicle = vehicle
        self.export_formats = export_formats


class GeometryJobRunner:
    """单 worker 几何作业执行器（OCCT 专用线程，硬规则 3）。"""

    def __init__(
        self,
        store: ArtifactStore | None = None,
        *,
        queue_size: int = 32,
        board: JobBoard | None = None,
    ) -> None:
        self.store = store or ArtifactStore()
        self.board = board if board is not None else JobBoard()
        self._queue: queue.Queue[_GeometryWork | None] = queue.Queue(maxsize=queue_size)
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(target=self._worker, name="aeroforge-geometry", daemon=True)
        self._thread.start()

    # ── 对外接口（非阻塞，可在 async handler 内安全调用） ──

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.board.bind_loop(loop)

    def submit(
        self, profile: MeridianProfile, *, boosters: BoosterSummary | None = None
    ) -> JobRecord:
        """入队一个几何构建作业（剖面形态）并立即返回 ``queued`` 快照。

        ``boosters`` 是 M4 简化捆绑摘要（OI-36）；省略 = 无助推器，构建路径与
        既有实现逐字节一致（§9.2 缓存纪律）。

        ⚠ 本方法**不做几何计算**：只有键计算（JSON + sha256，微秒级）与入队。
        """
        record = self.board.create()
        self._queue.put(_GeometryWork(record.job_id, profile=profile, boosters=boosters))
        return record

    def submit_vehicle(self, vehicle: Vehicle) -> JobRecord:
        """入队一个车辆形态构建作业（M5 九段分区装配树，§5.9 / OI-33）。

        与剖面形态并存：同一执行线程（OCCT 串行硬规则不变），键计算走
        :func:`aeroforge.cache.store.compute_vehicle_key`（vehicle canonical）。
        """
        record = self.board.create()
        self._queue.put(_GeometryWork(record.job_id, vehicle=vehicle))
        return record

    def submit_export(self, vehicle: Vehicle, formats: tuple[str, ...]) -> JobRecord:
        """入队一个多格式导出作业（§5.8 / M5 第三片）并立即返回 ``queued`` 快照。

        导出挂几何线程（OCCT 专用单线程——硬规则 3 的导出侧兑现）；验证门禁
        与产物落位见 :func:`aeroforge.geometry.exportmod.run_export`。
        """
        record = self.board.create()
        self._queue.put(_GeometryWork(record.job_id, vehicle=vehicle, export_formats=formats))
        return record

    def get(self, job_id: str) -> JobRecord | None:
        return self.board.get(job_id)

    def cancel(self, job_id: str) -> bool:
        return self.board.cancel(job_id)

    def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]] | None:
        return self.board.subscribe(job_id)

    def unsubscribe(self, job_id: str, target: asyncio.Queue[dict[str, Any]]) -> None:
        self.board.unsubscribe(job_id, target)

    def shutdown(self, timeout_s: float = 10.0) -> None:
        """停止工作线程（进程退出时调用，保证无线程残留）。"""
        with self._stop_lock:
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
            try:
                if item.export_formats is not None:
                    assert item.vehicle is not None
                    self._run_export(item.job_id, item.vehicle, item.export_formats)
                elif item.vehicle is not None:
                    self._run_vehicle(item.job_id, item.vehicle)
                else:
                    assert item.profile is not None
                    self._run(item.job_id, item.profile, item.boosters)
            except BaseException as exc:
                self._settle_failure(item.job_id, exc)

    def _stage(
        self, job_id: str, stage: JobStage, started: float, timings: dict[str, float]
    ) -> None:
        timings[stage.value] = round((time.perf_counter() - started) * 1000.0, 2)
        self.board.update(
            job_id,
            stage=stage,
            progress=_STAGE_PROGRESS[stage],
            timings_ms=dict(timings),
        )
        self._raise_if_cancelled(job_id)

    def _raise_if_cancelled(self, job_id: str) -> None:
        event = self.board.cancel_event(job_id)
        if event is not None and event.is_set():
            raise _Cancelled

    def _run(self, job_id: str, profile: MeridianProfile, boosters: BoosterSummary | None) -> None:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        self.board.update(job_id, status=JobStatus.RUNNING, started_at=_now())
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

        self.board.update(
            job_id,
            status=JobStatus.SUCCEEDED,
            stage=JobStage.DONE,
            progress=1.0,
            result_key=cache_key.key,
            metrics=metrics,
            finished_at=_now(),
            timings_ms=timings,
        )

    def _run_vehicle(self, job_id: str, vehicle: Vehicle) -> None:
        """车辆形态构建（M5 九段分区装配树）：布局 → 实体 + 校验 → GLB/STEP → metrics。

        metrics 扩展：``assembly_tree``（节点名 → 部件账目）与
        ``common_bulkhead_saving_m``（级序 → 级长缩减量）；尾翼 / 助推器体积
        单独成账（``fins`` / ``boosters`` 块），不并入 ``volume``。
        """
        started = time.perf_counter()
        timings: dict[str, float] = {}
        self.board.update(job_id, status=JobStatus.RUNNING, started_at=_now())
        self._raise_if_cancelled(job_id)

        # ── 阶段 1：九段分区布局（纯数值，无 OCCT） ──
        self._stage(job_id, JobStage.MERIDIAN, started, timings)

        # ── 阶段 2：分区实体 + §5.5 共底四校验 + 意图断言 ──
        self._stage(job_id, JobStage.SOLID, started, timings)
        assembly = build_assembly(vehicle)
        checks_ok = all(check.ok for check in assembly.checks)

        cache_key = compute_vehicle_key(vehicle)
        manifest = build_manifest(cache_key)
        written: list[str] = []

        with self.store.stage(cache_key.key) as staged:
            # ── 阶段 3：LOD 网格（分区具名节点：s<级序>-<分区> / fin-<k> / booster-<k>） ──
            self._stage(job_id, JobStage.MESH, started, timings)
            export_glb(
                assembly.root,
                staged.register(ARTIFACT_LOD1),
                deflection=LOD1_DEFLECTION,
                angular=LOD1_ANGULAR,
            )
            written.append(ARTIFACT_LOD1)
            export_glb(
                assembly.root,
                staged.register(ARTIFACT_LOD2),
                deflection=LOD2_DEFLECTION,
                angular=LOD2_ANGULAR,
            )
            written.append(ARTIFACT_LOD2)

            # ── 阶段 4：STEP（§5.8 规则 4：校验未通过则拒绝精确格式导出） ──
            self._stage(job_id, JobStage.STEP, started, timings)
            if checks_ok:
                export_step(assembly.root, staged.register(ARTIFACT_STEP))
                written.append(ARTIFACT_STEP)

            metrics: dict[str, Any] = {
                "key": cache_key.key,
                "form": "vehicle",
                "volume": assembly.volume,
                "total_length": assembly.total_length,
                "max_radius": assembly.max_radius,
                "node_count": len(assembly.nodes),
                "assembly_tree": {
                    name: node.model_dump(mode="json") for name, node in assembly.nodes.items()
                },
                "common_bulkhead_saving_m": {
                    str(index): saving for index, saving in assembly.saving_by_stage.items()
                },
                "checks": [check.model_dump(mode="json") for check in assembly.checks],
                "artifacts": [*written, ARTIFACT_METRICS, ARTIFACT_PROVENANCE],
                "kernel_version": cache_key.kernel_version,
                "spec_version": cache_key.spec_version,
            }
            if assembly.fins is not None:
                metrics["fins"] = assembly.fins
            if assembly.boosters is not None:
                metrics["boosters"] = assembly.boosters
            staged.write_json(ARTIFACT_METRICS, metrics)

            timings[JobStage.DONE.value] = round((time.perf_counter() - started) * 1000.0, 2)
            manifest.timings_ms = dict(timings)
            staged.commit(manifest)

        self.board.update(
            job_id,
            status=JobStatus.SUCCEEDED,
            stage=JobStage.DONE,
            progress=1.0,
            result_key=cache_key.key,
            metrics=metrics,
            finished_at=_now(),
            timings_ms=timings,
        )

    def _run_export(self, job_id: str, vehicle: Vehicle, formats: tuple[str, ...]) -> None:
        """多格式导出作业（§5.8 / M5 第三片）：验证门禁 → 逐格式写出 → 产物登记。

        metrics = :class:`~aeroforge.geometry.exportmod.ExportResult`（files /
        warnings / diagnostics / keys / provenance）；规则 4 拒绝经
        :class:`~aeroforge.errors.ExportValidationError` 收敛为作业失败
        （``EXPORT_VALIDATION_FAILED`` → 422，diagnostics 摘要在错误详情）。
        """
        started = time.perf_counter()
        timings: dict[str, float] = {}
        self.board.update(job_id, status=JobStatus.RUNNING, started_at=_now())
        self._raise_if_cancelled(job_id)

        # ── 阶段 1：装配 + §5.7 校验（含几何格式时；报告类纯数值） ──
        self._stage(job_id, JobStage.SOLID, started, timings)

        # ── 阶段 2：逐格式写出（exportmod 内含规则 4 门禁与产物登记） ──
        self._stage(job_id, JobStage.EXPORT, started, timings)
        result = run_export(vehicle, formats, self.store)

        # result_key：几何格式在场取构建键（与 build 车辆形态同键——导出是衍生），
        # 纯报告请求取 report- 键
        result_key = (
            result.keys["geometry"]
            if any(
                item.format not in ("params_json", "mass_csv", "perf_json") for item in result.files
            )
            else result.keys["report"]
        )
        metrics: dict[str, Any] = {
            "form": "export",
            **result.model_dump(mode="json"),
        }

        timings[JobStage.DONE.value] = round((time.perf_counter() - started) * 1000.0, 2)
        self.board.update(
            job_id,
            status=JobStatus.SUCCEEDED,
            stage=JobStage.DONE,
            progress=1.0,
            result_key=result_key,
            metrics=metrics,
            finished_at=_now(),
            timings_ms=timings,
        )

    def _settle_failure(self, job_id: str, exc: BaseException) -> None:
        """把异常收敛为作业终态（取消是其中一种）。"""
        stage = self.board.stage_of(job_id)
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
        self.board.update(
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


# ---------------------------------------------------------------------------
# 计算侧执行器（M4 扩池：MC / 数值作业 → 进程池）
# ---------------------------------------------------------------------------

#: 计算进程数（§9.1 任务口径：min(4, cpu_count)——纯 numpy/Python 数值，无 OCCT）。
COMPUTE_POOL_WORKERS = min(4, os.cpu_count() or 1)

_compute_pool: ProcessPoolExecutor | None = None
_compute_pool_lock = threading.Lock()


def compute_pool() -> ProcessPoolExecutor:
    """模块级惰性进程池单例（首个计算作业时创建；随进程退出收线）。

    不随执行器 / lifespan 重建：Windows spawn 每次创建要付秒级进程导入成本，
    而 TestClient 每个 fixture 都走一遍 lifespan——池作为进程级资源（同哲学：
    连接池），空闲 worker 无状态、不持有作业数据。
    """
    global _compute_pool
    with _compute_pool_lock:
        if _compute_pool is None:
            _compute_pool = ProcessPoolExecutor(max_workers=COMPUTE_POOL_WORKERS)
        return _compute_pool


def shutdown_compute_pool(wait: bool = True) -> None:
    """显式收池（测试 / 进程退出用；常规路径由解释器 atexit 兜底）。"""
    global _compute_pool
    with _compute_pool_lock:
        if _compute_pool is not None:
            _compute_pool.shutdown(wait=wait)
            _compute_pool = None


#: 计算侧作业消息（tagged 元组：首元素 = 作业类型 "mc" | "trajectory"，随 payload 展开；
#: mc → (job_id, vehicle_json, samples:int, seed:int)，trajectory → (job_id,
#: vehicle_json, program_json, dt_s:float)。线程边界消息按字段落型——payload 类型
#: 由 submit_* 的调用方保证，分支内不再做联合窄化）。
_ComputeWork = tuple[Any, ...]


class ComputeJobRunner:
    """MC / 计算作业执行器：协调线程分派样本块到进程池（§9.1 计算侧）。

    协调线程只做轻活（LHS 采样、块提交、numpy 统计——毫秒级），重活（逐样本
    evaluate 链路）全在进程池：事件循环不被独占（硬规则 1 的计算侧兑现）。
    """

    def __init__(self, board: JobBoard, store: ArtifactStore | None = None) -> None:
        self.board = board
        self.store = store or ArtifactStore()
        self._queue: queue.Queue[_ComputeWork | None] = queue.Queue(maxsize=64)
        self._stop_lock = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(target=self._worker, name="aeroforge-compute", daemon=True)
        self._thread.start()

    # ── 对外接口（非阻塞） ──

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.board.bind_loop(loop)

    def submit_mc(
        self, vehicle: Vehicle, *, samples: int = DEFAULT_SAMPLES, seed: int | None = None
    ) -> JobRecord:
        """入队一个 MC 作业并立即返回 ``queued`` 快照（OI-25 阶段②的载体）。

        ⚠ 只做微秒级簿记与入队——采样 / 求值 / 统计全部在协调线程与进程池。
        ``seed=None`` 时用飞行器 canonical JSON 哈希派生**确定性种子**：同一构型
        重复评估命中同一份缓存结果（§9.2），不重跑 10 000 样本。
        """
        if not (MIN_SAMPLES <= samples <= MAX_SAMPLES):
            from aeroforge.errors import PerfError

            raise PerfError(
                f"样本数 {samples} 越出可配区间 [{MIN_SAMPLES}, {MAX_SAMPLES}]（§8.7）",
                suggestion=f"样本数在 {MIN_SAMPLES}–{MAX_SAMPLES} 之间，默认 {DEFAULT_SAMPLES}",
            )
        if seed is None:
            import hashlib

            from aeroforge.params.schema import canonical_json

            digest = hashlib.sha256(canonical_json(vehicle).encode("utf-8")).digest()
            seed = int.from_bytes(digest[:4], "big")
        record = self.board.create()
        self._queue.put(("mc", record.job_id, vehicle.model_dump_json(), samples, seed))
        return record

    def submit_trajectory(
        self,
        vehicle: Vehicle,
        program_json: str,
        *,
        dt_s: float,
    ) -> JobRecord:
        """入队一个 L2 上升弹道积分作业（§8.6 L2）并立即返回 ``queued`` 快照。

        ⚠ 只做微秒级簿记与入队——积分（百毫秒级实测，F9 ≈76 ms / CZ-5 ≈153 ms
        / SV ≈272 ms，RK4 固定步长 0.1 s）在协调线程执行：超过端点同步阈值的
        计算按 §9.1 惯例走作业体系，事件循环不被独占。
        """
        record = self.board.create()
        self._queue.put(
            ("trajectory", record.job_id, vehicle.model_dump_json(), program_json, dt_s)
        )
        return record

    def submit_optimize(self, kind: str, spec_json: str) -> JobRecord:
        """入队一个优化作业（§14：nsga2 / trade-study / sweep / inverse）。

        ⚠ 只做微秒级簿记与入队——候选评估链（实测 ~0.2 ms/次，见
        :mod:`aeroforge.optimize` 模块注）在协调线程顺序执行：按 §9.1 惯例走
        作业体系（进度按代数 / 批次上报、可取消），事件循环不被独占。请求
        校验（变量 / 上限）已在 API 层同步完成，此处只做执行。
        """
        record = self.board.create()
        self._queue.put(("optimize", record.job_id, kind, spec_json))
        return record

    def get(self, job_id: str) -> JobRecord | None:
        return self.board.get(job_id)

    def cancel(self, job_id: str) -> bool:
        return self.board.cancel(job_id)

    def subscribe(self, job_id: str) -> asyncio.Queue[dict[str, Any]] | None:
        return self.board.subscribe(job_id)

    def unsubscribe(self, job_id: str, target: asyncio.Queue[dict[str, Any]]) -> None:
        self.board.unsubscribe(job_id, target)

    def shutdown(self, timeout_s: float = 10.0) -> None:
        """停止协调线程（在途作业让其收尾；池不在此收——见 :func:`compute_pool`）。"""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout_s)

    # ── 协调线程 ──

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if item[0] == "trajectory":
                _, job_id, vehicle_json, program_json, dt_s = item
                try:
                    self._execute_trajectory(job_id, vehicle_json, program_json, float(dt_s))
                except BaseException as exc:
                    self._settle_failure(job_id, exc, default_code="TRAJECTORY_FAILED")
            elif item[0] == "optimize":
                _, job_id, kind, spec_json = item
                try:
                    self._execute_optimize(job_id, str(kind), str(spec_json))
                except BaseException as exc:
                    self._settle_failure(
                        job_id, exc, default_code="OPTIMIZE_FAILED", respect_domain_code=True
                    )
            else:
                _, job_id, vehicle_json, samples, seed = item
                try:
                    self._execute_mc(job_id, vehicle_json, int(samples), int(seed))
                except BaseException as exc:
                    self._settle_failure(job_id, exc)

    def _execute_mc(self, job_id: str, vehicle_json: str, samples: int, seed: int) -> None:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        vehicle = Vehicle.model_validate_json(vehicle_json)
        cache_key = mc_cache_key(vehicle, samples, seed)

        self.board.update(job_id, status=JobStatus.RUNNING, started_at=_now())

        cached = self.store.load_mc(cache_key)
        if cached is not None:
            metrics = {**cached, "interval_pending": False, "mc_job_id": job_id}
            self.board.update(
                job_id,
                status=JobStatus.SUCCEEDED,
                stage=JobStage.DONE,
                progress=1.0,
                result_key=cache_key,
                metrics=metrics,
                finished_at=_now(),
                timings_ms={"cache_hit_ms": round((time.perf_counter() - started) * 1000.0, 2)},
            )
            return

        cancel_event = self.board.cancel_event(job_id)
        if cancel_event is not None and cancel_event.is_set():
            raise _Cancelled

        self.board.update(
            job_id, stage=JobStage.SAMPLING, progress=_STAGE_PROGRESS[JobStage.SAMPLING]
        )
        timings[JobStage.SAMPLING.value] = round((time.perf_counter() - started) * 1000.0, 2)

        self.board.update(
            job_id, stage=JobStage.EVALUATING, progress=_STAGE_PROGRESS[JobStage.EVALUATING]
        )
        evaluate_started = time.perf_counter()

        def _on_progress(done: int, total: int) -> None:
            fraction = done / total if total else 1.0
            self.board.update(
                job_id,
                stage=JobStage.EVALUATING,
                progress=_STAGE_PROGRESS[JobStage.EVALUATING] + (0.85 * fraction),
            )

        def _should_cancel() -> bool:
            event = self.board.cancel_event(job_id)
            return event is not None and event.is_set()

        result = mc_module.run_monte_carlo(
            vehicle,
            samples=samples,
            seed=seed,
            executor=compute_pool(),
            on_progress=_on_progress,
            should_cancel=_should_cancel,
        )
        timings[JobStage.EVALUATING.value] = round(
            (time.perf_counter() - evaluate_started) * 1000.0, 2
        )

        self.board.update(
            job_id, stage=JobStage.SUMMARIZING, progress=_STAGE_PROGRESS[JobStage.SUMMARIZING]
        )
        metrics = mc_module.mc_metrics_payload(result, job_id=job_id)
        # 结果只在此处（全部样本完成、统计已出）原子落盘——取消路径无半成品（规则 4）
        self.store.save_mc(cache_key, result.model_dump(mode="json"))

        timings[JobStage.DONE.value] = round((time.perf_counter() - started) * 1000.0, 2)
        self.board.update(
            job_id,
            status=JobStatus.SUCCEEDED,
            stage=JobStage.DONE,
            progress=1.0,
            result_key=cache_key,
            metrics=metrics,
            finished_at=_now(),
            timings_ms=timings,
        )

    def _execute_trajectory(
        self, job_id: str, vehicle_json: str, program_json: str, dt_s: float
    ) -> None:
        """L2 上升弹道积分（§8.6 L2）：协调线程内单发积分（百毫秒级，无分阶段）。"""
        from aeroforge.perf.trajectory import TrajectoryProgram, integrate_ascent

        started = time.perf_counter()
        vehicle = Vehicle.model_validate_json(vehicle_json)
        program = TrajectoryProgram.model_validate_json(program_json)

        self.board.update(
            job_id,
            status=JobStatus.RUNNING,
            stage=JobStage.INTEGRATING,
            progress=_STAGE_PROGRESS[JobStage.INTEGRATING],
            started_at=_now(),
        )

        result = integrate_ascent(vehicle, program, dt_s=dt_s)
        metrics = result.model_dump(mode="json")
        metrics["trajectory_job_id"] = job_id

        self.board.update(
            job_id,
            status=JobStatus.SUCCEEDED,
            stage=JobStage.DONE,
            progress=1.0,
            metrics=metrics,
            finished_at=_now(),
            timings_ms={"integrate_ms": round((time.perf_counter() - started) * 1000.0, 2)},
        )

    #: 优化作业类型 → (请求模型, 运行入口)（§14 四能力；kind 即端点路径尾段）。
    _OPTIMIZE_RUNNERS: ClassVar[dict[str, tuple[type[BaseModel], Any]]] = {
        "nsga2": (Nsga2Request, run_nsga2),
        "trade-study": (TradeStudyRequest, run_trade_study),
        "sweep": (SweepRequest, run_sweep),
        "inverse": (InverseRequest, run_inverse),
    }

    def _execute_optimize(self, job_id: str, kind: str, spec_json: str) -> None:
        """优化作业（§14）：协调线程内顺序评估，进度按代数 / 批次上报。

        请求模型与运行入口按 ``kind`` 分派（类属性 :data:`_OPTIMIZE_RUNNERS`）；
        结果载荷（pareto_front / variants / rows / config + warnings + provenance）
        原样进 ``metrics`` 下发（前端契约键，见各 run_* 模块）。
        """
        entry = self._OPTIMIZE_RUNNERS.get(kind)
        if entry is None:  # pragma: no cover - kind 由 API 层固定下发，防御分支
            raise ValueError(f"未知优化作业类型：{kind}")
        spec_model, runner = entry
        started = time.perf_counter()

        request = spec_model.model_validate_json(spec_json)
        self.board.update(
            job_id,
            status=JobStatus.RUNNING,
            stage=JobStage.OPTIMIZING,
            progress=_STAGE_PROGRESS[JobStage.OPTIMIZING],
            started_at=_now(),
        )

        def _on_progress(done: int, total: int) -> None:
            fraction = done / total if total else 1.0
            self.board.update(
                job_id,
                stage=JobStage.OPTIMIZING,
                progress=_STAGE_PROGRESS[JobStage.OPTIMIZING] + (0.85 * fraction),
            )

        def _should_cancel() -> bool:
            event = self.board.cancel_event(job_id)
            return event is not None and event.is_set()

        result = runner(
            request, store=self.store, on_progress=_on_progress, should_cancel=_should_cancel
        )
        metrics: dict[str, Any] = {**result, "optimize_job_id": job_id}

        self.board.update(
            job_id,
            status=JobStatus.SUCCEEDED,
            stage=JobStage.DONE,
            progress=1.0,
            metrics=metrics,
            finished_at=_now(),
            timings_ms={"optimize_ms": round((time.perf_counter() - started) * 1000.0, 2)},
        )

    def _settle_failure(
        self,
        job_id: str,
        exc: BaseException,
        *,
        default_code: str = "MC_FAILED",
        respect_domain_code: bool = False,
    ) -> None:
        """把异常收敛为作业终态（取消 / 域错误 / 未预期异常）。

        ``default_code`` 是作业类型对应的兜底错误码（MC 域维持既有
        ``MC_FAILED``；L2 弹道积分为 ``TRAJECTORY_FAILED``；优化为
        ``OPTIMIZE_FAILED``）。域异常自带的消息与 suggestion 原样透传（§10.3：
        可操作，不降级为"未知错误"）；``respect_domain_code=True``（优化域）时
        域异常的**错误码也保留**——``OPTIMIZE_INFEASIBLE``（逆向不硬凑）与
        ``OPTIMIZE_FAILED`` 语义不同，不得被兜底码覆盖。
        """
        stage = self.board.stage_of(job_id)
        if isinstance(exc, (_Cancelled, MCCancelled, OptimizeCancelled)):
            if isinstance(exc, OptimizeCancelled):
                message = "优化作业已按请求取消（代 / 批边界生效；结果未落盘，无半成品）"
                suggestion = "如需重试，请重新提交优化请求"
            else:
                message = "MC 作业已按请求取消（块边界生效；结果未落盘，无半成品）"
                suggestion = "如需重试，请重新提交 /api/uncertainty/mc 或再触发一次 evaluate"
            error = ErrorBody(
                code="JOB_CANCELLED",
                stage=stage.value,
                message=message,
                suggestion=suggestion,
            )
            status = JobStatus.CANCELLED
        elif isinstance(exc, AeroForgeError) and respect_domain_code:
            error = to_error_body(exc).model_copy(update={"stage": stage.value})
            status = JobStatus.FAILED
        else:
            body = to_error_body(exc)
            error = body.model_copy(update={"stage": stage.value, "code": default_code})
            status = JobStatus.FAILED
        self.board.update(
            job_id,
            status=status,
            error=error,
            progress=1.0,
            finished_at=_now(),
        )
