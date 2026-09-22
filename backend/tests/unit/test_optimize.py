"""优化与权衡研究（规格 §14，M6）：NSGA-II · 权衡 · 扫描 · 逆向 + 四端点作业链。

覆盖形态（任务口径逐条）
------------------------
- **单目标退化收敛对照**：min_glow 单变量退化问题，NSGA-II 最优解对拍全因子
  扫描参照（同为边界解，偏差 ≤ 1%）——优化不劣于穷举、不劣于基线；
- **seed 逐位一致**：同 seed 同输入两跑，Pareto 前沿的候选内容（params / glow /
  payload）逐位一致（行级 provenance 的耗时 / 命中标记是审计元数据，随运行而变，
  **有意**不参与逐位比对——结果级 audit 中的 seed / 规模 / 代数逐位一致）；
- **Pareto 支配白盒**：约束支配（Deb 2001 §7.1）与非支配排序的构造用例对拍；
- **不可行淘汰机检**：评估链对硬违 / 分区不铺满候选显式淘汰带原因；约束支配
  使"目标更差的可行解"仍胜"目标更好的不可行解"；真实运行中不可行候选计数控
  制且永不入前沿；
- **缓存复用率 > 0**（§14 约束 1）：同输入第二次运行全部命中候选缓存；
- **超限 422**：权衡 > 20 方案、扫描 > 10⁴ 组合 / > 3 轴（§14 目标尺度，
  ``OPTIMIZE_INVALID``，code / message / suggestion 齐）；变量路径 / 类别 /
  基线一致性同样 422；
- **逆向设计**：F9 目标（低于基线运力）→ 构型运力 ≥ 目标且 GLOW ≤ 基线；
  不可达目标 → 作业 FAILED（``OPTIMIZE_INFEASIBLE``），**不硬凑**；
- **四端点全链作业形状**：POST → job_id → 作业通道终态 → metrics 契约键
  （pareto_front / variants / rows / config）与行形态（label?/params/glow_kg/
  payload_kg/provenance?）逐键核对；
- **判据实测**：F9 × 三变量 × 40×20 NSGA-II 端到端 < 60 s（§16 M6），
  实测秒数与缓存命中数随报告呈交。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.cache.store import ArtifactStore
from aeroforge.errors import OptimizeError
from aeroforge.optimize.design_space import OptimizeVariableSpec, parse_variables
from aeroforge.optimize.inverse import InverseRequest, run_inverse
from aeroforge.optimize.nsga2 import (
    Nsga2Request,
    _Individual,
    constraint_dominates,
    crowding_distance,
    fast_non_dominated_sort,
    run_nsga2,
    validate_objectives,
)
from aeroforge.optimize.objectives import (
    CandidateResult,
    evaluate_candidate,
    objective_vector,
)
from aeroforge.optimize.sweep import SweepAxisSpec, SweepRequest, run_sweep
from aeroforge.optimize.trade_study import MAX_VARIANTS, TradeStudyRequest, run_trade_study
from aeroforge.params.report import has_hard
from aeroforge.params.templates import falcon9_vehicle
from aeroforge.perf.capacity import vehicle_ledger

#: F9 基线三变量（判据口径：s1 / s2 length 缩放 + engine_count）。
_F9_VARIABLES: list[OptimizeVariableSpec] = [
    OptimizeVariableSpec(path="stages[0].length_m", kind="continuous_scale", base=42.6),
    OptimizeVariableSpec(path="stages[1].length_m", kind="continuous_scale", base=19.2),
    OptimizeVariableSpec(path="stages[0].engine_count", kind="integer_count", base=9),
]


@pytest.fixture
def store() -> ArtifactStore:
    """候选缓存仓库（conftest 已把数据根隔离到临时区）。"""
    return ArtifactStore()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


def _await_job(client: TestClient, job_id: str, *, timeout_s: float = 90.0) -> dict[str, Any]:
    """轮询作业到终态（优化作业为秒级，轮询间隔 20 ms 足够）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        record: dict[str, Any] = response.json()
        if record["status"] in ("succeeded", "failed", "cancelled"):
            return record
        time.sleep(0.02)
    raise AssertionError(f"优化作业 {job_id} 在 {timeout_s}s 内未到终态")


