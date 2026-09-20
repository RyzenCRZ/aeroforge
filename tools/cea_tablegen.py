"""CEA 预计算表生成器（规格 §8.2 / ADR-004；调用契约 ADR-014）。

    uv run python tools/cea_tablegen.py --propellant lox_rp1 [--out data/cea] [--fast]
    uv run python tools/cea_tablegen.py --probe-solid

仅开发期运行，需要 NASA ``cea``（Apache-2.0）；表是**纯数据**（.npz），运行时只读表、
禁止实时调用 CEA（AGENTS 红线）。``data/`` 不可变、禁止手改（§15）——本工具是唯一写入方。

网格（§8.2 全分辨率）：O/F 50 点（线性 + 端点加密）、Pc 20 点对数 5–200 bar、
ε 20 点对数 5–300、Pamb 5 点（真空、0.1、0.5、1.013、5 bar）。
``--fast`` 每维 3 点，做冒烟与耗时外推，不用于交付。

**生成期门禁**（ADR-014 契约③ + R-29 表级防线，任一不过即中止、不落盘）：

1. 逐点 ``last_error == 0`` 且 Tc ∈ [200, 6000] K（拦截静默错解）；
2. 每个 (Pc, ε) 切片 Isp 对 O/F **单峰**（先增后减）——"凝相错位 / Tc 异常"类
   失效在切片上表现为双峰/锯齿，这是表级门禁；
3. 黄金锚点核对：三组 ε=40 处**插值** vs §3.2 实测，Isp 偏差 < 1% 才允许写入。

量纲（ADR-014 契约①）：``isp_vacuum_m_s`` / ``isp_ambient_m_s`` 原样存 **m/s**
（CEA 字段即有效排气速度），manifest 显式声明；转秒在运行时 ÷ g₀=9.80665。

解数组布局（实测钉死，勿凭记忆改）：
- 仅 ``supar``：[室, 喉, 出口@ε]，``Isp_vacuum[-1]`` = 出口真空 Isp；
- 加 ``pi_p``：[室, 喉, 环境点, 出口@ε]，``pi_p = Pc/Pamb``（该绑定是比值倒数语义），
  ``Isp[2]`` = 最优膨胀至 Pamb 的环境 Isp；``Isp[-1]`` 只是动量项，**不可当海平面 Isp 用**。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator

from aeroforge.paths import cea_tables_root
from aeroforge.perf import G0
from aeroforge.perf.cea_table import KNOWN_PROPELLANTS

#: 健全性门禁的 Tc 物理区间（K）。上限给足余量（最高约 3900 K），下限拦"冻结错解"。
TC_MIN_K = 200.0
TC_MAX_K = 6000.0

#: 黄金锚点 Isp 允许偏差（§3.2，任务口径 <1%）。
ANCHOR_ISP_TOL_PCT = 1.0

#: Pamb 网格（bar）：真空、0.1、0.5、1.013、5（§8.2）。
PAMB_GRID: tuple[float, ...] = (0.0, 0.1, 0.5, 1.013, 5.0)

#: O/F 端点加密偏移（相对区间长度的分数）：两端各补 5 点，加 40 线性点 = 50 点。
_OF_EDGE_FRACTIONS: tuple[float, ...] = (0.005, 0.01, 0.02, 0.04, 0.08)


class TablegenError(RuntimeError):
    """生成中止（门禁失败 / 物种缺失 / 锚点超差）。"""


@dataclass(frozen=True)
class CustomReactant:
    """ThermoDB 反应物库里没有的物种：以化学式 + 生成焓自定义（ce a ``Reactant`` 语义）。"""

    name: str
    formula: tuple[tuple[str, float], ...]
    molecular_weight: float
    dhf_kj_mol: float
    source: str


@dataclass(frozen=True)
class GoldenAnchor:
    """§3.2 黄金测试锚点（M0 实测，平衡流 iac=True，纯气相产物列表）。"""

    label: str
    of: float
    pc_bar: float
    eps: float
    isp_vacuum_m_s: float
    tc_k: float | None  # LOX/LH2 锚点未实测 Tc
    c_star_m_s: float | None


@dataclass(frozen=True)
class PropellantSpec:
    """一组推进剂的建模口径（反应物顺序 = [燃料, 氧化剂]，沿 selfcheck 模板约定）。"""

    key: str
    label: str
    reactants: tuple[str, ...]
    products: tuple[str, ...]  # 纯气相；凝相物种只允许出现在末位（ADR-014 契约②）
    reactant_t0_k: float
    of_range: tuple[float, float]
    custom_reactants: tuple[CustomReactant, ...] = field(default=())
    golden_anchors: tuple[GoldenAnchor, ...] = field(default=())

    @property
    def npz_name(self) -> str:
        return f"cea_{self.key}.npz"


# 产物列表沿 M0 selfcheck 模板（§3.2 锚点即以它实测，逐位复现）；LH2 无碳（元素表
# 必须与反应物一致——不一致会触发 cea 的 Fortran abort，进程直接死亡）。
_GAS_CH = ("C", "CO", "CO2", "H", "H2", "H2O", "O", "O2", "OH", "CH4", "C2H4", "C2H6")
_GAS_HO = ("H", "H2", "H2O", "H2O2", "HO2", "O", "O2", "OH")
_GAS_CHNO = (
    "CO",
    "CO2",
    "H",
    "H2",
    "H2O",
    "N2",
    "N",
    "NO",
    "NO2",
    "O",
    "O2",
    "OH",
    "NH3",
    "HCN",
    "CH4",
)

_SPECS: tuple[PropellantSpec, ...] = (
    PropellantSpec(
        key="lox_rp1",
        label="LOX/RP-1",
        reactants=("RP-1", "O2(L)"),
        products=_GAS_CH,
        reactant_t0_k=300.0,
        of_range=(1.0, 4.0),
        golden_anchors=(GoldenAnchor("LOX/RP-1 §3.2", 2.56, 68.9, 40.0, 3496.9, 3662.5, 1796.2),),
    ),
    PropellantSpec(
        key="lox_lh2",
        label="LOX/LH2",
        reactants=("H2", "O2(L)"),
        products=_GAS_HO,
        reactant_t0_k=20.0,  # 锚点实测口径：反应物 T0 = 20 K（液氢）
        of_range=(2.0, 8.0),
        golden_anchors=(GoldenAnchor("LOX/LH2 §3.2", 5.50, 68.0, 40.0, 4400.1, None, 2319.2),),
    ),
    PropellantSpec(
        key="lox_ch4",
        label="LOX/CH4",
        reactants=("CH4", "O2(L)"),
        products=_GAS_CH,
        reactant_t0_k=300.0,
        of_range=(1.5, 4.5),
        golden_anchors=(GoldenAnchor("LOX/CH4 §3.2", 3.40, 100.0, 40.0, 3651.8, 3629.2, 1859.9),),
    ),
    PropellantSpec(
        key="n2o4_mmh",
        label="N2O4/MMH",
        reactants=("MMH", "N2O4(L)"),
        products=_GAS_CHNO,
        reactant_t0_k=300.0,
        of_range=(0.8, 3.5),
        custom_reactants=(
            # MMH 不在本版 ThermoDB 反应物库（实测确认），以化学式 + 生成焓自定义。
            # ΔHf(MMH, l) ≈ +54.2 kJ/mol：公开热化学数据常用值（工程惯例，非权威）。
            CustomReactant(
                "MMH", (("C", 1), ("H", 6), ("N", 2)), 46.07, 54.2, "公开热化学数据常用值"
            ),
        ),
    ),
)

_SPEC_BY_KEY: dict[str, PropellantSpec] = {s.key: s for s in _SPECS}


def _check_registry_consistency() -> None:
    """运行时表键集与本生成器注册表必须一致（两处各写一半即立即失败）。"""
    if frozenset(_SPEC_BY_KEY) != frozenset(KNOWN_PROPELLANTS):
        msg = (
            f"生成器注册表 {sorted(_SPEC_BY_KEY)} 与 "
            f"perf.cea_table.KNOWN_PROPELLANTS {sorted(KNOWN_PROPELLANTS)} 不一致"
        )
        raise TablegenError(msg)


def of_grid(lo: float, hi: float, *, fast: bool) -> np.ndarray:
    """O/F 网格：40 线性点 + 两端各 5 点加密 = 50 点（§8.2"线性 + 端点加密"）。"""
    if fast:
        return np.array([lo, (lo + hi) / 2.0, hi])
    linear = np.linspace(lo, hi, 40)
    span = hi - lo
    edges = np.concatenate(
        [lo + span * np.asarray(_OF_EDGE_FRACTIONS), hi - span * np.asarray(_OF_EDGE_FRACTIONS)]
    )
    grid = np.unique(np.concatenate([linear, edges]))
    if grid.size != 50:
        msg = f"O/F 网格构造出 {grid.size} 点（应为 50）：区间 [{lo}, {hi}]"
        raise TablegenError(msg)
    return grid


def log_grid(lo: float, hi: float, count: int, *, fast: bool) -> np.ndarray:
    """对数网格（Pc 20 点 5–200 bar；ε 20 点 5–300）。"""
    if fast:
        return np.geomspace(lo, hi, 3)
    return np.geomspace(lo, hi, count)


@dataclass
class _SolverBundle:
    """跨点复用的 CEA 对象（混合物 / 求解器只建一次，权重逐点变）。"""

    reactants: object  # cea.Mixture
    products: object  # cea.Mixture
    solver: object  # cea.RocketSolver
    fuel_weights: np.ndarray
    oxid_weights: np.ndarray


def _make_bundle(spec: PropellantSpec) -> _SolverBundle:
    import cea

    species: list[object] = []
    for name in spec.reactants:
        custom = next((c for c in spec.custom_reactants if c.name == name), None)
        if custom is not None:
            species.append(
                cea.Reactant(
                    custom.name,
                    dict(custom.formula),
                    custom.molecular_weight,
                    custom.dhf_kj_mol,
                    "kJ/mol",
                )
            )
        else:
            species.append(name)

    reactants = cea.Mixture(species)
    products = cea.Mixture(list(spec.products))
    solver = cea.RocketSolver(products, reactants=reactants)
    n = len(spec.reactants)
    fuel_weights = reactants.moles_to_weights(np.eye(n)[0])
    oxid_weights = reactants.moles_to_weights(np.eye(n)[1])
    return _SolverBundle(reactants, products, solver, fuel_weights, oxid_weights)


def _solve_point(
    spec: PropellantSpec,
    bundle: _SolverBundle,
    of: float,
    pc_bar: float,
    eps: float,
    pamb_bar: float,
) -> tuple[np.ndarray, float]:
    """解一点，返回 (6 字段向量, 环境比冲)。含 ADR-014 契约③健全性门禁。

    环境比冲：``pamb_bar == 0`` → 与真空 Isp 恒等；``pamb_bar >= pc_bar`` → NaN
    （物理无解角点，生成器登记数量、运行时按 NaN 透传）。
    """
    import cea

    weights = bundle.reactants.of_ratio_to_weights(bundle.oxid_weights, bundle.fuel_weights, of)
    # 契约：RocketSolver 必须提供 hc 或 tc；hc = 反应物总焓 / R
    hc = bundle.reactants.calc_property(cea.ENTHALPY, weights, spec.reactant_t0_k) / cea.R

    solution = cea.RocketSolution(bundle.solver)
    if 0.0 < pamb_bar < pc_bar:
        bundle.solver.solve(solution, weights, pc_bar, supar=eps, hc=hc, pi_p=pc_bar / pamb_bar)
    else:
        bundle.solver.solve(solution, weights, pc_bar, supar=eps, hc=hc)

    # ADR-014 契约③：last_error 不充分，必须叠加 Tc 物理区间断言，任一不过即判失败
    tc = float(np.asarray(solution.T).ravel()[0])
    if solution.last_error != 0:
        msg = (
            f"求解失败：{spec.key} O/F={of:g} Pc={pc_bar:g} ε={eps:g} "
            f"last_error={solution.last_error}"
        )
        raise TablegenError(msg)
    if not TC_MIN_K <= tc <= TC_MAX_K:
        msg = (
            f"Tc 健全性门禁失败（R-29 静默错解特征）：{spec.key} O/F={of:g} Pc={pc_bar:g} "
            f"ε={eps:g} Pamb={pamb_bar:g} → Tc={tc:.1f} K ∉ [{TC_MIN_K:.0f}, {TC_MAX_K:.0f}] K"
        )
        raise TablegenError(msg)

    isp_vacuum = float(np.asarray(solution.Isp_vacuum).ravel()[-1])
    if pamb_bar == 0.0:
        isp_ambient = isp_vacuum
    elif pamb_bar >= pc_bar:
        isp_ambient = float("nan")
    else:
        # 布局 [室, 喉, 环境点, 出口@ε]：环境点在索引 2（实测钉死）
        isp_ambient = float(np.asarray(solution.Isp).ravel()[2])

    fields = np.array(
        [
            isp_vacuum,
            float(np.asarray(solution.c_star).ravel()[0]),
            tc,
            float(np.asarray(solution.gamma_s).ravel()[0]),
            float(np.asarray(solution.MW).ravel()[0]),
            float(np.asarray(solution.density).ravel()[0]),
        ]
    )
    if not np.isfinite(fields).all():
        msg = f"字段含非有限值：{spec.key} O/F={of:g} Pc={pc_bar:g} ε={eps:g} → {fields}"
        raise TablegenError(msg)
    return fields, isp_ambient


def _assert_unimodal(values: np.ndarray, where: str) -> None:
    """Isp 对 O/F 单峰门禁：先增后减；容差只吸收求解放敛级数值噪声。"""
    peak = int(np.argmax(values))
    tol = 2e-4 * float(values[peak])
    if (np.diff(values[: peak + 1]) < -tol).any() or (np.diff(values[peak:]) > tol).any():
        msg = (
            f"单峰性门禁失败（凝相错位 / Tc 异常类静默失效的表级特征）：{where}\n"
            f"  Isp 序列 = {np.round(values, 1).tolist()}"
        )
        raise TablegenError(msg)


def _check_anchors(
    spec: PropellantSpec,
    of: np.ndarray,
    pc: np.ndarray,
    eps: np.ndarray,
    grid_values: np.ndarray,
) -> dict[str, object]:
    """黄金锚点核对：在生成表上做**插值**（非直接读点）对比 §3.2 实测。"""
    interpolator = RegularGridInterpolator(
        (of, np.log10(pc), np.log10(eps)), grid_values, method="linear", bounds_error=True
    )
    records: list[dict[str, object]] = []
    passed = True
    for anchor in spec.golden_anchors:
        values = np.asarray(
            interpolator(np.array([anchor.of, np.log10(anchor.pc_bar), np.log10(anchor.eps)]))
        ).ravel()
        isp = float(values[0])
        isp_dev_pct = abs(isp - anchor.isp_vacuum_m_s) / anchor.isp_vacuum_m_s * 100.0
        passed = passed and isp_dev_pct < ANCHOR_ISP_TOL_PCT
        record: dict[str, object] = {
            "label": anchor.label,
            "of": anchor.of,
            "pc_bar": anchor.pc_bar,
            "eps": anchor.eps,
            "isp_vacuum_ref_m_s": anchor.isp_vacuum_m_s,
            "isp_vacuum_interp_m_s": round(isp, 3),
            "isp_dev_pct": round(isp_dev_pct, 4),
            "isp_vacuum_ref_s": round(anchor.isp_vacuum_m_s / G0, 2),
        }
        if anchor.tc_k is not None:
            tc = float(values[2])
            record["tc_ref_k"] = anchor.tc_k
            record["tc_interp_k"] = round(tc, 2)
            record["tc_dev_pct"] = round(abs(tc - anchor.tc_k) / anchor.tc_k * 100.0, 4)
        if anchor.c_star_m_s is not None:
            cstar = float(values[1])
            record["c_star_ref_m_s"] = anchor.c_star_m_s
            record["c_star_interp_m_s"] = round(cstar, 3)
            record["c_star_dev_pct"] = round(
                abs(cstar - anchor.c_star_m_s) / anchor.c_star_m_s * 100.0, 4
            )
        records.append(record)
    return {
        "spec": "AeroForge-Spec.md §3.2 黄金测试锚点",
        "tolerance_pct": ANCHOR_ISP_TOL_PCT,
        "passed": passed,
        "anchors": records,
    }


def generate(spec: PropellantSpec, out_dir: Path, *, fast: bool) -> int:
    """生成单组推进剂表并写 manifest；返回进程退出码。"""
    import cea

    of = of_grid(*spec.of_range, fast=fast)
    pc = log_grid(5.0, 200.0, 20, fast=fast)
    eps = log_grid(5.0, 300.0, 20, fast=fast)
    pamb = np.asarray(PAMB_GRID, dtype=float)
    n_of, n_pc, n_eps, n_pamb = of.size, pc.size, eps.size, pamb.size
    total_points = n_of * n_pc * n_eps
    total_calls = total_points * n_pamb
    print(
        f"[{spec.key}] 网格 O/F={n_of} × Pc={n_pc} × ε={n_eps} × Pamb={n_pamb}"
        f" → {total_points} 节点 / 约 {total_calls} 次求解"
        f"{'（--fast 冒烟）' if fast else ''}",
        flush=True,
    )

    bundle = _make_bundle(spec)
    grid_values = np.empty((n_of, n_pc, n_eps, 6))
    ambient_values = np.empty((n_of, n_pc, n_eps, n_pamb))
    degenerate = 0
    started = time.perf_counter()
    solved = 0

    for ip in range(n_pc):
        for ie in range(n_eps):
            vac = np.empty((n_of, 6))
            amb = np.full((n_of, n_pamb), np.nan)
            for io in range(n_of):
                fields, _ = _solve_point(
                    spec, bundle, float(of[io]), float(pc[ip]), float(eps[ie]), 0.0
                )
                vac[io] = fields
                amb[io, 0] = fields[0]  # Pamb=0：与真空 Isp 恒等
                solved += 1
            for ia in range(1, n_pamb):
                p_amb = float(pamb[ia])
                if p_amb >= float(pc[ip]):
                    degenerate += n_of  # 环境压 ≥ 室压：物理无解角点，登记为 NaN
                    continue
                for io in range(n_of):
                    _, isp_amb = _solve_point(
                        spec, bundle, float(of[io]), float(pc[ip]), float(eps[ie]), p_amb
                    )
                    amb[io, ia] = isp_amb
                    solved += 1
            # 表级门禁：每个 (Pc, ε) 切片 Isp 对 O/F 单峰（真空 + 各环境压）
            _assert_unimodal(vac[:, 0], f"{spec.label} Pc={pc[ip]:g} bar ε={eps[ie]:g} Isp_vac")
            for ia in range(1, n_pamb):
                column = amb[:, ia]
                if np.isfinite(column).any():
                    _assert_unimodal(
                        column,
                        f"{spec.label} Pc={pc[ip]:g} bar ε={eps[ie]:g}"
                        f" Pamb={pamb[ia]:g} bar Isp_amb",
                    )
            grid_values[:, ip, ie, :] = vac
            ambient_values[:, ip, ie, :] = amb

        elapsed = time.perf_counter() - started
        rate = solved / elapsed if elapsed > 0 else 0.0
        remaining = (total_calls - solved) / rate if rate > 0 else 0.0
        rate_ms = 1000.0 / rate if rate > 0 else 0.0
        print(
            f"[{spec.key}] Pc 行 {ip + 1}/{n_pc}（{pc[ip]:g} bar）"
            f" 已 {solved} 次求解 / {elapsed:.1f} s，均 {rate_ms:.2f} ms/次，"
            f"预计剩余 {remaining:.0f} s",
            flush=True,
        )

    anchor_report = _check_anchors(spec, of, pc, eps, grid_values)
    if fast:
        print(f"[{spec.key}] --fast 网格过粗，跳过黄金锚点核对", flush=True)
    else:
        if not bool(anchor_report["passed"]):
            print(
                f"[{spec.key}] 黄金锚点核对失败：{json.dumps(anchor_report, ensure_ascii=False)}",
                flush=True,
            )
            return 1
        for record in anchor_report["anchors"]:
            print(
                f"[{spec.key}] 锚点 {record['label']}:"
                f" Isp 插值 {record['isp_vacuum_interp_m_s']} m/s"
                f" vs 实测 {record['isp_vacuum_ref_m_s']} m/s（偏差 {record['isp_dev_pct']}%）",
                flush=True,
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    npz_path = out_dir / spec.npz_name
    np.savez_compressed(
        npz_path,
        of=of,
        pc_bar=pc,
        eps=eps,
        pamb_bar=pamb,
        **{
            name: grid_values[:, :, :, i]
            for i, name in enumerate(
                (
                    "isp_vacuum_m_s",
                    "c_star_m_s",
                    "t_chamber_k",
                    "gamma",
                    "mw",
                    "chamber_density_kg_m3",
                )
            )
        },
        isp_ambient_m_s=ambient_values,
    )
    sha256 = hashlib.sha256(npz_path.read_bytes()).hexdigest()

    manifest_path = out_dir / "manifest.json"
    manifest: dict[str, object] = {"version": 1}
    if manifest_path.is_file():
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            manifest = loaded
    manifest["version"] = 1
    manifest["g0_m_s2"] = G0
    manifest["generated_by"] = "tools/cea_tablegen.py"
    manifest.setdefault("spec_refs", "AeroForge-Spec.md §8.2 / §8.3 / §3.2；ADR-004 / ADR-014")
    tables = manifest.setdefault("tables", {})
    assert isinstance(tables, dict)
    isp_unit = "m/s（CEA 有效排气速度，ADR-014 契约①；转秒 ÷ g0）"
    tables[spec.key] = {
        "propellant": spec.key,
        "label": spec.label,
        "reactants": list(spec.reactants),
        "custom_reactants": [
            {
                "name": c.name,
                "formula": dict(c.formula),
                "mw": c.molecular_weight,
                "dhf_kj_mol": c.dhf_kj_mol,
                "source": c.source,
            }
            for c in spec.custom_reactants
        ],
        "products_gas_phase": list(spec.products),
        "reactant_t0_k": spec.reactant_t0_k,
        "cea_version": cea.__version__,
        "file": spec.npz_name,
        "sha256": sha256,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "grid": {
            "of": {
                "count": n_of,
                "min": float(of[0]),
                "max": float(of[-1]),
                "mode": "linear+端点加密",
            },
            "pc_bar": {"count": n_pc, "min": float(pc[0]), "max": float(pc[-1]), "mode": "log10"},
            "eps": {"count": n_eps, "min": float(eps[0]), "max": float(eps[-1]), "mode": "log10"},
            "pamb_bar": {
                "count": n_pamb,
                "values": [float(x) for x in pamb],
                "mode": "linear（含真空 0）",
            },
            "points": int(total_points),
        },
        "fields": {
            "isp_vacuum_m_s": {"unit": isp_unit, "dims": ["of", "pc_bar", "eps"]},
            "c_star_m_s": {"unit": "m/s", "dims": ["of", "pc_bar", "eps"]},
            "t_chamber_k": {"unit": "K", "dims": ["of", "pc_bar", "eps"]},
            "gamma": {"unit": "无量纲（燃烧室 γ_s）", "dims": ["of", "pc_bar", "eps"]},
            "mw": {"unit": "g/mol（燃烧室燃气）", "dims": ["of", "pc_bar", "eps"]},
            "chamber_density_kg_m3": {
                "unit": "kg/m³（燃烧室燃气）",
                "dims": ["of", "pc_bar", "eps"],
            },
            "isp_ambient_m_s": {
                "unit": isp_unit,
                "dims": ["of", "pc_bar", "eps", "pamb_bar"],
                "note": "CEA 环境点 = 最优膨胀至 Pamb 的 Isp_opt；Pamb≥Pc 角点为 NaN（物理无解）",
            },
        },
        "gates": {
            "last_error_and_tc": (
                f"全部 {solved} 次求解 last_error=0 且 Tc∈[{TC_MIN_K:.0f}, {TC_MAX_K:.0f}] K"
            ),
            "unimodal_of_slices": "全部 (Pc, ε) 切片 Isp 对 O/F 单峰（含各环境压）",
            "degenerate_ambient_points": degenerate,
        },
        "golden_anchor_check": anchor_report,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    size_mb = npz_path.stat().st_size / 1e6
    print(
        f"[{spec.key}] 完成：{npz_path}（{size_mb:.2f} MB，sha256 前 12 位 {sha256[:12]}…）",
        flush=True,
    )
    return 0


def probe_solid() -> int:
    """固体探针：AP/Al/PBAN 单点验证 CEA 调用与"凝相物种末位"纪律（§8.2 固体表留白，不生成）。"""
    import cea

    print(
        "固体探针：AP/Al/PBAN（质量分数 0.70 / 0.16 / 0.14），Pc=70 bar，ε=10，T0=300 K", flush=True
    )
    # AP、PBAN 不在 ThermoDB 反应物库（实测确认），以化学式 + 生成焓自定义：
    # AP(s) ΔHf = -295.77 kJ/mol（公开热化学数据标准值）；
    # PBAN ΔHf 文献存在分歧，取 -875 kJ/kg 作常用值并做敏感性对照。
    ap = cea.Reactant("AP", {"N": 1, "H": 4, "CL": 1, "O": 4}, 117.49, -295.77, "kJ/mol")
    products = (
        "CO",
        "CO2",
        "H2",
        "H2O",
        "N2",
        "HCL",
        "OH",
        "H",
        "O",
        "O2",
        "N",
        "CL",
        "CL2",
        "AL",
        "AL2O",
        "ALO",
        "ALO2",
        "ALOCL",
        "ALOH",
        "AL(OH)2",
        "ALH",
        "ALH2",
        "ALCL",
        "ALCL2",
        "AL2CL6",
        "ALN",
        "AL2O3",
        "AL2O3(L)",  # 凝相物种只允许末位（ADR-014 契约②）
    )
    for dhf_pban in (-875.0, -2650.0):
        pban = cea.Reactant(
            "PBAN", {"C": 6.5, "H": 7.5, "N": 1, "O": 1.5}, 123.64, dhf_pban, "kJ/kg"
        )
        reactants = cea.Mixture([pban, ap, "AL"])
        mixture = cea.Mixture(list(products))
        weights = np.array([0.14, 0.70, 0.16])
        hc = reactants.calc_property(cea.ENTHALPY, weights, 300.0) / cea.R
        solver = cea.RocketSolver(mixture, reactants=reactants)
        started = time.perf_counter()
        solution = cea.RocketSolution(solver)
        solver.solve(solution, weights, 70.0, supar=10.0, hc=hc)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        tc = float(np.asarray(solution.T).ravel()[0])
        isp = float(np.asarray(solution.Isp_vacuum).ravel()[-1])
        cstar = float(np.asarray(solution.c_star).ravel()[0])
        ok = solution.last_error == 0 and TC_MIN_K <= tc <= TC_MAX_K
        print(
            f"  ΔHf(PBAN)={dhf_pban:g} kJ/kg → last_error={solution.last_error}"
            f" Tc={tc:.1f} K c*={cstar:.1f} m/s Isp_vac(ε=10)={isp:.1f} m/s = {isp / G0:.1f} s"
            f"（{elapsed_ms:.1f} ms）门禁{'通过' if ok else '失败'}",
            flush=True,
        )
        if not ok:
            return 1
    print(
        "结论：CEA 调用与凝相末位纪律在固体配方上成立；全固体表按 M4 第一片口径留白。", flush=True
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；返回进程退出码。"""
    parser = argparse.ArgumentParser(description="CEA 预计算表生成器（§8.2 / ADR-004 / ADR-014）")
    parser.add_argument("--propellant", choices=sorted(_SPEC_BY_KEY), help="推进剂组合键")
    parser.add_argument("--out", type=Path, default=None, help="输出目录（缺省 data/cea，§15）")
    parser.add_argument(
        "--fast", action="store_true", help="每维 3 点冒烟网格（耗时估算用，不作交付）"
    )
    parser.add_argument("--probe-solid", action="store_true", help="固体配方单点探针（不生成表）")
    args = parser.parse_args(argv)

    _check_registry_consistency()

    if args.probe_solid:
        return probe_solid()
    if args.propellant is None:
        parser.error("需要 --propellant（或 --probe-solid）")

    out_dir = args.out if args.out is not None else cea_tables_root()
    try:
        return generate(_SPEC_BY_KEY[args.propellant], out_dir, fast=args.fast)
    except TablegenError as exc:
        print(f"表生成中止：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
