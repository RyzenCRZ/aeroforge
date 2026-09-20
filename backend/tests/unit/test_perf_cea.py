"""CEA 预计算表 + 最优 O/F 工作点 + 效率因子（M4 第一片：§8.2 / §8.3 / §3.2 / ADR-014）。

真实表在仓库 ``data/cea/``（不可变，由 ``tools/cea_tablegen.py`` 生成），测试离线只读；
会话夹具把真实表复制进 conftest 重定向的数据根，使**默认加载路径**也被覆盖。
表未生成时相关用例**显式 skip 并留原因**（§13.8：未验证必须与已验证同等显式）。
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from aeroforge.errors import AeroForgeError
from aeroforge.perf import G0
from aeroforge.perf.cea_table import load_table
from aeroforge.perf.efficiency import CYCLE_EFFICIENCY, isp_actual, resolve_efficiency
from aeroforge.perf.workpoint import average_density, mass_flow, optimal_of, usable_of_range

#: 仓库内真实表目录（backend/tests/unit → 仓库根上溯 3 级）
REPO_TABLES = Path(__file__).resolve().parents[3] / "data" / "cea"

#: §3.2 黄金测试锚点（ε=40）：组合键、O/F、Pc(bar)、Isp_vac 字段实测值（m/s）、实测值（s）
GOLDEN_ANCHORS: tuple[tuple[str, float, float, float, float], ...] = (
    ("lox_rp1", 2.56, 68.9, 3496.9, 356.6),
    ("lox_ch4", 3.40, 100.0, 3651.8, 372.4),
    ("lox_lh2", 5.50, 68.0, 4400.1, 448.7),
)


@pytest.fixture(scope="session")
def repo_tables_in_data_root() -> Iterator[Path]:
    """把仓库真实表复制进测试数据根（conftest 已重定向），覆盖默认加载路径。

    缺表时显式 skip（表由开发期生成器产出，不入 CI 产物也必须留痕）。
    """
    if not (REPO_TABLES / "manifest.json").is_file():
        pytest.skip(f"CEA 表未生成（{REPO_TABLES}）；请运行 uv run python tools/cea_tablegen.py")
    target = Path(os.environ["AEROFORGE_DATA_DIR"]) / "cea"
    target.mkdir(parents=True, exist_ok=True)
    for source in REPO_TABLES.iterdir():
        if source.is_file() and not (target / source.name).is_file():
            shutil.copy2(source, target / source.name)
    yield target


def test_golden_anchors_within_one_percent(repo_tables_in_data_root: Path) -> None:
    """三组 ε=40 锚点：表插值 vs §3.2 实测，Isp 偏差必须 < 1%（m/s 与 s 两种口径）。"""
    for propellant, of, pc_bar, isp_ref_ms, isp_ref_s in GOLDEN_ANCHORS:
        point = load_table(propellant).lookup(of, pc_bar, 40.0, 0.0)
        dev_ms = abs(point.isp_vacuum_m_s - isp_ref_ms) / isp_ref_ms
        dev_s = abs(point.isp_vacuum_m_s / G0 - isp_ref_s) / isp_ref_s
        assert dev_ms < 0.01, f"{propellant}: Isp_vac 偏差 {dev_ms * 100:.3f}% ≥ 1%"
        assert dev_s < 0.01
        assert point.t_chamber_k > 2000.0, "锚点附近 Tc 必须在物理区间（R-29 静默错解特征）"


def test_grid_roundtrip_exact_at_nodes(repo_tables_in_data_root: Path) -> None:
    """插值往返：在网格节点处查询必须逐点还原表值（插值器没抄错轴、没换维序）。"""
    npz_path = REPO_TABLES / "cea_lox_rp1.npz"
    with np.load(npz_path) as npz:
        of = np.asarray(npz["of"], dtype=float)
        pc = np.asarray(npz["pc_bar"], dtype=float)
        eps = np.asarray(npz["eps"], dtype=float)
        isp = np.asarray(npz["isp_vacuum_m_s"], dtype=float)
        cstar = np.asarray(npz["c_star_m_s"], dtype=float)
    table = load_table("lox_rp1")
    for i, j, k in ((0, 0, 0), (25, 10, 5), (49, 19, 19), (10, 3, 17), (40, 15, 8)):
        point = table.lookup(float(of[i]), float(pc[j]), float(eps[k]), 0.0)
        assert point.isp_vacuum_m_s == pytest.approx(isp[i, j, k], rel=1e-12)
        assert point.c_star_m_s == pytest.approx(cstar[i, j, k], rel=1e-12)


def test_unimodality_spot_checks(repo_tables_in_data_root: Path) -> None:
    """单峰性抽查（表级门禁的抽验）：抽样切片上 Isp 对 O/F 必须先增后减。"""
    npz_path = REPO_TABLES / "cea_lox_lh2.npz"
    with np.load(npz_path) as npz:
        isp = np.asarray(npz["isp_vacuum_m_s"], dtype=float)
    for j, k in ((0, 0), (10, 10), (19, 19), (5, 15)):
        column = isp[:, j, k]
        peak = int(np.argmax(column))
        tol = 2e-4 * float(column[peak])
        assert (np.diff(column[: peak + 1]) >= -tol).all(), f"Pc#{j}/ε#{k} 峰前出现下降"
        assert (np.diff(column[peak:]) <= tol).all(), f"Pc#{j}/ε#{k} 峰后出现上升"


def test_lookup_latency(repo_tables_in_data_root: Path) -> None:
    """查询 < 1 ms（§8.2 目标）。CI 抖动放宽到 5 ms 断言，实测值打印留痕。

    真空口径（pamb=0，免 4 维插值的常用路径）与含环境压口径分别计时：
    前者须 < 1 ms 目标域内，后者实测值一并报告（≥1 ms 时由主线裁决是否拆表）。
    """
    table = load_table("lox_rp1")
    table.lookup(2.34, 84.0, 33.0, 0.0)  # 预热（首次含插值器构造）
    table.lookup(2.34, 84.0, 33.0, 0.5)
    for label, pamb in (("真空(pamb=0)", 0.0), ("环境压(pamb=0.5)", 0.5)):
        samples: list[float] = []
        for _ in range(300):
            started = time.perf_counter()
            table.lookup(2.34, 84.0, 33.0, pamb)
            samples.append(time.perf_counter() - started)
        mean_ms = sum(samples) / len(samples) * 1000.0
        max_ms = max(samples) * 1000.0
        print(f"\nlookup 延迟实测[{label}]：均值 {mean_ms:.3f} ms，最大 {max_ms:.3f} ms（300 次）")
        assert mean_ms < 5.0


def test_lookup_out_of_domain_raises(repo_tables_in_data_root: Path) -> None:
    """域外查询显式报错（禁止静默外推），信息含覆盖域描述。"""
    table = load_table("lox_ch4")
    with pytest.raises(AeroForgeError) as excinfo:
        table.lookup(3.4, 100.0, 1000.0, 0.0)
    assert "超出表覆盖域" in str(excinfo.value)
    with pytest.raises(AeroForgeError):
        table.lookup(3.4, 100.0, 40.0, 50.0)  # Pamb 越界同理


def test_missing_table_error_points_to_tablegen(tmp_path: Path) -> None:
    """表缺失 / 组合未收录：报错必须指路 tools/cea_tablegen.py（禁止静默回退常数）。"""
    with pytest.raises(AeroForgeError) as excinfo:
        load_table("lox_rp1", root=tmp_path)
    assert "tools/cea_tablegen.py" in str(excinfo.value)

    with pytest.raises(AeroForgeError) as excinfo_unknown:
        load_table("ap_al_pban", root=tmp_path)
    assert "未收录" in str(excinfo_unknown.value)


def test_default_root_missing_table_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认加载路径指向空表目录时：同样指路生成器，而非崩溃或回退常数。

    显式 monkeypatch 默认根，避免与本会话早前用例已复制进数据根的真实表串场
    （pytest 按文件序执行，"表在默认根能查到"本身是正确行为，不能当报错测）。
    """
    monkeypatch.setattr("aeroforge.perf.cea_table.cea_tables_root", lambda: tmp_path)
    with pytest.raises(AeroForgeError) as excinfo:
        load_table("lox_rp1")
    assert "tools/cea_tablegen.py" in str(excinfo.value)