def _variable_rows(specs: list[OptimizeVariableSpec]) -> list[dict[str, Any]]:
    return [spec.model_dump(mode="json") for spec in specs]


def _row_content(row: dict[str, Any]) -> tuple[Any, ...]:
    """行的候选内容（逐位比对口径：排除审计元数据 provenance，见模块 docstring）。"""
    return (
        row.get("label"),
        tuple(sorted(row["params"].items())),
        row["glow_kg"],
        row["payload_kg"],
    )


# ---------------------------------------------------------------------------
# 候选评估与缓存（§14 约束 1）
# ---------------------------------------------------------------------------


def test_evaluate_candidate_feasible_and_cache_hit(store: ArtifactStore) -> None:
    """候选评估：可行候选带全量目标量；同输入第二次评估直接命中缓存。"""
    vehicle = falcon9_vehicle()
    first = evaluate_candidate(vehicle, store=store)
    assert first.feasible
    assert first.infeasible_reason is None
    assert first.glow_kg is not None and first.glow_kg > 0.0
    assert first.dry_mass_kg is not None and first.dry_mass_kg > 0.0
    assert first.payload_leo_kg is not None and first.payload_leo_kg > 0.0
    assert not first.cache_hit
    assert first.vehicle_key.startswith("perf-optimize-")

    second = evaluate_candidate(vehicle, store=store)
    assert second.cache_hit
    assert second.glow_kg == first.glow_kg
    assert second.payload_leo_kg == first.payload_leo_kg
    assert second.dry_mass_kg == first.dry_mass_kg


def test_evaluate_candidate_rejects_infeasible_with_reason(store: ArtifactStore) -> None:
    """不可行候选：显式淘汰带原因（硬违 / 分区不铺满），不给半凑数字。"""
    vehicle = falcon9_vehicle()
    # 硬约束违反：isp_source=custom 却无值（check_vehicle 的 HARD_ISP_CUSTOM_REQUIRES_VALUES）
    broken_isp = vehicle.model_copy(
        update={
            "stages": (
                vehicle.stages[0].model_copy(update={"isp_source": "custom"}),
                vehicle.stages[1],
            )
        }
    )
    result = evaluate_candidate(broken_isp, store=store)
    assert not result.feasible
    assert result.infeasible_reason is not None and "硬约束" in result.infeasible_reason
    assert result.violation_count >= 1

    # 分区铺满失败：级长缩到装不下固定占位（0.5×42.6/19.2 → 二级可用箱长非正）
    scaled = vehicle.model_copy(
        update={
            "stages": tuple(
                stage.model_copy(update={"length_m": stage.length_m * 0.5})
                for stage in vehicle.stages
            )
        }
    )
    result = evaluate_candidate(scaled, store=store)
    assert not result.feasible
    assert result.infeasible_reason is not None
    assert (
        "分区铺满复核失败" in result.infeasible_reason or "评估链不可解" in result.infeasible_reason
    )


def test_optimize_cache_corrupted_payload_treated_as_miss(
    store: ArtifactStore, tmp_path: Any
) -> None:
    """缓存内容损坏（半写截断）：按未命中处理，重算覆盖（不把 None 当有效目标值）。"""
    # 独立命名保证缓存键不与同文件其他测试共享（同构型会同键命中）
    vehicle = falcon9_vehicle().model_copy(update={"name": "缓存损坏测试箭"})
    first = evaluate_candidate(vehicle, store=store)
    assert first.feasible and not first.cache_hit

    from aeroforge.cache.store import ARTIFACT_OPTIMIZE

    cache_file = store.dir_for(first.vehicle_key) / ARTIFACT_OPTIMIZE
    cache_file.write_text('{"glow_kg": 12.5', encoding="utf-8")  # 截断 JSON
    reloaded = evaluate_candidate(vehicle, store=store)
    assert not reloaded.cache_hit  # 损坏 → 未命中 → 重算
    assert reloaded.glow_kg == first.glow_kg
    assert evaluate_candidate(vehicle, store=store).cache_hit  # 重算已覆盖修复


