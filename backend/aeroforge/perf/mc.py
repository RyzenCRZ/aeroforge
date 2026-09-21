"""Monte Carlo 不确定度（规格 §8.7 / §10.1，M4 计算内核第四片）。

方法与纪律
----------
- **LHS**（拉丁超立方采样，§8.7）：``scipy.stats.qmc.LatinHypercube``，默认
  10 000 样本（可配 1 000–100 000），显著降低单纯随机采样的方差。
- **分布表**（§8.7 逐行）：实际 Isp 正态 ±2%；结构系数 σ 正态 ±15%（截断到
  物理域 (0, 0.9)，越界截断计数入 provenance）；损失预算正态 ±20%（各轨道
  ΔV 需求的非理想含量整体缩放，份额 ≥ 0 截断）；效率因子——**不另设扰动**：
  Schema 比冲已是实际值（QA-1，含效率），再设 η 会与 Isp 扰动双计。
- **每样本完整 evaluate 点值链路**（§8.7 硬纪律）：``vehicle_ledger``（几何
  解析质量账）+ 四轨道 ``payload_for_dv_on_ledger`` 二分 + GLOW——**禁止另写
  第二套简化物理**；每样本毫秒级，10 000 样本秒级（§8.7 预期）。
- **偏度**（§8.7 末 / OI-01）：``adjusted_sample`` 口径
  ``G₁ = √(n(n−1))/(n−2) · m₃/m₂^{3/2}``；``n<3`` 或 ``m₂=0`` → ``null`` 并附
  原因；``|G₁|>0.5`` 时 ``mean_vs_p50_note`` 提示「均值 ≠ P50」。
- **敏感度**：一阶差分（每参数 ±1σ 重算点值；**非 Sobol**，provenance 注记），
  影响 = ±1σ 两方向中较大的 |Δ运力|/运力，按最大值归一，Top-8。
- **种子**：请求可带 seed（复现性）；未给则随机生成并把实际值记入 provenance。
  同 seed 两跑结果**逐字节一致**（统计全为确定性 numpy 运算，结果不含时钟）。

执行形态（OI-25 两阶段契约的阶段②）
------------------------------------
本模块只做**计算**，不碰作业簿记：worker 侧由
:class:`aeroforge.worker.jobs.ComputeJobRunner` 驱动（§9.1 两级执行器的计算侧
进程池），``executor=None`` 时逐块同步执行（单进程形态，供测试与轻量调用）。
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from aeroforge.errors import PerfError
from aeroforge.params.schema import Vehicle
from aeroforge.perf.capacity import (
    PAYLOAD_ORBITS,
    OrbitPayload,
    payload_by_orbit,
    payload_for_dv_on_ledger,
    vehicle_ledger,
)
from aeroforge.perf.losses import DEFAULT_LAUNCH_SITE, ideal_orbit_dv_km_s

#: 默认样本数（§8.7：10 000，LHS）。
DEFAULT_SAMPLES = 10_000

#: 样本数可配区间（§8.7：1 000–100 000，越界拒绝）。
MIN_SAMPLES = 1_000
MAX_SAMPLES = 100_000

#: 直方图 bin 数（固定 50）。
HISTOGRAM_BINS = 50

#: 收敛诊断检查点（前 1k / 2k / 5k / 10k 的 P50 漂移；超过样本数的检查点跳过）。
CONVERGENCE_CHECKPOINTS: tuple[int, ...] = (1_000, 2_000, 5_000, 10_000)

#: 分布表（§8.7）：实际 Isp 正态 ±2% / σ 正态 ±15% / 损失预算正态 ±20%。
ISP_REL_SIGMA = 0.02
SIGMA_REL_SIGMA = 0.15
LOSS_REL_SIGMA = 0.20

#: σ 的物理域（§8.7：截断到 (0, 0.9)；下界取 0.001 防 0 值破坏 σ/(1−σ) 账）。
SIGMA_DOMAIN: tuple[float, float] = (0.001, 0.9)

#: 偏度显著偏离 0 的判据（|G₁| > 0.5 → 「均值 ≠ P50」提示，§8.7 末）。
SKEWNESS_NOTE_THRESHOLD = 0.5

#: 敏感度输出条数上限（Top-8）。
SENSITIVITY_TOP_N = 8

#: GLOW 区间的量键（与四轨道键 ``leo_kg`` 等同层）。
GLOW_KEY = "glow_kg"

#: 进程池在途块上限（池大小 = min(4, cpu)，两倍在途保流水线不断）。
_MAX_INFLIGHT_CHUNKS = 8

#: 分布表文案（provenance 用，单一来源防两份漂移）。
DISTRIBUTION_TABLE_NOTE = (
    "实际 Isp 正态 ±2%；结构系数 σ 正态 ±15%（截断到物理域 (0, 0.9)，"
    "越界样本截断并计数）；损失预算正态 ±20%（各轨道 ΔV 需求的非理想含量 "
    "(dv_used − ideal) 整体缩放，份额 ≥ 0 截断）；效率因子不另设扰动——"
    "Schema 比冲已是实际值（QA-1 含效率），另设 η 会与 Isp 双计（§8.7）"
)

#: 偏度口径文案（§8.7：必须同时声明 n 与 estimator，两种口径数值不同）。
SKEWNESS_ESTIMATOR_NOTE = (
    "adjusted_sample（G₁ = √(n(n−1))/(n−2)·m₃/m₂^{3/2}，与 Excel SKEW / "
    "pandas Series.skew 默认一致；n<3 或 m₂=0 → null 并附原因，§8.7）"
)


class MCCancelled(Exception):
    """内部信号：MC 作业被取消（由执行器捕获后收敛为作业终态）。"""


# ---------------------------------------------------------------------------
# 扰动空间（维度布局——协调进程与工作进程共用，须可 pickle）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerturbDim:
    """一个扰动维度（LHS 的一维）：类型 + 敏感度参数路径 + 作用目标。

    ``target``：``("core", 级号)`` / ``("booster", 组序)`` / ``None``（全局量）。
    值口径随 ``kind`` 变化：``isp`` → 乘子；``sigma`` → 截断后的绝对 σ；
    ``loss`` → 损失缩放偏移 δ（≥ −1）。
    """

    kind: Literal["isp", "sigma", "loss"]
    path: str
    target: tuple[str, int] | None


def perturbation_space(vehicle: Vehicle) -> tuple[PerturbDim, ...]:
    """从飞行器构造扰动维度：逐芯级 Isp/σ + 逐助推器组 Isp/σ + 全局损失预算。"""
    dims: list[PerturbDim] = []
    for position, stage in enumerate(vehicle.stages):
        dims.append(PerturbDim("isp", f"stages[{position}].isp_vacuum_s", ("core", stage.index)))
        dims.append(
            PerturbDim("sigma", f"stages[{position}].structure_coefficient", ("core", stage.index))
        )
    for group in range(len(vehicle.boosters)):
        dims.append(PerturbDim("isp", f"boosters[{group}].stage.isp_vacuum_s", ("booster", group)))
        dims.append(
            PerturbDim(
                "sigma", f"boosters[{group}].stage.structure_coefficient", ("booster", group)
            )
        )
    dims.append(PerturbDim("loss", "loss_budget", None))
    return tuple(dims)


def _quantity_key(orbit: str) -> str:
    """轨道名 → 区间量键（LEO → leo_kg；§8.7 契约形态）。"""
    return orbit.lower() + "_kg"


def _sigma_of(vehicle: Vehicle, target: tuple[str, int]) -> float:
    if target[0] == "core":
        for stage in vehicle.stages:
            if stage.index == target[1]:
                return stage.structure_coefficient
    else:
        booster = vehicle.boosters[target[1]]
        return booster.stage.structure_coefficient
    raise PerfError(  # pragma: no cover - 维度由同一 vehicle 构造，目标必存在
        f"扰动目标 {target} 不存在于飞行器",
        suggestion="这是实现缺陷，请提交 issue 附复现参数",
    )


# ---------------------------------------------------------------------------
# 采样（LHS + 分布变换 + 截断；协调进程侧，scipy 惰性导入）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SampleDraw:
    """一次 LHS 采样的产出：样本值矩阵 + 实际种子 + 截断计数。"""

    values: np.ndarray
    """(samples × dims) 的扰动值矩阵（行序即样本序，统计与复现的锚）。"""

    seed: int
    truncated: dict[str, int]
    """逐维截断计数（σ 越出 (0, 0.9) / 损失 δ < −1 的样本数）。"""


def draw_samples(
    vehicle: Vehicle, dims: Sequence[PerturbDim], samples: int, seed: int | None
) -> SampleDraw:
    """LHS + §8.7 分布表变换（正态经标准正态逆 CDF；σ 截断到物理域并计数）。"""
    # scipy 惰性导入：工作进程只跑 _evaluate_chunk（纯数值），不付 scipy.stats 的导入成本
    from scipy.special import ndtri

    sampler_seed = (
        seed if seed is not None else int.from_bytes(os.urandom(4), "little")
    )  # 未给种子 → 随机生成并如实记录（复现性靠 provenance 里的实际值）
    from scipy.stats import qmc

    sampler = qmc.LatinHypercube(d=len(dims), seed=sampler_seed)
    unit = sampler.random(samples)
    values = np.empty_like(unit)
    truncated: dict[str, int] = {}
    for column, dim in enumerate(dims):
        z = ndtri(np.clip(unit[:, column], 1e-12, 1.0 - 1e-12))
        if dim.kind == "isp":
            values[:, column] = 1.0 + ISP_REL_SIGMA * z
        elif dim.kind == "sigma":
            nominal = _sigma_of(vehicle, dim.target)  # type: ignore[arg-type]  # loss 维不进此分支
            raw = nominal * (1.0 + SIGMA_REL_SIGMA * z)
            clipped = np.clip(raw, *SIGMA_DOMAIN)
            count = int(np.count_nonzero(clipped != raw))
            if count:
                truncated[dim.path] = count
            values[:, column] = clipped
        else:
            delta = LOSS_REL_SIGMA * z
            clipped = np.maximum(delta, -1.0)  # 损失份额 ≥ 0 ⟺ δ ≥ −1
            count = int(np.count_nonzero(clipped != delta))
            if count:
                truncated[dim.path] = count
            values[:, column] = clipped
    return SampleDraw(values=values, seed=sampler_seed, truncated=truncated)


def nominal_row(vehicle: Vehicle, dims: Sequence[PerturbDim]) -> list[float]:
    """无扰动基准行（敏感度的 nominal 参照）：Isp 乘 1、σ 取标称、δ=0。"""
    row: list[float] = []
    for dim in dims:
        if dim.kind == "isp":
            row.append(1.0)
        elif dim.kind == "sigma":
            row.append(_sigma_of(vehicle, dim.target))  # type: ignore[arg-type]
        else:
            row.append(0.0)
    return row


def perturbed_row(
    vehicle: Vehicle, dims: Sequence[PerturbDim], index: int, sign: float
) -> list[float]:
    """第 ``index`` 维取 ±1σ、其余取标称的敏感度行（截断与采样同口径）。"""
    row = nominal_row(vehicle, dims)
    dim = dims[index]
    if dim.kind == "isp":
        row[index] = 1.0 + sign * ISP_REL_SIGMA
    elif dim.kind == "sigma":
        nominal = _sigma_of(vehicle, dim.target)  # type: ignore[arg-type]
        row[index] = min(
            max(nominal * (1.0 + sign * SIGMA_REL_SIGMA), SIGMA_DOMAIN[0]), SIGMA_DOMAIN[1]
        )
    else:
        row[index] = max(sign * LOSS_REL_SIGMA, -1.0)
    return row


# ---------------------------------------------------------------------------
# 工作进程侧：逐样本完整 evaluate 点值链路（模块级函数，可 pickle）
# ---------------------------------------------------------------------------


def _apply_row(
    vehicle: Vehicle, dims: Sequence[PerturbDim], row: Sequence[float]
) -> tuple[Vehicle, float]:
    """把一行扰动值施加到飞行器：Isp 同比缩放发动机真空推力/比冲（保 ṁ，0 级段
    账不漂移）、σ 直接覆写、损失偏移单独返回（作用在 ΔV 需求侧）。"""
    isp_core: dict[int, float] = {}
    sigma_core: dict[int, float] = {}
    isp_booster: dict[int, float] = {}
    sigma_booster: dict[int, float] = {}
    loss_delta = 0.0
    for dim, value in zip(dims, row, strict=True):
        if dim.kind == "loss":
            loss_delta = float(value)
        elif dim.kind == "isp":
            target = dim.target
            assert target is not None
            if target[0] == "core":
                isp_core[target[1]] = float(value)
            else:
                isp_booster[target[1]] = float(value)
        else:
            target = dim.target
            assert target is not None
            if target[0] == "core":
                sigma_core[target[1]] = float(value)
            else:
                sigma_booster[target[1]] = float(value)

    stages = []
    for stage in vehicle.stages:
        updates: dict[str, Any] = {}
        multiplier = isp_core.get(stage.index)
        if multiplier is not None:
            engine = stage.engine.model_copy(
                update={
                    "isp_vacuum_s": stage.engine.isp_vacuum_s * multiplier,
                    "thrust_vacuum_n": stage.engine.thrust_vacuum_n * multiplier,
                }
            )
            updates["engine"] = engine
            if stage.isp_source == "custom" and stage.isp_vacuum_s is not None:
                updates["isp_vacuum_s"] = stage.isp_vacuum_s * multiplier
        if stage.index in sigma_core:
            updates["structure_coefficient"] = sigma_core[stage.index]
        stages.append(stage.model_copy(update=updates) if updates else stage)

    boosters = []
    for group, booster in enumerate(vehicle.boosters):
        booster_updates: dict[str, Any] = {}
        stage_updates: dict[str, Any] = {}
        multiplier = isp_booster.get(group)
        if multiplier is not None:
            engine = booster.stage.engine.model_copy(
                update={
                    "isp_vacuum_s": booster.stage.engine.isp_vacuum_s * multiplier,
                    "thrust_vacuum_n": booster.stage.engine.thrust_vacuum_n * multiplier,
                }
            )
            stage_updates["engine"] = engine
            if booster.stage.isp_source == "custom" and booster.stage.isp_vacuum_s is not None:
                stage_updates["isp_vacuum_s"] = booster.stage.isp_vacuum_s * multiplier
        if group in sigma_booster:
            stage_updates["structure_coefficient"] = sigma_booster[group]
        if stage_updates:
            booster_updates["stage"] = booster.stage.model_copy(update=stage_updates)
        boosters.append(booster.model_copy(update=booster_updates) if booster_updates else booster)

    return vehicle.model_copy(update={"stages": tuple(stages), "boosters": boosters}), loss_delta


def evaluate_chunk(
    args: tuple[
        str,
        tuple[tuple[str, float, float], ...],
        tuple[PerturbDim, ...],
        list[list[float]],
        float,
    ],
) -> dict[str, list[float]]:
    """进程池工作函数：一批样本逐个跑**完整 evaluate 点值链路**。

    每样本：扰动飞行器 → ``vehicle_ledger``（几何解析账）→ GLOW + 四轨道
    ``payload_for_dv_on_ledger`` 二分（需求含损失预算扰动）；不可达轨道记 0
    （与 ``payload_by_orbit`` 的 attainable=False 口径一致）。返回按量分列的
    结果（列序 = 行序，协调侧按块序拼回全样本序列）。
    """
    vehicle_json, orbit_specs, dims, rows, payload_mass_kg = args
    vehicle = Vehicle.model_validate_json(vehicle_json)
    keys = (*(_quantity_key(orbit) for orbit, _, _ in orbit_specs), GLOW_KEY)
    columns: dict[str, list[float]] = {key: [] for key in keys}
    for row in rows:
        perturbed, loss_delta = _apply_row(vehicle, dims, row)
        ledger = vehicle_ledger(perturbed)
        for orbit, dv_used_km_s, ideal_km_s in orbit_specs:
            # 损失预算 ±20%：非理想含量 (dv_used − ideal) 整体缩放（§8.7 分布表）
            dv = ideal_km_s + (dv_used_km_s - ideal_km_s) * (1.0 + loss_delta)
            try:
                payload = payload_for_dv_on_ledger(ledger, dv, bracket_hint_kg=payload_mass_kg)
            except PerfError:
                payload = 0.0
            columns[_quantity_key(orbit)].append(payload)
        columns[GLOW_KEY].append(ledger.glow_kg(payload_mass_kg))
    return columns


# ---------------------------------------------------------------------------
# 统计（偏度 / 直方图 / 收敛 / 分位）
# ---------------------------------------------------------------------------


def skewness_adjusted_sample(values: Sequence[float]) -> tuple[float | None, str | None]:
    """修正样本偏度 G₁（§8.7 公式；adjusted_sample 口径）。

    返回 ``(G₁, None)``；``n < 3`` 或 ``m₂ = 0``（样本全部相同）返回
    ``(None, 原因)``——**不得输出 0 冒充无偏**（§8.7 明文）。
    """
    n = len(values)
    if n < 3:
        return None, f"n={n} < 3：修正样本偏度未定义（§8.7），不输出 0 冒充无偏"
    array = np.asarray(values, dtype=float)
    centered = array - array.mean()
    m2 = float(np.mean(centered**2))
    if m2 <= 0.0:
        return None, "m₂=0（样本全部相同）：偏度未定义（§8.7），不输出 0 冒充无偏"
    m3 = float(np.mean(centered**3))
    g1 = math.sqrt(n * (n - 1)) / (n - 2) * m3 / m2**1.5
    return g1, None


def _percentiles(values: np.ndarray) -> tuple[float, float, float]:
    """P5 / P50 / P95（numpy 线性插值，口径入 provenance）。"""
    p5, p50, p95 = np.percentile(values, (5.0, 50.0, 95.0))
    return float(p5), float(p50), float(p95)


# ---------------------------------------------------------------------------
# 结果模型（阶段②契约形态；mc_job_id / interval_pending 由作业层包装）
# ---------------------------------------------------------------------------


class IntervalRow(BaseModel):
    """一个量的 P5/P50/P95 区间（§8.7 输出契约）。"""

    p5: float = Field(description="P5 分位（kg）")
    p50: float = Field(description="P50 分位（kg，中位数）")
    p95: float = Field(description="P95 分位（kg）")


class MomentsRow(BaseModel):
    """一个量的分布形态指标（§8.7 末 / OI-01：n 与 estimator 必须同时声明）。"""

    n: int = Field(description="样本数（缺 n 的统计量不可复现，CON-04）")
    estimator: Literal["population", "adjusted_sample"] = Field(
        description="偏度口径：population | adjusted_sample（两种口径数值不同）"
    )
    skewness: float | None = Field(
        default=None, description="偏度系数（n<3 或 m₂=0 时为 null，见 skewness_null_reason）"
    )
    skewness_null_reason: str | None = Field(
        default=None, description="偏度为 null 的原因（§8.7：null 必须附原因，不得输出 0）"
    )
    mean_vs_p50_note: str | None = Field(
        default=None,
        description="|G₁|>0.5 时的提示：均值 ≠ P50，不得把 P50 当期望值使用（§8.7）",
    )


class HistogramRow(BaseModel):
    """直方图数据（bin 数固定 50）。"""

    bin_edges: tuple[float, ...] = Field(description="bin 边界（51 个，含两端）")
    counts: tuple[int, ...] = Field(description="各 bin 计数（50 个）")


class ConvergenceRow(BaseModel):
    """收敛诊断：前 1k/2k/5k/10k 的 P50 及其相对最终值的漂移。"""

    checkpoints: tuple[int, ...] = Field(description="检查点样本数（≤ 总样本数的那些）")
    p50: tuple[float, ...] = Field(description="各检查点处的 P50")
    drift_vs_final: tuple[float, ...] = Field(
        description="|P50(检查点) − P50(全部样本)|：漂移收窄即收敛中"
    )


class SensitivityItem(BaseModel):
    """一个参数的一阶差分敏感度（Top-8 成员）。"""

    param: str = Field(description="参数路径（stages[i].… / boosters[i].… / loss_budget）")
    impact: float = Field(description="影响（±1σ 较大 |Δ运力|/运力，按最大值归一 ≤ 1）")


class McResult(BaseModel):
    """MC 结果（阶段②载荷；两阶段契约的 interval/moments/sensitivity 来源）。"""

    interval: dict[str, IntervalRow] = Field(
        description="各量 P5/P50/P95：leo_kg / sso_kg / gto_kg / geo_kg / glow_kg"
    )
    moments: dict[str, MomentsRow] = Field(description="各量偏度与口径声明（§8.7 末）")
    histogram: dict[str, HistogramRow] = Field(description="各量直方图（50 bin）")
    convergence: dict[str, ConvergenceRow] = Field(description="各量 P50 收敛诊断")
    sensitivity: tuple[SensitivityItem, ...] = Field(
        description="参数影响排序 Top-8（一阶差分，非 Sobol）"
    )
    provenance: dict[str, str] = Field(description="溯源账目（seed/样本数/分布表/截断计数…）")


# ---------------------------------------------------------------------------
# 编排（协调线程调用；executor=None 时单进程逐块——测试与轻量路径）
# ---------------------------------------------------------------------------


_OrbitSpecs = tuple[tuple[str, float, float], ...]


def _orbit_specs(
    vehicle: Vehicle,
) -> tuple[_OrbitSpecs, dict[str, OrbitPayload], bool]:
    """各轨道 (名称, dv_used, ideal) + 标称运力表（复用 payload_by_orbit 唯一链路）。"""
    site = vehicle.mission.launch_site
    site_defaulted = site is None
    if site is None:
        site = DEFAULT_LAUNCH_SITE
    table = payload_by_orbit(vehicle, site)
    specs = tuple(
        (orbit, row.dv_used_km_s, ideal_orbit_dv_km_s(orbit, vehicle.mission))
        for orbit, row in table.items()
    )
    return specs, table, site_defaulted


def _metric_orbit(vehicle: Vehicle) -> str:
    """敏感度基准轨道：Mission 目标（运力表七目标内）否则 LEO。"""
    orbit = str(vehicle.mission.orbit_type)
    return orbit if orbit in PAYLOAD_ORBITS else "LEO"


def _chunk_rows(values: np.ndarray) -> list[list[list[float]]]:
    """样本矩阵按块切分（块 50–500：并行粒度与取消响应的平衡）。"""
    total = values.shape[0]
    size = max(50, min(500, total // 20))
    return [values[start : start + size].tolist() for start in range(0, total, size)]


def _run_chunks(
    args_by_chunk: list[tuple[Any, ...]],
    executor: ProcessPoolExecutor | None,
    on_progress: Callable[[int, int], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> list[dict[str, list[float]]]:
    """执行全部块并按块序拼回（结果顺序与样本序一致——复现性的前提）。

    进程池形态用**波次提交**（在途 ≤ 8 块）：取消在块边界即时生效，未启动的
    块直接撤回；单进程形态逐块同步。两条路径的数值**逐字节一致**（每样本的
    运算只依赖自身输入，与执行载体无关）。
    """
    total = len(args_by_chunk)
    results: dict[int, dict[str, list[float]]] = {}

    def _check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise MCCancelled

    if executor is None:
        for index, chunk_args in enumerate(args_by_chunk):
            _check_cancel()
            results[index] = evaluate_chunk(chunk_args)
            if on_progress is not None:
                on_progress(index + 1, total)
        return [results[i] for i in range(total)]

    futures: dict[Future[dict[str, list[float]]], int] = {}
    pending = list(range(total))
    while pending or futures:
        _check_cancel()
        while pending and len(futures) < _MAX_INFLIGHT_CHUNKS:
            index = pending.pop(0)
            futures[executor.submit(evaluate_chunk, args_by_chunk[index])] = index
        done, _not_done = _wait_first(futures)
        for future in done:
            index = futures.pop(future)
            results[index] = future.result()
            if on_progress is not None:
                on_progress(len(results), total)
    return [results[i] for i in range(total)]


def _wait_first(
    futures: dict[Future[dict[str, list[float]]], int],
) -> tuple[list[Future[dict[str, list[float]]]], list[Future[dict[str, list[float]]]]]:
    """等待任一在途 future 完成（concurrent.futures.wait 的薄封装，便于类型收口）。"""
    from concurrent.futures import FIRST_COMPLETED, wait

    done, not_done = wait(list(futures), return_when=FIRST_COMPLETED)
    return list(done), list(not_done)


def run_monte_carlo(
    vehicle: Vehicle,
    *,
    samples: int = DEFAULT_SAMPLES,
    seed: int | None = None,
    executor: ProcessPoolExecutor | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> McResult:
    """Monte Carlo 主入口（§8.7）：LHS 采样 → 全样本 evaluate → 统计 + 敏感度。

    - ``on_progress(done, total)``：每完成一块回调（作业进度条）。
    - ``should_cancel()``：返回 True 即抛 :class:`MCCancelled`（块边界生效）。
    - 同 ``seed`` 两跑结果逐字节一致（无时钟、无未定序统计）。
    """
    if not (MIN_SAMPLES <= samples <= MAX_SAMPLES):
        raise PerfError(
            f"样本数 {samples} 越出可配区间 [{MIN_SAMPLES}, {MAX_SAMPLES}]（§8.7）",
            suggestion=f"样本数在 {MIN_SAMPLES}–{MAX_SAMPLES} 之间，默认 {DEFAULT_SAMPLES}",
        )

    dims = perturbation_space(vehicle)
    draw = draw_samples(vehicle, dims, samples, seed)
    orbit_specs, _nominal_table, site_defaulted = _orbit_specs(vehicle)
    vehicle_json = vehicle.model_dump_json()
    payload_mass = vehicle.payload_mass_kg
    metric_orbit = _metric_orbit(vehicle)
    metric_key = _quantity_key(metric_orbit)

    chunks = _chunk_rows(draw.values)
    sample_args = [(vehicle_json, orbit_specs, dims, rows, payload_mass) for rows in chunks]
    # 敏感度行（单块批量）：基准 1 行 + 每维 ±1σ 两行——同一条 evaluate 链路、
    # 同一套截断口径；行序即结果序（nominal 在首位，随后 (dim₀₊, dim₀₋, dim₁₊, …)）
    sensitivity_rows = [nominal_row(vehicle, dims)]
    for index in range(len(dims)):
        sensitivity_rows.append(perturbed_row(vehicle, dims, index, +1.0))
        sensitivity_rows.append(perturbed_row(vehicle, dims, index, -1.0))
    sensitivity_args = [(vehicle_json, orbit_specs, dims, sensitivity_rows, payload_mass)]

    sample_columns = _run_chunks(sample_args, executor, on_progress, should_cancel)
    sensitivity_columns = _run_chunks(sensitivity_args, executor, None, should_cancel)

    # 拼列（块序 = 样本序）
    keys = (*(_quantity_key(orbit) for orbit, _, _ in orbit_specs), GLOW_KEY)
    columns: dict[str, np.ndarray] = {
        key: np.concatenate([chunk[key] for chunk in sample_columns]) for key in keys
    }

    interval: dict[str, IntervalRow] = {}
    moments: dict[str, MomentsRow] = {}
    histogram: dict[str, HistogramRow] = {}
    convergence: dict[str, ConvergenceRow] = {}
    for key, values in columns.items():
        p5, p50, p95 = _percentiles(values)
        interval[key] = IntervalRow(p5=p5, p50=p50, p95=p95)
        g1, null_reason = skewness_adjusted_sample(values.tolist())
        moments[key] = MomentsRow(
            n=samples,
            estimator="adjusted_sample",
            skewness=g1,
            skewness_null_reason=null_reason,
            mean_vs_p50_note=(
                f"偏度显著偏离 0（G₁={g1:+.3f}）：均值 ≠ P50，不得把 P50 当期望值使用（§8.7）"
                if g1 is not None and abs(g1) > SKEWNESS_NOTE_THRESHOLD
                else None
            ),
        )
        counts, edges = np.histogram(values, bins=HISTOGRAM_BINS)
        histogram[key] = HistogramRow(
            bin_edges=tuple(float(edge) for edge in edges),
            counts=tuple(int(count) for count in counts),
        )
        checkpoints = [point for point in CONVERGENCE_CHECKPOINTS if point <= samples]
        p50_trail = [float(np.percentile(values[:point], 50.0)) for point in checkpoints]
        convergence[key] = ConvergenceRow(
            checkpoints=tuple(checkpoints),
            p50=tuple(p50_trail),
            drift_vs_final=tuple(abs(value - p50) for value in p50_trail),
        )

    # 敏感度：基准 = 标称行的基准轨道运力；影响 = ±1σ 较大 |Δ| / 基准，按最大归一
    metric_values = sensitivity_columns[0][metric_key]
    nominal_metric = metric_values[0]
    impacts: list[tuple[str, float]] = []
    if nominal_metric > 0.0:
        for index, dim in enumerate(dims):
            plus = metric_values[1 + 2 * index]
            minus = metric_values[2 + 2 * index]
            raw = max(abs(plus - nominal_metric), abs(minus - nominal_metric)) / nominal_metric
            impacts.append((dim.path, raw))
        top = sorted(impacts, key=lambda item: item[1], reverse=True)[:SENSITIVITY_TOP_N]
        max_raw = top[0][1] if top else 0.0
        sensitivity = tuple(
            SensitivityItem(param=path, impact=raw / max_raw if max_raw > 0.0 else 0.0)
            for path, raw in top
        )
    else:
        sensitivity = ()
    metric_state = f"{metric_orbit} 运力"
    if nominal_metric <= 0.0:
        metric_state += "（标称 0——构型对该轨道不可达，敏感度不输出）"
    sensitivity_note = (
        "一阶差分（±1σ 重算点值，非 Sobol，§8.7）：影响 = ±1σ 两方向中较大的 "
        f"|Δ{metric_orbit} 运力| / 标称运力，按最大值归一（Top-{SENSITIVITY_TOP_N}）；"
        f"基准量 = {metric_state}"
    )

    truncated_note = (
        "; ".join(f"{path}: {count}" for path, count in sorted(draw.truncated.items()))
        if draw.truncated
        else "无（全部样本落在物理域内）"
    )
    provenance = {
        "mc.samples": str(samples),
        "mc.seed": str(draw.seed),
        "mc.sampler": "scipy.stats.qmc.LatinHypercube（§8.7：LHS 降低方差）",
        "mc.distribution_table": DISTRIBUTION_TABLE_NOTE,
        "mc.truncated_samples": truncated_note,
        "mc.linked_chain": (
            "每样本完整 evaluate 点值链路：vehicle_ledger（几何解析质量账）→ GLOW + "
            "四轨道 payload_for_dv_on_ledger 二分（§8.7：禁止简化物理）"
        ),
        "mc.loss_budget_model": (
            "损失预算 ±20%：各轨道 dv_used 的非理想含量 (dv_used − ideal) 整体 × (1+δ)"
            "（L1 级扰动；dv_used 复用 payload_by_orbit 唯一链路，含发射场纬度依赖）"
        ),
        "mc.percentile_method": "numpy.percentile 线性插值（method='linear'）",
        "mc.skewness_estimator": SKEWNESS_ESTIMATOR_NOTE,
        "mc.sensitivity_method": sensitivity_note,
        "mc.metric_orbit": metric_orbit,
        "mc.orbits": "、".join(orbit for orbit, _, _ in orbit_specs),
    }
    if site_defaulted:
        provenance["mc.launch_site"] = (
            "Mission.launch_site 缺失：ΔV 需求按默认发射场（卡纳维拉尔 28.5°N、向东）"
            "计算——与 /api/perf/evaluate 同口径（§6.1）"
        )

    return McResult(
        interval=interval,
        moments=moments,
        histogram=histogram,
        convergence=convergence,
        sensitivity=sensitivity,
        provenance=provenance,
    )


def mc_metrics_payload(result: McResult, *, job_id: str) -> dict[str, Any]:
    """阶段②契约包装：McResult + ``interval_pending=false`` + ``mc_job_id``。

    契约形态（OI-25 定死）：阶段②到达后整体覆盖同名键；本函数产出的 dict 即
    作业 ``metrics``（经既有作业通道 GET /api/jobs/{id} 与 WS 下发）。
    """
    payload = result.model_dump(mode="json")
    payload["interval_pending"] = False
    payload["mc_job_id"] = job_id
    return payload


__all__ = [
    "CONVERGENCE_CHECKPOINTS",
    "DEFAULT_SAMPLES",
    "DISTRIBUTION_TABLE_NOTE",
    "GLOW_KEY",
    "HISTOGRAM_BINS",
    "ISP_REL_SIGMA",
    "LOSS_REL_SIGMA",
    "MAX_SAMPLES",
    "MIN_SAMPLES",
    "SENSITIVITY_TOP_N",
    "SIGMA_DOMAIN",
    "SIGMA_REL_SIGMA",
    "SKEWNESS_ESTIMATOR_NOTE",
    "SKEWNESS_NOTE_THRESHOLD",
    "ConvergenceRow",
    "HistogramRow",
    "IntervalRow",
    "MCCancelled",
    "McResult",
    "MomentsRow",
    "PerturbDim",
    "SensitivityItem",
    "draw_samples",
    "evaluate_chunk",
    "mc_metrics_payload",
    "nominal_row",
    "perturbation_space",
    "perturbed_row",
    "run_monte_carlo",
    "skewness_adjusted_sample",
]
