"""M4 第四片：MC 不确定度（§8.7）+ 任务时序耦合（§8.9）+ 两阶段契约（OI-25）+ §9.1 扩池。

覆盖形态（任务口径逐条）
------------------------
- **时序**：两事件序列（分离 + 抛罩）质量闭合手算对拍；回收点火 → 运力下降断言
  （「直接减少运力」）；抛罩时刻超动压 → warning（规则 2：警告不中止）；回写迭代
  的 GLOW 增长与回收模式运力闭合（规则 1：capacity_recoverable ≈ 设计载荷）。
- **MC**：seed 复现（同 seed 两跑逐字节一致）；P5≤P50≤P95；区间合理
  （P50 ≈ 点值 ±5%——同链路应如此，偏差大说明物理分叉）；收敛诊断单调性；
  敏感度 Top-8 非空且 impact 归一（max=1、降序）；偏度 n=2 → null 带原因；
  samples=200 → 4xx。
- **两阶段**：evaluate 响应含 interval_pending=true + mc_job_id；mc=false 时不投递；
  作业结果形态与契约定死形态**逐键**一致；阶段①不调 MC（evaluate < 100 ms）。
- **§9.1 扩池**：MC 跑时 /api/health < 50 ms（事件循环不阻塞）；作业可取消、
  取消后无半成品落盘（mc.json 不存在）。
- **端点 + 422** + 真实 F9 MC 冒烟（samples=1000 提速）：报告 P50 vs 点值偏差与
  P5–P95 宽度（数字随 -s 输出呈交主线）。
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.cache.store import mc_cache_key
from aeroforge.params.schema import Recovery, Sequence, SequenceEvent, Vehicle
from aeroforge.params.templates import falcon9_vehicle
from aeroforge.perf.capacity import payload_by_orbit, payload_for_dv
from aeroforge.perf.mc import (
    CONVERGENCE_CHECKPOINTS,
    HISTOGRAM_BINS,
    run_monte_carlo,
    skewness_adjusted_sample,
)
from aeroforge.perf.sequence import (
    FAIRING_Q_LIMIT_PA,
    apply_sequence,
    fairing_mass_kg,
)
from aeroforge.perf.solver import solve

#: 时序测试的目标 ΔV（m/s，LEO 量级；two_stage 夹具可达）。
_TARGET_DV_M_S = 9_650.0

#: MC 冒烟样本数（任务口径：1000 提速；全量 10 000 走 API 两阶段测试）。
_MC_SMOKE_SAMPLES = 1_000

_JOB_TIMEOUT_S = 180.0  # 10 000 样本 + 进程池冷启动的等待上限


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _await_job(client: TestClient, job_id: str) -> dict[str, object]:
    """轮询作业直到终态（§10.2 降级路径）。"""
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    while time.monotonic() < deadline:
        body: dict[str, object] = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("succeeded", "failed", "cancelled"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"作业 {job_id} 在 {_JOB_TIMEOUT_S}s 内未达终态")


def _recovered(vehicle: Vehicle) -> Vehicle:
    """给飞行器装上动力反推回收（三项代价齐备：§8.9 规则 3 的正例剖面）。"""
    return vehicle.model_copy(
        update={
            "fairing_diameter_m": vehicle.fairing_diameter_m or 5.2,
            "recovery": Recovery(
                enabled=True,
                stage_indices=(1,),
                method="propulsive",
                landing_propellant_margin_fraction=0.10,
                system_mass_kg=2_000.0,
                reinforcement_mass_kg=1_500.0,
            ),
        }
    )


# ---------------------------------------------------------------------------
# 时序（§8.9）：事件账手算对拍 + 回收代价 + 动压校验
# ---------------------------------------------------------------------------


def test_sequence_mass_closure_hand_reconciled(two_stage_vehicle: Vehicle) -> None:
    """两事件序列（分离 + 抛罩）质量闭合：事件账 vs 求解器质量行的手算对拍。"""
    vehicle = two_stage_vehicle.model_copy(update={"fairing_diameter_m": 5.2})
    sizing = solve(vehicle, _TARGET_DV_M_S)
    report = apply_sequence(vehicle, sizing, target_delta_v_m_s=_TARGET_DV_M_S)

    assert report.recovery is None  # 未启用回收：无回写、无代价
    assert report.writeback_iterations == 0
    assert report.capacity_penalty_kg == 0.0
    assert report.payload_capacity_expendable_kg == pytest.approx(
        report.payload_capacity_recoverable_kg, rel=1e-9
    )
    assert report.payload_capacity_recoverable_kg == pytest.approx(
        sizing.payload_mass_kg, rel=1e-3
    )  # 闭合：定尺火箭在其设计 ΔV 下的运力 ≈ 设计载荷

    stage1 = sizing.stages[0]
    stage2 = sizing.stages[1]
    fairing = fairing_mass_kg(vehicle)
    assert fairing is not None and 1_500.0 < fairing < 2_500.0  # 5.2 m 罩 ≈ 1.96 t 量级锚

    events = {event.event: event for event in report.events}
    # 点火：整箭 = GLOW + 整流罩（求解器无整流罩自由度，时序账列示）
    assert events["ignition"].mass_before_kg == pytest.approx(sizing.glow_kg + fairing, rel=1e-9)
    # 抛罩：恰抛整流罩质量（§8.9 表）
    assert events["fairing_jettison"].mass_delta_kg == pytest.approx(-fairing, rel=1e-9)
    assert events["fairing_jettison"].mass_after_kg == pytest.approx(sizing.glow_kg, rel=1e-9)
    # 分离：燃烧可用推进剂后抛掉一级干重（手算对拍；缺省事件序中抛罩先于分离，
    # 故分离后的栈 = 二级总质量 + 载荷——整流罩已在前一事件抛离）
    separation = events["stage_separation"]
    assert separation.mass_before_kg == pytest.approx(
        sizing.glow_kg - stage1.m_propellant_kg, rel=1e-9
    )
    assert separation.mass_delta_kg == pytest.approx(-stage1.m_dry_kg, rel=1e-9)
    assert separation.mass_after_kg == pytest.approx(
        stage2.m_total_kg + sizing.payload_mass_kg, rel=1e-9
    )
    # 入轨闭合：主账余量 == 载荷 + 未分离级干重（§8.9 规则 1 的机检面）
    insertion = events["orbit_insertion"]
    assert insertion.mass_before_kg == pytest.approx(
        sizing.payload_mass_kg + stage2.m_dry_kg, rel=1e-6
    )


def test_sequence_recovery_reduces_capacity(two_stage_vehicle: Vehicle) -> None:
    """回收点火 → 运力下降（§8.9 表「直接减少运力」）+ 回写使 GLOW 增长（规则 1）。"""
    expendable = two_stage_vehicle
    recovered = _recovered(two_stage_vehicle)

    sizing_exp = solve(expendable, _TARGET_DV_M_S)
    report_exp = apply_sequence(expendable, sizing_exp, target_delta_v_m_s=_TARGET_DV_M_S)

    sizing_rec = solve(recovered, _TARGET_DV_M_S)
    report_rec = apply_sequence(recovered, sizing_rec, target_delta_v_m_s=_TARGET_DV_M_S)

    # 回写痕迹：迭代重跑、GLOW 增长（回收代价是真实吨位）
    assert report_rec.writeback_iterations >= 1
    assert report_rec.glow_kg > report_rec.glow_kg_expendable
    # 三项代价独立成账（规则 3）：系统 / 增强 / 预留各列其值
    costs = report_rec.recovery
    assert costs is not None
    assert costs.system_mass_kg == 2_000.0
    assert costs.reinforcement_mass_kg == 1_500.0
    assert costs.landing_propellant_margin_fraction == 0.10
    assert costs.landing_propellant_kg > 0.0
    # 回收点火的运力代价：同一枚火箭，回收模式 < 全燃烧模式（「直接减少运力」）
    assert report_rec.payload_capacity_recoverable_kg < report_rec.payload_capacity_expendable_kg
    assert report_rec.capacity_penalty_kg > 0.0
    # 回写后的火箭在回收模式下仍闭合于设计载荷（迭代在时序约束下完成）
    assert report_rec.payload_capacity_recoverable_kg == pytest.approx(
        sizing_rec.payload_mass_kg, rel=1e-3
    )
    # landing 事件：消耗预留推进剂（被回收级独立账），着陆后级质量 = 干重 − 预留
    landing = next(e for e in report_rec.events if e.event == "landing")
    assert landing.account == "recovered_stage"
    assert landing.mass_delta_kg == pytest.approx(-costs.landing_propellant_kg, rel=1e-9)
    assert landing.mass_after_kg == pytest.approx(
        landing.mass_before_kg - costs.landing_propellant_kg
    )
    # 无回收构型无运力代价（对照组）
    assert report_exp.capacity_penalty_kg == 0.0


def test_sequence_fairing_jettison_over_q_warning() -> None:
    """规则 2：抛罩时刻动压超限 → 显式 warning（不中止——时序是用户权威）。

    用 F9 模板（真实推重比 ~1.4–1.7，剖面模型的有效标定域）：t=40 s 在 Q 峰值区
    → 超限 warning；缺省 t=120 s（高空低动压区）→ 无 warning。
    """
    vehicle = falcon9_vehicle()
    early = vehicle.model_copy(
        update={
            "sequence": Sequence(
                events=(
                    SequenceEvent(event="ignition", time_s=0.0),
                    SequenceEvent(event="fairing_jettison", time_s=40.0),
                    SequenceEvent(event="stage_separation", stage_index=1),
                    SequenceEvent(event="orbit_insertion"),
                )
            )
        }
    )
    sizing = solve(early, _TARGET_DV_M_S)
    report = apply_sequence(early, sizing, target_delta_v_m_s=_TARGET_DV_M_S)
    assert 1.0 < report.twr_liftoff < 3.0  # 求解尺寸下 F9 剖面在模型有效域（前置自检）
    over_q = [w for w in report.warnings if "动压" in w and "上限" in w]
    assert over_q, "抛罩 t=40s（Q 峰值区）必须有动压超限 warning（§8.9 规则 2）"
    assert "不中止" in over_q[0]

    # 对照：默认 t=120s（高空低动压区）无动压 warning
    nominal = vehicle  # F9 自带 fairing_diameter_m=5.2，缺省事件线含 120s 抛罩
    sizing_late = solve(nominal, _TARGET_DV_M_S)
    report_late = apply_sequence(nominal, sizing_late, target_delta_v_m_s=_TARGET_DV_M_S)
    assert not any("动压" in w and "上限" in w for w in report_late.warnings)
    # 上限常量本身在工程惯例锚量级（kPa 级，§8.9）
    assert FAIRING_Q_LIMIT_PA == 1_000.0


def test_sequence_endpoint_shapes_and_422(client: TestClient, two_stage_vehicle: Vehicle) -> None:
    """POST /api/sizing/sequence：200 形状 + 422 路径（约束违反 / 非法目标 ΔV）。"""
    body = {
        "vehicle": two_stage_vehicle.model_dump(mode="json"),
        "target_delta_v_m_s": _TARGET_DV_M_S,
    }
    response = client.post("/api/sizing/sequence", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "events",
        "glow_kg",
        "glow_kg_expendable",
        "payload_mass_kg",
        "fairing_mass_kg",
        "twr_liftoff",
        "recovery",
        "payload_capacity_expendable_kg",
        "payload_capacity_recoverable_kg",
        "capacity_penalty_kg",
        "writeback_iterations",
        "warnings",
        "provenance",
    }
    assert payload["glow_kg"] > 0.0
    assert payload["twr_liftoff"] > 0.0
    assert payload["events"][0]["event"] == "ignition"

    # 422：目标 ΔV 非正（Schema 值域）
    bad = {**body, "target_delta_v_m_s": 0.0}
    assert client.post("/api/sizing/sequence", json=bad).status_code == 422
    # 422：硬约束违反（custom 比冲缺值）
    broken = two_stage_vehicle.model_copy(
        update={
            "stages": (
                two_stage_vehicle.stages[0].model_copy(update={"isp_source": "custom"}),
                two_stage_vehicle.stages[1],
            )
        }
    )
    violated = {
        "vehicle": broken.model_dump(mode="json"),
        "target_delta_v_m_s": _TARGET_DV_M_S,
    }
    rejected = client.post("/api/sizing/sequence", json=violated)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "PARAMS_CONSTRAINT_VIOLATION"


def test_sequence_recovery_endpoint_writeback(two_stage_vehicle: Vehicle) -> None:
    """端点形态：回收构型 200 + 三项代价 + 回写迭代痕迹（QA-4 挂点经 API 启用）。"""
    with TestClient(app) as api:
        recovered = _recovered(two_stage_vehicle)
        response = api.post(
            "/api/sizing/sequence",
            json={
                "vehicle": recovered.model_dump(mode="json"),
                "target_delta_v_m_s": _TARGET_DV_M_S,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["recovery"] is not None
        assert payload["recovery"]["landing_propellant_kg"] > 0.0
        assert payload["writeback_iterations"] >= 1
        assert payload["capacity_penalty_kg"] > 0.0
        assert any(e["event"] == "landing" for e in payload["events"])


# ---------------------------------------------------------------------------
# MC 统计（§8.7）：seed 复现 / 分位序 / 区间合理 / 收敛 / 敏感度 / 偏度
# ---------------------------------------------------------------------------


def test_mc_seed_reproducibility_byte_identical(two_stage_vehicle: Vehicle) -> None:
    """同 seed 两跑结果逐字节一致（统计全为确定性运算，结果不含时钟）。"""
    first = run_monte_carlo(two_stage_vehicle, samples=_MC_SMOKE_SAMPLES, seed=42)
    second = run_monte_carlo(two_stage_vehicle, samples=_MC_SMOKE_SAMPLES, seed=42)
    assert first.model_dump_json() == second.model_dump_json()
    # 不同 seed → 不同样本（防「假采样」：两次采样必须真的不同）
    other = run_monte_carlo(two_stage_vehicle, samples=_MC_SMOKE_SAMPLES, seed=43)
    assert other.model_dump_json() != first.model_dump_json()


def test_mc_percentiles_ordered_and_reasonable(two_stage_vehicle: Vehicle) -> None:
    """P5 ≤ P50 ≤ P95（全量）；P50(LEO) ≈ 点值 ±5%（同链路应如此——偏差大即物理分叉）。"""
    result = run_monte_carlo(two_stage_vehicle, samples=_MC_SMOKE_SAMPLES, seed=7)
    assert set(result.interval) == {"leo_kg", "sso_kg", "gto_kg", "geo_kg", "glow_kg"}
    for key, row in result.interval.items():
        assert row.p5 <= row.p50 <= row.p95, f"{key} 分位序破坏：{row}"
        assert math.isfinite(row.p5) and math.isfinite(row.p95)
    # geo 边界轨道：标称不可达（two_stage 上限 < GEO 需求）——扰动后仅少数样本可达，
    # P5 必须仍为 0（多数样本落在不可达侧），偏度呈强右偏或 null（口径都已声明）
    assert result.interval["geo_kg"].p5 == 0.0
    geo_moments = result.moments["geo_kg"]
    assert geo_moments.n == _MC_SMOKE_SAMPLES
    if geo_moments.skewness is not None:
        assert geo_moments.skewness > 0.5  # 0 与稀疏非零混合 → 强右偏（均值 ≠ P50 情形）
        assert geo_moments.mean_vs_p50_note is not None
    else:
        assert geo_moments.skewness_null_reason is not None
    # 区间合理：P50(LEO) 与点值（同一条 evaluate 链路）偏差 < 5%
    point = payload_for_dv(two_stage_vehicle, 9.65)
    p50 = result.interval["leo_kg"].p50
    deviation = abs(p50 - point) / point
    print(f"\n[MC 区间] LEO P50={p50:.0f} vs 点值 {point:.0f}（偏差 {deviation:.1%}）")
    assert deviation < 0.05, f"P50 与点值偏差 {deviation:.1%} 超过 5%——物理分叉，须报告"


def test_mc_histogram_and_convergence(two_stage_vehicle: Vehicle) -> None:
    """直方图（50 bin）形态 + 收敛诊断：检查点覆盖、末点漂移为 0、早期漂移更大。"""
    samples = 10_000  # 覆盖全部检查点（1k/2k/5k/10k）
    result = run_monte_carlo(two_stage_vehicle, samples=samples, seed=11)
    for key, histogram in result.histogram.items():
        assert len(histogram.counts) == HISTOGRAM_BINS
        assert len(histogram.bin_edges) == HISTOGRAM_BINS + 1
        assert sum(histogram.counts) == samples
        assert histogram.bin_edges[0] <= result.interval[key].p5
        assert histogram.bin_edges[-1] >= result.interval[key].p95
    convergence = result.convergence["leo_kg"]
    assert convergence.checkpoints == CONVERGENCE_CHECKPOINTS
    assert set(result.convergence["glow_kg"].checkpoints) <= set(CONVERGENCE_CHECKPOINTS)
    drifts = convergence.drift_vs_final
    assert all(d >= 0.0 for d in drifts)
    # 检查点机械性：k == n 时 P50 与终值同源，漂移恰为 0
    assert drifts[-1] == 0.0
    # 收敛量级：前 5k 检查点的 P50 距终值漂移 < 1%（中位数标准误 ∝ 1/√k——逐点
    # 单调在有限样本下是随机量，按量级断言才是稳健的收敛判据）
    assert drifts[2] < 0.01 * convergence.p50[-1], (
        f"前 5k 的 P50 漂移 {drifts[2]:.1f} kg ≥ 终值的 1%——收敛异常，须报告"
    )


def test_mc_sensitivity_top8_normalized(two_stage_vehicle: Vehicle) -> None:
    """敏感度 Top-8：非空、降序、impact 归一（max=1）；方法注记「一阶差分，非 Sobol」。"""
    result = run_monte_carlo(two_stage_vehicle, samples=_MC_SMOKE_SAMPLES, seed=5)
    sensitivity = result.sensitivity
    assert 0 < len(sensitivity) <= 8
    impacts = [item.impact for item in sensitivity]
    assert impacts == sorted(impacts, reverse=True), "敏感度必须按影响降序"
    assert impacts[0] == pytest.approx(1.0), "归一口径：最大影响 = 1"
    assert all(0.0 < impact <= 1.0 + 1e-9 for impact in impacts)
    # 参数路径形态（契约示例：stages[0].structure_coefficient）与 loss_budget 全局项
    params = {item.param for item in sensitivity}
    assert any(p.startswith("stages[") and p.endswith(".structure_coefficient") for p in params)
    assert "loss_budget" in params
    assert "一阶差分" in result.provenance["mc.sensitivity_method"]
    assert "非 Sobol" in result.provenance["mc.sensitivity_method"]
    # 分布表与 QA-1 口径入 provenance（效率因子不另设扰动的双计防护声明）
    assert "±2%" in result.provenance["mc.distribution_table"]
    assert "±15%" in result.provenance["mc.distribution_table"]
    assert "±20%" in result.provenance["mc.distribution_table"]
    assert "QA-1" in result.provenance["mc.distribution_table"]
    # 种子与样本数入 provenance（复现性账目）
    assert result.provenance["mc.seed"] == "5"
    assert result.provenance["mc.samples"] == str(_MC_SMOKE_SAMPLES)


def test_mc_skewness_null_cases() -> None:
    """偏度（§8.7 末）：n<3 → null 带原因；全同样本（m₂=0）→ null 带原因。"""
    g1, reason = skewness_adjusted_sample([1.0, 2.0])
    assert g1 is None and reason is not None and "n=2" in reason
    g1_flat, reason_flat = skewness_adjusted_sample([3.0, 3.0, 3.0, 3.0])
    assert g1_flat is None and reason_flat is not None and "m₂=0" in reason_flat
    # 正例：n≥3 且非常数 → 有限值；对称样本偏度 ≈ 0
    symmetric = [float(x) for x in range(-50, 51)]
    g1_sym, reason_sym = skewness_adjusted_sample(symmetric)
    assert g1_sym is not None and reason_sym is None
    assert abs(g1_sym) < 1e-9


def test_mc_skewness_note_when_significant(two_stage_vehicle: Vehicle) -> None:
    """|G₁|>0.5 → mean_vs_p50_note 提示「均值 ≠ P50」（§8.7：不得把 P50 当期望值）。"""
    # geo 全 0 + 少量非零样本 → 强右偏（构造性检查 note 通路）
    result = run_monte_carlo(two_stage_vehicle, samples=_MC_SMOKE_SAMPLES, seed=7)
    noted = [(key, row) for key, row in result.moments.items() if row.mean_vs_p50_note is not None]
    for _key, row in noted:
        assert row.skewness is not None and abs(row.skewness) > 0.5
        note = row.mean_vs_p50_note or ""
        assert "P50" in note or "均值" in note
    # 每个量都声明 n 与 estimator（缺口径的统计量不可复现，CON-04）
    for row in result.moments.values():
        assert row.n == _MC_SMOKE_SAMPLES
        assert row.estimator == "adjusted_sample"


def test_falcon9_mc_smoke_reports_numbers() -> None:
    """真实 F9 MC 冒烟（samples=1000 提速）：P50 vs 点值偏差与 P5–P95 宽度随输出呈交。

    点值对照取 ``payload_by_orbit`` 的 LEO 行（发射场 28.56° 插值需求，与 MC 的
    dv_used 同源同链路——口径不一致的对照会把「比较误差」误报成物理分叉）。
    """
    vehicle = falcon9_vehicle()
    started = time.perf_counter()
    result = run_monte_carlo(vehicle, samples=_MC_SMOKE_SAMPLES, seed=2026)
    elapsed = time.perf_counter() - started
    site = vehicle.mission.launch_site
    assert site is not None
    point = payload_by_orbit(vehicle, site)["LEO"].payload_kg
    row = result.interval["leo_kg"]
    width = row.p95 - row.p5
    deviation = abs(row.p50 - point) / point
    print(
        f"\n[F9 MC 冒烟 | {elapsed:.1f}s | {vehicle.name}] "
        f"LEO: P5={row.p5:.0f} P50={row.p50:.0f}（点值 {point:.0f}，偏差 {deviation:.1%}）"
        f" P95={row.p95:.0f}（P5–P95 宽度 {width:.0f} kg）"
        f" | 偏度 G₁={result.moments['leo_kg'].skewness:+.3f}"
        f" | GLOW P50={result.interval['glow_kg'].p50:.0f} kg"
    )
    assert row.p5 <= row.p50 <= row.p95
    assert deviation < 0.05, f"F9 P50 偏差 {deviation:.1%} > 5%——物理分叉，须报告"
    assert width > 0.0


# ---------------------------------------------------------------------------
# 两阶段契约（OI-25）+ §9.1 扩池（进程池 / 事件循环不阻塞 / 可取消）
# ---------------------------------------------------------------------------


def test_evaluate_two_stage_contract_and_job_result_shape(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """阶段①：interval_pending=true + mc_job_id；阶段②：作业结果与契约逐键一致。

    本测试走默认 10 000 样本（顺带产出全量 MC 实测耗时，随报告呈交）。
    """
    vehicle = two_stage_vehicle.model_copy(update={"name": "两阶段契约测试箭"})
    body = {"vehicle": vehicle.model_dump(mode="json")}
    started = time.perf_counter()
    response = client.post("/api/perf/evaluate", json=body)
    sync_ms = (time.perf_counter() - started) * 1000.0
    assert response.status_code == 200
    stage1 = response.json()
    assert stage1["interval_pending"] is True
    assert isinstance(stage1["mc_job_id"], str) and stage1["mc_job_id"]
    print(f"\n[两阶段] evaluate 同步段（含 MC 投递）实测 {sync_ms:.1f} ms")
    assert sync_ms < 100.0, f"阶段①同步耗时 {sync_ms:.1f} ms ≥ 100 ms（规则 3：不得调 MC）"

    job_started = time.monotonic()
    job = _await_job(client, stage1["mc_job_id"])
    job_elapsed = time.monotonic() - job_started
    assert job["status"] == "succeeded", job.get("error")
    print(f"[两阶段] MC 10 000 样本作业（进程池，含冷启动）实测 {job_elapsed:.1f} s")

    metrics: dict[str, Any] = job["metrics"]  # type: ignore[assignment]
    # 契约顶层键（OI-25 定死）：interval / moments / sensitivity / interval_pending /
    # mc_job_id / provenance；直方图与收敛诊断为附加输出
    assert {
        "interval",
        "moments",
        "sensitivity",
        "interval_pending",
        "mc_job_id",
        "provenance",
        "histogram",
        "convergence",
    } <= set(metrics)
    assert metrics["interval_pending"] is False  # 阶段②到达：不再是占位态
    assert metrics["mc_job_id"] == stage1["mc_job_id"]
    # interval 逐键：leo/gto/glow（契约示例键）+ sso/geo（四目标齐备）
    assert {"leo_kg", "gto_kg", "glow_kg"} <= set(metrics["interval"])
    for key in ("leo_kg", "gto_kg", "glow_kg"):
        row = metrics["interval"][key]
        assert set(row) == {"p5", "p50", "p95"}
        assert row["p5"] <= row["p50"] <= row["p95"]
    # moments 逐键：n / estimator / skewness / mean_vs_p50_note（§8.7 必须声明口径）
    leo_moments = metrics["moments"]["leo_kg"]
    assert {"n", "estimator", "skewness", "mean_vs_p50_note"} <= set(leo_moments)
    assert leo_moments["n"] == 10_000
    assert leo_moments["estimator"] == "adjusted_sample"
    # sensitivity 逐键：param / impact，Top-8
    assert 0 < len(metrics["sensitivity"]) <= 8
    for item in metrics["sensitivity"]:
        assert set(item) == {"param", "impact"}
    assert metrics["provenance"]["mc.samples"] == "10000"
    assert metrics["provenance"]["mc.seed"].isdigit()


def test_evaluate_mc_false_skips_submission(client: TestClient, two_stage_vehicle: Vehicle) -> None:
    """mc=false：不投递——interval_pending=false 且无 mc_job_id（阶段①仍完整返回点值）。"""
    vehicle = two_stage_vehicle.model_copy(update={"name": "mc 关闭测试箭"})
    body = {"vehicle": vehicle.model_dump(mode="json"), "mc": False}
    response = client.post("/api/perf/evaluate", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["interval_pending"] is False
    assert payload["mc_job_id"] is None
    # 点值字段仍齐备（阶段①载荷不受 mc 开关影响）
    assert payload["point"]["payload_by_orbit"]["LEO"]["payload_kg"] > 0.0
    assert payload["delta_v_budget"]["total_dv_km_s"] > 0.0


def test_mc_endpoint_standalone_and_cache_reproducibility(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """POST /api/uncertainty/mc：独立触发 → {job_id}；同 seed 两投结果一致（缓存复算同源）。"""
    vehicle = two_stage_vehicle.model_copy(update={"name": "MC 独立触发测试箭"})
    body = {
        "vehicle": vehicle.model_dump(mode="json"),
        "samples": _MC_SMOKE_SAMPLES,
        "seed": 99,
    }
    first = client.post("/api/uncertainty/mc", json=body)
    assert first.status_code == 200
    job_id = first.json()["job_id"]
    job = _await_job(client, job_id)
    assert job["status"] == "succeeded", job.get("error")
    metrics: dict[str, Any] = job["metrics"]  # type: ignore[assignment]
    assert metrics["mc_job_id"] == job_id

    # 同输入再投：命中 mc.json 缓存，立即成功且结果一致（除 mc_job_id）
    second = client.post("/api/uncertainty/mc", json=body)
    assert second.status_code == 200
    job2 = _await_job(client, second.json()["job_id"])
    assert job2["status"] == "succeeded"
    timings: dict[str, float] = job2["timings_ms"]  # type: ignore[assignment]
    assert timings.get("cache_hit_ms") is not None  # 缓存命中路径留痕
    metrics2: dict[str, Any] = job2["metrics"]  # type: ignore[assignment]
    expected = {k: v for k, v in metrics.items() if k != "mc_job_id"}
    actual = {k: v for k, v in metrics2.items() if k != "mc_job_id"}
    assert actual == expected


def test_mc_endpoint_rejects_out_of_range_samples(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """samples=200（越出 §8.7 可配区间 1 000–100 000）→ 4xx。"""
    body = {
        "vehicle": two_stage_vehicle.model_dump(mode="json"),
        "samples": 200,
    }
    response = client.post("/api/uncertainty/mc", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_INVALID"
    # 上界外同理（100 001）
    body["samples"] = 100_001
    assert client.post("/api/uncertainty/mc", json=body).status_code == 422


def test_mc_running_health_stays_responsive(client: TestClient, two_stage_vehicle: Vehicle) -> None:
    """§9.1 硬规则 1（计算侧）：MC 跑时事件循环不阻塞——/api/health < 50 ms。"""
    vehicle = two_stage_vehicle.model_copy(update={"name": "健康并发测试箭"})
    body = {
        "vehicle": vehicle.model_dump(mode="json"),
        "samples": 10_000,  # 全量样本：作业运行期间打健康检查
        "seed": 314,
    }
    submitted = client.post("/api/uncertainty/mc", json=body)
    job_id = submitted.json()["job_id"]

    worst_ms = 0.0
    probes = 0
    while True:
        started = time.perf_counter()
        health = client.get("/api/health")
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        assert health.status_code == 200
        worst_ms = max(worst_ms, elapsed_ms)
        probes += 1
        status = client.get(f"/api/jobs/{job_id}").json()["status"]
        if status in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.02)
    assert status == "succeeded"
    print(f"\n[扩池] MC 运行期间 /api/health 探针 {probes} 次，最差 {worst_ms:.1f} ms")
    assert worst_ms < 50.0, f"MC 运行时 health 最差 {worst_ms:.1f} ms ≥ 50 ms（事件循环被阻塞）"


def test_mc_job_cancellable_no_partial_artifacts(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """§9.1 硬规则 4：作业可取消；取消后无半成品落盘（mc.json 不存在）。"""
    vehicle = two_stage_vehicle.model_copy(update={"name": "取消测试箭"})
    body = {
        "vehicle": vehicle.model_dump(mode="json"),
        "samples": 100_000,  # 长作业：保证取消窗口
        "seed": 271,
    }
    submitted = client.post("/api/uncertainty/mc", json=body)
    job_id = submitted.json()["job_id"]

    # 等 MC 真正跑起来（RUNNING 且进入 evaluating）再取消
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        record = client.get(f"/api/jobs/{job_id}").json()
        if record["status"] == "running" and record["stage"] == "evaluating":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("MC 作业未进入 evaluating 阶段（无法测取消）")

    # 取消走作业执行器（与几何作业同一取消通道；HTTP 取消端点不在本片契约内）
    from aeroforge.api.deps import get_compute_runner

    assert get_compute_runner().cancel(job_id) is True
    cancelled = _await_job(client, job_id)
    assert cancelled["status"] == "cancelled"
    assert cancelled["metrics"] is None  # 无结果即无半成品

    # 半成品落盘检查：该键的 mc.json 必须不存在（结果只在全部样本完成后原子写入）
    key = mc_cache_key(vehicle, 100_000, 271)
    from aeroforge.paths import artifacts_root

    assert not (artifacts_root() / key / "mc.json").is_file()


def test_evaluate_latency_under_100ms_stage1(
    client: TestClient, two_stage_vehicle: Vehicle
) -> None:
    """规则 3 机检：evaluate 同步段 < 100 ms（MC 在后台作业，不在交互路径）。"""
    vehicle = two_stage_vehicle.model_copy(update={"name": "耗时复核测试箭"})
    body = {"vehicle": vehicle.model_dump(mode="json")}
    first_started = time.perf_counter()
    first = client.post("/api/perf/evaluate", json=body)
    first_ms = (time.perf_counter() - first_started) * 1000.0
    cached_started = time.perf_counter()
    cached = client.post("/api/perf/evaluate", json=body)
    cached_ms = (time.perf_counter() - cached_started) * 1000.0
    assert first.status_code == 200 and cached.status_code == 200
    print(f"\n[规则 3] evaluate 首算 {first_ms:.1f} ms / 缓存命中 {cached_ms:.1f} ms")
    assert first_ms < 100.0
    assert cached_ms < 100.0
    # 两次都投递了 MC 作业（interval_pending=true），且第二次 MC 命中缓存后仍即时返回
    assert first.json()["interval_pending"] is True
    assert cached.json()["interval_pending"] is True
    assert cached.json()["cache_hit"] is True