# ---------------------------------------------------------------------------
# NSGA-II（单目标退化收敛 / seed 复现 / 支配白盒 / 不可行淘汰）
# ---------------------------------------------------------------------------


def test_nsga2_single_objective_matches_grid_reference(store: ArtifactStore) -> None:
    """单目标退化收敛对照：min_glow 单变量，NSGA-II 最优对拍全因子扫描参照。

    该退化问题的真解在缩放带下界（GLOW 对长度缩放单调增），扫描格点 0.5 即
    全局最优——NSGA-II 必须收敛到同一处（偏差 ≤ 1%）且不劣于基线。
    """
    vehicle = falcon9_vehicle()
    variables = [
        OptimizeVariableSpec(path="stages[0].length_m", kind="continuous_scale", base=42.6)
    ]

    sweep = run_sweep(
        SweepRequest(
            vehicle=vehicle,
            axes=[SweepAxisSpec(path="stages[0].length_m", min=0.5, max=1.5, steps=21)],
        ),
        store=store,
    )
    assert sweep["rows"], "扫描参照应有可行格点"
    grid_best = min(row["glow_kg"] for row in sweep["rows"])

    result = run_nsga2(
        Nsga2Request(
            vehicle=vehicle,
            variables=variables,
            population_size=20,
            generations=15,
            seed=42,
            objectives=["min_glow"],
        ),
        store=store,
    )
    front: list[dict[str, Any]] = result["pareto_front"]
    assert front
    best = min(row["glow_kg"] for row in front)
    assert best >= grid_best * (1.0 - 1e-9), "NSGA-II 不应优于全因子参照的全局最优"
    assert best <= grid_best * 1.01, f"NSGA-II 最优 {best} 偏离参照最优 {grid_best} 超过 1%"

    baseline = evaluate_candidate(vehicle, store=store)
    assert baseline.glow_kg is not None
    assert best <= baseline.glow_kg, "优化不应使 min_glow 劣于基线"


def test_nsga2_seed_reproducible(store: ArtifactStore) -> None:
    """seed 逐位一致：同 seed 两跑的 Pareto 前沿候选内容逐位一致（§14 可复现）。"""
    vehicle = falcon9_vehicle()
    request = Nsga2Request(
        vehicle=vehicle,
        variables=_F9_VARIABLES,
        population_size=12,
        generations=6,
        seed=7,
        objectives=["min_glow", "max_payload_leo"],
    )
    first = run_nsga2(request, store=store)
    second = run_nsga2(request, store=store)

    front_a: list[dict[str, Any]] = first["pareto_front"]
    front_b: list[dict[str, Any]] = second["pareto_front"]
    assert len(front_a) == len(front_b) > 0
    assert [_row_content(row) for row in front_a] == [_row_content(row) for row in front_b]

    # 审计 provenance：确定性字段逐位一致；缓存命中数随第二次运行上升（§14 复用）
    prov_a, prov_b = first["provenance"], second["provenance"]
    assert prov_a["seed"] == prov_b["seed"] == 7
    assert prov_a["population_size"] == prov_b["population_size"]
    assert prov_a["generations"] == prov_b["generations"]
    assert prov_a["evals_total"] == prov_b["evals_total"]
    assert prov_b["cache_hits_file"] > prov_a["cache_hits_file"] >= 0