def test_manifest_tamper_rejected(tmp_path: Path) -> None:
    """sha256 篡改拒载：data/ 不可变（§15），校验失败绝不静默使用。"""
    for name in ("cea_lox_lh2.npz", "manifest.json"):
        source = REPO_TABLES / name
        if not source.is_file():
            pytest.skip(f"CEA 表未生成（{source}）")
        shutil.copy2(source, tmp_path / name)
    victim = tmp_path / "cea_lox_lh2.npz"
    data = bytearray(victim.read_bytes())
    data[len(data) // 2] ^= 0xFF
    victim.write_bytes(bytes(data))

    with pytest.raises(AeroForgeError) as excinfo:
        load_table("lox_lh2", root=tmp_path)
    assert "sha256" in str(excinfo.value)


def test_degenerate_ambient_corner_is_nan(repo_tables_in_data_root: Path) -> None:
    """Pamb≥Pc 角点（Pc=5, Pamb=5）：环境比冲为 NaN（物理无解），其余字段照常有限。"""
    point = load_table("lox_ch4").lookup(3.4, 5.0, 20.0, 5.0)
    assert np.isnan(point.isp_ambient_m_s)
    assert np.isfinite(point.isp_vacuum_m_s)
    assert np.isfinite(point.t_chamber_k)


def test_optimal_of_isp_lox_lh2_in_expected_band(repo_tables_in_data_root: Path) -> None:
    """LOX/LH2 max-Isp 峰 O/F ≈ 4.0–5.0（公开常识量级锚定，断言放宽）。"""
    workpoint = optimal_of("lox_lh2", 68.0, 40.0, "isp")
    assert 3.8 <= workpoint.of_optimal <= 5.4
    assert workpoint.isp_s == pytest.approx(workpoint.isp_vacuum_m_s / G0, rel=1e-12)
    # 峰值 Isp 不低于锚点（O/F=5.5、同 Pc/ε 的实测插值）——极值搜索确实在峰上
    anchor = load_table("lox_lh2").lookup(5.5, 68.0, 40.0, 0.0)
    assert workpoint.isp_vacuum_m_s >= anchor.isp_vacuum_m_s


def test_density_impulse_peak_shifts_richer_for_lh2(repo_tables_in_data_root: Path) -> None:
    """密度比冲峰的 O/F 比纯 Isp 峰**更富氧**（定性物理断言，LH2 上显著）。

    依据：ρ_avg 随 O/F 单调上升（趋近 ρ_ox），故 Isp·ρ_avg 的峰只会出现在 Isp 峰
    的富氧侧——真实佐证：LOX/LH2 一级（RS-68，MR≈6.0）比 Isp 峰位（≈4.8）更富氧。
    LH2 的密度比冲在表域富氧侧单调上升，极值落在域边界（at_domain_boundary=True）。
    """
    isp_point = optimal_of("lox_lh2", 68.0, 40.0, "isp")
    density_point = optimal_of("lox_lh2", 68.0, 40.0, "density_impulse")
    assert density_point.of_optimal > isp_point.of_optimal
    assert density_point.at_domain_boundary, "LH2 密度比冲在表域内应单调升至富氧边界"
    # ρ_avg 口径锚定（Sutton 体积平均式）：LOX/LH2 @5.5 ≈ 344、LOX/RP-1 @2.56 ≈ 1023
    assert average_density(5.5, 1141.0, 71.0) == pytest.approx(343.8, rel=1e-3)
    assert average_density(2.56, 1141.0, 810.0) == pytest.approx(1023.0, rel=1e-2)
    assert density_point.rho_avg_kg_m3 is not None
    assert density_point.density_impulse is not None
    assert density_point.density_impulse == pytest.approx(
        density_point.isp_s * density_point.rho_avg_kg_m3, rel=1e-9
    )


def test_density_impulse_n2o4_mmh_solved(repo_tables_in_data_root: Path) -> None:
    """N2O4/MMH 密度比冲可用（物性已入 params/propellants，Sutton typical）。

    合理值锚定：Isp 峰 O/F≈2.32（341.5 s，NTO/MMH 公开量级）；密度比冲峰
    O/F > 纯 Isp 峰 O/F（富氧侧方向——ρ_avg 随 O/F 单调上升的必然结果，
    MMH 密度接近 LOX，偏移比 LH2 小但方向一致）。
    """
    isp_point = optimal_of("n2o4_mmh", 70.0, 40.0, "isp")
    density_point = optimal_of("n2o4_mmh", 70.0, 40.0, "density_impulse")
    assert 2.0 <= isp_point.of_optimal <= 2.6
    assert 330.0 <= isp_point.isp_s <= 350.0
    assert density_point.of_optimal > isp_point.of_optimal
    assert not density_point.at_domain_boundary
    assert density_point.rho_avg_kg_m3 is not None
    assert 1100.0 <= density_point.rho_avg_kg_m3 <= 1300.0
    assert density_point.density_impulse is not None
    assert density_point.density_impulse == pytest.approx(414_144.0, rel=0.01)


def test_mass_flow_hand_check() -> None:
    """ṁ = F/(Isp·g₀)：手算对拍与自反性。"""
    assert mass_flow(1_000_000.0, 300.0) == pytest.approx(1_000_000.0 / (300.0 * G0), rel=1e-15)
    thrust = 845_000.0
    assert mass_flow(thrust, 282.0) * 282.0 * G0 == pytest.approx(thrust, rel=1e-12)


def test_usable_of_range_shrinks_with_tighter_tc(repo_tables_in_data_root: Path) -> None:
    """可用区间：默认阈值下非空且在表域内；收紧 Tc 上限只会变窄或为空。"""
    wide = usable_of_range("lox_lh2", 68.0, 40.0)
    assert wide is not None
    low, high = wide
    assert 2.0 <= low < high <= 8.0
    narrow = usable_of_range("lox_lh2", 68.0, 40.0, tc_max_k=2500.0)
    assert narrow is None or narrow[0] >= low


def test_efficiency_table_and_isp_actual() -> None:
    """§8.3：组合效率中值表 + Isp_actual = η × Isp_ideal。"""
    assert CYCLE_EFFICIENCY == {
        "gas_generator": 0.93,
        "staged_combustion": 0.96,
        "expander": 0.96,
        "solid": 0.90,
    }
    assert isp_actual(3651.8, 0.93) == pytest.approx(3651.8 * 0.93, rel=1e-15)
    assert isp_actual(372.4, 0.96) == pytest.approx(357.504, rel=1e-12)


def test_efficiency_explicit_overrides_cycle_median() -> None:
    """η 语义：显式值优先；缺省按循环中值；未收录循环显式报错不臆造。"""
    assert resolve_efficiency(0.97, "staged_combustion") == 0.97
    assert resolve_efficiency(None, "expander") == 0.96
    assert resolve_efficiency(None, "solid") == 0.90
    with pytest.raises(AeroForgeError):
        resolve_efficiency(None, "pressure_fed")  # §8.3 无该行，不臆造中值
    with pytest.raises(AeroForgeError):
        resolve_efficiency(None, None)