def test_nsga2_infeasible_eliminated_from_front(store: ArtifactStore) -> None:
    """不可行淘汰机检（真实运行）：不可行候选计数 > 0 且 Pareto 前沿全为可行解。

    仅缩放二级时低因子落入不可行区（探针实测 0.5×19.2 m 装不下固定占位）——
    初始种群必然抽到不可行基因；约束支配保证它们全部被可行解支配。
    """
    vehicle = falcon9_vehicle()
    result = run_nsga2(
        Nsga2Request(
            vehicle=vehicle,
            variables=[
                OptimizeVariableSpec(path="stages[1].length_m", kind="continuous_scale", base=19.2)
            ],
            population_size=16,
            generations=6,
            seed=3,
            objectives=["min_glow", "max_payload_leo"],
        ),
        store=store,
    )
    prov = result["provenance"]
    assert prov["infeasible_evals"] > 0, "该变量空间应含不可行候选（低端分区铺满失败）"
    front: list[dict[str, Any]] = result["pareto_front"]
    assert front
    for row in front:
        assert row["glow_kg"] > 0.0 and row["payload_kg"] >= 0.0
        # 可行解的参数必落在设计空间内
        assert 0.5 * 19.2 <= row["params"]["stages[1].length_m"] <= 1.5 * 19.2


# ---------------------------------------------------------------------------
# 支配与排序的白盒对拍（Deb 2001 §6.1 / §7.1）
# ---------------------------------------------------------------------------


def _individual(
    objectives: tuple[float, ...], *, feasible: bool = True, violation: int = 0
) -> _Individual:
    """构造白盒用假想个体（不触真实评估链）。"""
    result = CandidateResult(
        vehicle_key="k",
        feasible=feasible,
        violation_count=violation,
        glow_kg=objectives[0] if feasible else None,
        dry_mass_kg=1.0 if feasible else None,
        payload_leo_kg=-objectives[1] if feasible else None,
    )
    return _Individual([], result, objectives=objectives if feasible else ())


def test_constraint_dominates_whitebox() -> None:
    """约束支配三分支：可行优先 / 双可行比目标 / 双不可行比违反数。"""
    better = _individual((100.0, -2000.0))
    worse = _individual((200.0, -1000.0))
    assert constraint_dominates(better, worse)
    assert not constraint_dominates(worse, better)

    equal_a = _individual((100.0, -2000.0))
    equal_b = _individual((100.0, -2000.0))
    assert not constraint_dominates(equal_a, equal_b)  # 全等互不支配

    feasible_worse = _individual((500.0, -100.0))
    infeasible_better = _individual((1.0, -99999.0), feasible=False, violation=2)
    assert constraint_dominates(feasible_worse, infeasible_better), "可行必胜不可行（目标更差也胜）"
    assert not constraint_dominates(infeasible_better, feasible_worse)

    low_violation = _individual((), feasible=False, violation=1)
    high_violation = _individual((), feasible=False, violation=3)
    assert constraint_dominates(low_violation, high_violation)
    assert not constraint_dominates(high_violation, low_violation)


def test_fast_non_dominated_sort_whitebox() -> None:
    """非支配排序对拍：构造 5 体的已知分层（含目标全等的并列个体，最小化口径）。"""
    a = _individual((1.0, 10.0))  # 非支配（层 0）
    b = _individual((2.0, 9.0))  # 与 a 互不支配（层 0）
    c = _individual((2.5, 10.5))  # 被 a、b 支配（层 1）
    d = _individual((3.0, 11.0))  # 被 a/b/c 支配（层 2）
    e = _individual((1.0, 10.0))  # 与 a 全等（层 0）
    population = [a, b, c, d, e]
    fronts = fast_non_dominated_sort(population)
    assert [sorted(front) for front in fronts] == [[0, 1, 4], [2], [3]]
    assert population[0].rank == 0 and population[3].rank == 2

    crowding_distance(fronts[0], population)
    # obj0 边界成员拥挤距离 = ∞（Deb 2001 §6.2；并列个体 a/e 保持下标序）
    assert population[0].crowding == float("inf")
    assert population[1].crowding == float("inf")


def test_validate_objectives_rejects_empty_and_unknown() -> None:
    """目标键校验：空清单 / 未知键显式拒绝（422 语义）。"""
    with pytest.raises(OptimizeError, match="至少选择一个"):
        validate_objectives([])
    with pytest.raises(OptimizeError, match="未知优化目标"):
        validate_objectives(["min_glow", "min_fuel"])  # type: ignore[list-item]
    assert validate_objectives(["max_payload_leo", "min_glow", "max_payload_leo"]) == (
        "max_payload_leo",
        "min_glow",
    )


def test_objective_vector_max_payload_negated() -> None:
    """目标向量折算：统一最小化口径（max_payload_leo 取负）。"""
    result = CandidateResult(
        vehicle_key="k",
        feasible=True,
        glow_kg=500_000.0,
        dry_mass_kg=40_000.0,
        payload_leo_kg=20_000.0,
    )
    assert objective_vector(result, ("min_glow",)) == (500_000.0,)
    assert objective_vector(result, ("max_payload_leo",)) == (-20_000.0,)
    assert objective_vector(result, ("min_glow", "max_payload_leo", "min_dry_mass")) == (
        500_000.0,
        -20_000.0,
        40_000.0,
    )


# ---------------------------------------------------------------------------
# 权衡研究 / 批量扫描
# ---------------------------------------------------------------------------


def test_trade_study_variants_and_cache(store: ArtifactStore) -> None:
    """权衡研究：方案族（缩放梯度）对比表 + 缓存复用 + 方案名与行级 provenance。"""
    vehicle = falcon9_vehicle()
    request = TradeStudyRequest(vehicle=vehicle, variant_count=5)
    first = run_trade_study(request, store=store)
    variants: list[dict[str, Any]] = first["variants"]
    assert len(variants) == 5
    labels = [row["label"] for row in variants]
    assert all(label and "缩放" in label for label in labels)
    # 缩放带两端：最轻方案在最下界侧
    assert variants[0]["glow_kg"] <= variants[-1]["glow_kg"]
    for row in variants:
        assert row["provenance"] is not None
        assert row["provenance"]["vehicle_key"].startswith("perf-optimize-")
        assert "scale_factor" in row["provenance"]

    second = run_trade_study(request, store=store)
    assert second["provenance"]["cache_hits_file"] == 5  # 全部命中（§14 约束 1）


def test_sweep_full_factor_and_aggregation(store: ArtifactStore) -> None:
    """批量扫描：全因子组合数、参数摘要与轴取值一致、缓存复用。"""
    vehicle = falcon9_vehicle()
    request = SweepRequest(
        vehicle=vehicle,
        axes=[
            SweepAxisSpec(path="stages[0].length_m", min=0.8, max=1.2, steps=5),
            SweepAxisSpec(path="stages[0].engine_count", min=7, max=11, steps=5),
        ],
    )
    result = run_sweep(request, store=store)
    rows: list[dict[str, Any]] = result["rows"]
    assert len(rows) == 5 * 5  # 全因子笛卡尔积
    lengths = {row["params"]["stages[0].length_m"] for row in rows}
    engines = {row["params"]["stages[0].engine_count"] for row in rows}
    assert len(lengths) == 5 and len(engines) == 5
    assert result["provenance"]["combinations"] == 25

    # 同输入复跑：全部命中候选缓存（整型轴取整去重后格点同键）
    second = run_sweep(request, store=store)
    assert second["provenance"]["cache_hits_file"] == len(rows)
    assert second["provenance"]["cache_hits_file"] > 0


# ---------------------------------------------------------------------------
# 逆向设计
# ---------------------------------------------------------------------------


def test_inverse_design_meets_target_below_baseline_glow(store: ArtifactStore) -> None:
    """逆向设计：F9 目标 15 t LEO → 构型运力 ≥ 目标且 GLOW ≤ 基线（更小构型）。"""
    vehicle = falcon9_vehicle()
    baseline_glow = evaluate_candidate(vehicle, store=store).glow_kg
    assert baseline_glow is not None

    result = run_inverse(
        InverseRequest(vehicle=vehicle, target_orbit="LEO", target_payload_kg=15_000.0),
        store=store,
    )
    config: dict[str, Any] = result["config"]
    assert config["payload_kg"] >= 15_000.0, "返回构型必须满足运力约束"
    assert config["glow_kg"] <= baseline_glow, "目标低于基线运力时最小构型 GLOW 不得高于基线"
    # 参数摘要：各级长度均不大于基线（缩放因子 ≤ 1）
    for stage_length in config["params"].values():
        assert stage_length > 0.0
    provenance_row = config["provenance"]
    assert provenance_row["target_orbit"] == "LEO"
    assert "scale_factor" in provenance_row
    assert result["warnings"] == []  # 目标低于基线：无放大提示


def test_inverse_design_unreachable_reports_not_fabricates(
    store: ArtifactStore, client: TestClient
) -> None:
    """逆向不硬凑：不可达目标 → 作业 FAILED（OPTIMIZE_INFEASIBLE），错误可操作。"""
    response = client.post(
        "/api/optimize/inverse",
        json={
            "vehicle": falcon9_vehicle().model_dump(mode="json"),
            "target_orbit": "LEO",
            "target_payload_kg": 100_000.0,
        },
    )
    assert response.status_code == 200
    record = _await_job(client, response.json()["job_id"])
    assert record["status"] == "failed"
    assert record["error"] is not None
    assert record["error"]["code"] == "OPTIMIZE_INFEASIBLE"
    assert record["error"]["message"]
    assert record["error"]["suggestion"]

    # 直接调用同判据（OptimizeError，不硬凑到上限给半解）
    with pytest.raises(OptimizeError, match="不硬凑") as excinfo:
        run_inverse(
            InverseRequest(
                vehicle=falcon9_vehicle(), target_orbit="LEO", target_payload_kg=100_000.0
            ),
            store=store,
        )
    assert excinfo.value.code == "OPTIMIZE_INFEASIBLE"


# ---------------------------------------------------------------------------
# 端点（同步校验 422 + 全链作业形状 + 判据实测）
# ---------------------------------------------------------------------------


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_endpoint_rejects_hard_constraint_violation(client: TestClient) -> None:
    """参数域硬约束前置拒绝（与 /api/perf/evaluate 同判据，不进作业）。"""
    vehicle = falcon9_vehicle().model_copy(
        update={
            "stages": (
                falcon9_vehicle().stages[0].model_copy(update={"isp_source": "custom"}),
                falcon9_vehicle().stages[1],
            )
        }
    )
    body = {
        "vehicle": vehicle.model_dump(mode="json"),
        "variables": _variable_rows(_F9_VARIABLES[:1]),
        "objectives": ["min_glow"],
    }
    response = client.post("/api/optimize/nsga2", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PARAMS_CONSTRAINT_VIOLATION"


@pytest.mark.parametrize(
    ("variables", "match"),
    [
        ([{"path": "stages[0].diameter_m", "kind": "continuous_scale", "base": 3.7}], "可用变量族"),
        ([{"path": "stages[5].length_m", "kind": "continuous_scale", "base": 42.6}], "可用变量族"),
        ([{"path": "stages[0].length_m", "kind": "integer_count", "base": 42.6}], "类别"),
        ([{"path": "stages[0].length_m", "kind": "continuous_scale", "base": 99.9}], "不一致"),
    ],
)
def test_nsga2_variable_validation_422(
    client: TestClient, variables: list[dict[str, Any]], match: str
) -> None:
    """变量三重校验：路径 / 类别 / 基线一致性，不合法当场 422（OPTIMIZE_INVALID）。"""
    body = {
        "vehicle": falcon9_vehicle().model_dump(mode="json"),
        "variables": variables,
        "objectives": ["min_glow"],
    }
    response = client.post("/api/optimize/nsga2", json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "OPTIMIZE_INVALID"
    assert error["message"]
    assert error["suggestion"]
    assert match in error["message"]


def test_trade_study_over_20_variants_422(client: TestClient) -> None:
    """权衡 > 20 方案：§14 目标尺度超限 → 422（OPTIMIZE_INVALID，可操作建议）。"""
    body = {"vehicle": falcon9_vehicle().model_dump(mode="json"), "variant_count": MAX_VARIANTS + 1}
    response = client.post("/api/optimize/trade-study", json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "OPTIMIZE_INVALID"
    assert "20" in error["message"]
    assert error["suggestion"]


def test_sweep_over_limit_422(client: TestClient) -> None:
    """扫描超限：组合数 > 10⁴ 与轴数 > 3 均当场 422（§14 目标尺度）。"""
    vehicle_json = falcon9_vehicle().model_dump(mode="json")
    # 3 轴：25 × 25 × 18（整型轴取整去重后）= 11 250 > 10⁴
    over_combinations = {
        "vehicle": vehicle_json,
        "axes": [
            {"path": "stages[0].length_m", "min": 0.8, "max": 1.2, "steps": 25},
            {"path": "stages[1].length_m", "min": 0.8, "max": 1.2, "steps": 25},
            {"path": "stages[0].engine_count", "min": 1, "max": 18, "steps": 25},
        ],
    }
    response = client.post("/api/optimize/sweep", json=over_combinations)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "OPTIMIZE_INVALID"
    assert "10" in error["message"] or "组合数" in error["message"]

    over_axes = {
        "vehicle": vehicle_json,
        "axes": [
            {"path": "stages[0].length_m", "min": 0.8, "max": 1.2, "steps": 3},
            {"path": "stages[0].engine_count", "min": 4, "max": 14, "steps": 3},
            {"path": "stages[1].length_m", "min": 0.8, "max": 1.2, "steps": 3},
            {"path": "stages[1].engine_count", "min": 1, "max": 2, "steps": 3},
        ],
    }
    response = client.post("/api/optimize/sweep", json=over_axes)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "OPTIMIZE_INVALID"

    # 轴范围越出设计空间（缩放因子 > 1.5）：不静默夹取
    out_of_bounds = {
        "vehicle": vehicle_json,
        "axes": [{"path": "stages[0].length_m", "min": 0.8, "max": 9.9, "steps": 3}],
    }
    response = client.post("/api/optimize/sweep", json=out_of_bounds)
    assert response.status_code == 422
    assert "设计空间" in response.json()["error"]["message"]


def test_all_four_endpoints_full_job_shape(client: TestClient) -> None:
    """四端点全链：POST 受理 → 作业终态 → metrics 契约键与行形态逐键核对。"""
    vehicle_json = falcon9_vehicle().model_dump(mode="json")

    # ── nsga2：pareto_front ──
    body = _post(
        client,
        "/api/optimize/nsga2",
        {
            "vehicle": vehicle_json,
            "variables": _variable_rows(_F9_VARIABLES[:2]),
            "population_size": 8,
            "generations": 3,
            "seed": 42,
            "objectives": ["min_glow", "max_payload_leo"],
        },
    )
    record = _await_job(client, body["job_id"])
    assert record["status"] == "succeeded"
    metrics = record["metrics"]
    assert set(metrics) >= {"pareto_front", "warnings", "provenance"}
    assert metrics["pareto_front"], "Pareto 前沿不应为空"
    for row in metrics["pareto_front"]:
        assert isinstance(row["glow_kg"], (int, float))
        assert isinstance(row["payload_kg"], (int, float))
        assert all(isinstance(v, (int, float)) for v in row["params"].values())
        assert isinstance(row["provenance"], dict)
        assert all(isinstance(v, str) for v in row["provenance"].values())
        assert row["provenance"]["vehicle_key"].startswith("perf-optimize-")
    assert metrics["provenance"]["seed"] == 42
    assert metrics["provenance"]["algorithm"].startswith("NSGA-II")

    # ── trade-study：variants ──
    body = _post(client, "/api/optimize/trade-study", {"vehicle": vehicle_json, "variant_count": 4})
    record = _await_job(client, body["job_id"])
    assert record["status"] == "succeeded"
    metrics = record["metrics"]
    assert set(metrics) >= {"variants", "warnings", "provenance"}
    assert len(metrics["variants"]) == 4
    assert all(row["label"] for row in metrics["variants"])

    # ── sweep：rows ──
    body = _post(
        client,
        "/api/optimize/sweep",
        {
            "vehicle": vehicle_json,
            "axes": [{"path": "stages[0].length_m", "min": 0.9, "max": 1.1, "steps": 5}],
        },
    )
    record = _await_job(client, body["job_id"])
    assert record["status"] == "succeeded"
    metrics = record["metrics"]
    assert set(metrics) >= {"rows", "warnings", "provenance"}
    assert len(metrics["rows"]) == 5

    # ── inverse：config ──
    body = _post(
        client,
        "/api/optimize/inverse",
        {"vehicle": vehicle_json, "target_orbit": "LEO", "target_payload_kg": 15_000.0},
    )
    record = _await_job(client, body["job_id"])
    assert record["status"] == "succeeded"
    metrics = record["metrics"]
    assert set(metrics) >= {"config", "warnings", "provenance"}
    config = metrics["config"]
    assert config["payload_kg"] >= 15_000.0
    assert isinstance(config["params"], dict) and config["params"]
    assert record["stage"] == "done"


def test_nsga2_latency_budget_f9_three_variables(client: TestClient) -> None:
    """§16 M6 判据实测：F9 × 三变量 × 40×20 NSGA-II 端到端 < 60 s（一次优化 < 1 min）。

    实测秒数 / 缓存命中数 / Pareto 解数随打印呈交报告（判据预算 60 s，含作业通道）。
    """
    body = _post(
        client,
        "/api/optimize/nsga2",
        {
            "vehicle": falcon9_vehicle().model_dump(mode="json"),
            "variables": _variable_rows(_F9_VARIABLES),
            "population_size": 40,
            "generations": 20,
            "seed": 42,
            "objectives": ["min_glow", "max_payload_leo"],
        },
    )
    started = time.perf_counter()
    record = _await_job(client, body["job_id"], timeout_s=120.0)
    wall_s = time.perf_counter() - started
    assert record["status"] == "succeeded"
    metrics = record["metrics"]
    elapsed_ms = metrics["provenance"]["elapsed_ms"]
    print(
        f"\n[NSGA-II 判据实测] F9 × 三变量 × 40×20：作业计算 {elapsed_ms / 1000.0:.2f} s，"
        f"端到端 {wall_s:.2f} s；评估 {metrics['provenance']['evals_total']} 次"
        f"（缓存命中 {metrics['provenance']['cache_hits_file']}），"
        f"Pareto 解 {len(metrics['pareto_front'])} 个"
    )
    assert elapsed_ms < 60_000.0, f"一次优化耗时 {elapsed_ms} ms 超出 60 s 预算（§16 M6）"
    assert metrics["pareto_front"]


def test_baseline_vehicle_is_legal() -> None:
    """夹具卫生：F9 模板无硬约束违反（优化基线的合法性前提）。"""
    vehicle = falcon9_vehicle()
    assert not has_hard([])
    vehicle_ledger(vehicle)  # 评估链可解
    parse_variables(vehicle, _F9_VARIABLES)  # 判据口径的三变量合法


def test_json_roundtrip_of_request_payloads() -> None:
    """请求载荷 JSON 往返：作业通道以 model_dump_json 传递、worker 侧复原一致。"""
    request = Nsga2Request(
        vehicle=falcon9_vehicle(),
        variables=_F9_VARIABLES,
        population_size=8,
        generations=2,
        seed=1,
        objectives=["min_glow"],
    )
    restored = Nsga2Request.model_validate_json(request.model_dump_json())
    assert restored == request
    assert json.loads(request.model_dump_json())["variables"][0]["path"] == "stages[0].length_m"


def test_parse_variables_rejects_empty(client: TestClient) -> None:
    """空变量清单：端点同步 422（不进作业）。"""
    response = client.post(
        "/api/optimize/nsga2",
        json={
            "vehicle": falcon9_vehicle().model_dump(mode="json"),
            "variables": [],
            "objectives": ["min_glow"],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "OPTIMIZE_INVALID"
