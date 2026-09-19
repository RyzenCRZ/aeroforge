"""AeroForge 环境预检（规格 §3.2 / ADR-014 / R-29 / R-30）。

把"依赖是否真的能跑"固化为可执行门禁。三项检查按依赖顺序执行，任一失败即非零退出：

1. **字体完整性** —— ``build123d`` 在**模块导入期**无条件扫描系统字体且 ``register_font()``
   无异常保护，系统内任一以 ``.ttf`` 命名的非字体文件都会使 ``import build123d`` 整体崩溃（R-30）。
   因此本项必须在导入 build123d **之前**执行。
2. **OCCT 几何内核自检** —— 解析体积 vs 内核体积。
3. **CEA 点火自检** —— 按 ADR-014 契约复核黄金锚点（纯气相产物列表 + Isp 转秒除 g₀ +
   健全性门禁），同时验证产物列表模板本身没有落入静默错解（R-29）。

**本模块是预检的唯一实现**（v0.4.1）。两个调用入口共用它：

- 开发期 CLI：``tools/preflight.py``（仅转调，不另存实现）
- 冻结交付物：``AeroForge.exe --preflight``（见 §16.2「预检保留」）

之所以不做成两份：R-30 的触发条件（系统字体损坏）在**部署机器**上同样成立，预检必须
随产物一同冻结；而"被冻结"的前提是该模块出现在 **PyInstaller 的导入图内**——这同时使
``build123d`` / ``cea``（及其 OCP / libcea 原生库）被收集，正是 §16.2「必收资源」的落点。
⚠ 反向教训：只写收集 hook 而没有导入路径回指该包时，hook **不会被执行**，产物静默缺库。

用法::

    uv run python tools/preflight.py     # 开发期
    AeroForge.exe --preflight            # 冻结产物
"""

from __future__ import annotations

import math
import os
import platform
import sys
from pathlib import Path

#: sfnt 容器合法 magic：TrueType / OpenType(CFF) / Apple TrueType / PostScript Type1 / 字体集合
VALID_SFNT_MAGIC = frozenset({b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1", b"ttcf"})

FONT_GLOBS = ("*.ttf", "*.otf", "*.ttc")

#: 标准重力加速度 [m/s^2]。ADR-014 契约①：CEA 的 Isp 字段是速度，转秒须除以此值。
STANDARD_GRAVITY = 9.80665

#: LOX/CH4 黄金锚点工况（规格 §3.2 实测表）。产物列表按 ADR-014 契约②固定为纯气相。
CEA_PRODUCTS_GAS = [
    "C",
    "CO",
    "CO2",
    "H",
    "H2",
    "H2O",
    "O",
    "O2",
    "OH",
    "CH4",
    "C2H4",
    "C2H6",
]
CEA_O_F = 3.40
CEA_PC_BAR = 100.0
CEA_AREA_RATIO = 40.0
CEA_REACTANT_T0_K = 300.0
#: 锚点实测值：Tc = 3629.2 K，Isp_vac = 372.4 s
CEA_ANCHOR_TC_K = 3629.2
CEA_ANCHOR_ISP_VAC_S = 372.4
CEA_ANCHOR_TC_TOL_K = 50.0
CEA_ANCHOR_ISP_TOL_S = 2.0
#: Tc 物理合理区间，用于拦截 R-29 的静默错解（实测错解落在 ~882 K）
CEA_TC_PLAUSIBLE_K = (2000.0, 5000.0)

OCCT_RADIUS_M = 1.85
OCCT_HEIGHT_M = 10.0


def font_directories() -> list[Path]:
    """返回本平台需要检查的字体目录（不存在的会被跳过）。"""
    system = platform.system()
    if system == "Windows":
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        dirs = [windir / "Fonts"]
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            dirs.append(Path(local_appdata) / "Microsoft" / "Windows" / "Fonts")
        return dirs
    if system == "Darwin":
        return [
            Path("/System/Library/Fonts"),
            Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts",
        ]
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local" / "share" / "fonts",
        Path.home() / ".fonts",
    ]


def find_broken_fonts() -> tuple[int, list[Path]]:
    """扫描字体目录，返回 (已检查文件数, 非法字体文件列表)。

    只做容器 magic 校验——这正是 build123d 崩溃的最小触发条件。
    """
    scanned = 0
    broken: list[Path] = []
    for directory in font_directories():
        if not directory.is_dir():
            continue
        for pattern in FONT_GLOBS:
            for path in sorted(directory.glob(pattern)):
                try:
                    with path.open("rb") as handle:
                        magic = handle.read(4)
                except OSError:
                    continue
                scanned += 1
                if magic not in VALID_SFNT_MAGIC:
                    broken.append(path)
    return scanned, broken


def check_fonts() -> bool:
    """检查 1：字体完整性。必须在导入 build123d 之前调用。"""
    scanned, broken = find_broken_fonts()
    if not broken:
        print(f"[1/3] 字体完整性      通过（已检查 {scanned} 个字体文件）")
        return True

    print(f"[1/3] 字体完整性      失败（已检查 {scanned} 个，非法 {len(broken)} 个）")
    for path in broken:
        size = path.stat().st_size if path.exists() else -1
        print(f"        ✗ {path}  ({size} B) 非合法 sfnt 容器")
    print("        影响：import build123d 将整体崩溃，后端无法启动（R-30）。")
    print("        处置：确认该文件不在任何字体注册表键（HKLM/HKCU/WOW6432Node）下后，")
    print("              将其重命名为 *.ttf.bak 隔离（可逆）。")
    return False


def check_occt() -> bool:
    """检查 2：OCCT 几何内核自检。"""
    from build123d import Cylinder

    solid = Cylinder(radius=OCCT_RADIUS_M, height=OCCT_HEIGHT_M)
    kernel_volume = solid.volume
    analytic_volume = math.pi * OCCT_RADIUS_M**2 * OCCT_HEIGHT_M
    relative_error = abs(kernel_volume - analytic_volume) / analytic_volume

    ok = relative_error < 1e-9
    status = "通过" if ok else "失败"
    print(
        f"[2/3] OCCT 几何内核    {status}"
        f"（体积 {kernel_volume:.6f} vs 解析 {analytic_volume:.6f}，相对误差 {relative_error:.3e}）"
    )
    return ok


def check_cea() -> bool:
    """检查 3：CEA 点火自检（ADR-014 契约 + 黄金锚点）。"""
    import cea
    import numpy as np

    reactants = cea.Mixture(["CH4", "O2(L)"])
    products = cea.Mixture(CEA_PRODUCTS_GAS)

    fuel_weights = reactants.moles_to_weights(np.array([1.0, 0.0]))
    oxid_weights = reactants.moles_to_weights(np.array([0.0, 1.0]))
    weights = reactants.of_ratio_to_weights(oxid_weights, fuel_weights, CEA_O_F)
    # 契约：RocketSolver 必须提供 hc 或 tc
    hc = reactants.calc_property(cea.ENTHALPY, weights, CEA_REACTANT_T0_K) / cea.R

    solver = cea.RocketSolver(products, reactants=reactants)
    solution = cea.RocketSolution(solver)
    solver.solve(solution, weights, CEA_PC_BAR, supar=CEA_AREA_RATIO, hc=hc)

    # 健全性门禁（ADR-014 契约③）：last_error 不充分，必须叠加 Tc 物理区间断言
    chamber_temperature = float(np.asarray(solution.T).ravel()[0])
    # 契约①：字段量纲为 m/s，除以 g0 才是比冲秒数
    isp_vacuum_s = float(np.asarray(solution.Isp_vacuum).ravel()[-1]) / STANDARD_GRAVITY

    failures: list[str] = []
    if solution.last_error != 0:
        failures.append(f"last_error = {solution.last_error}")
    low, high = CEA_TC_PLAUSIBLE_K
    if not low <= chamber_temperature <= high:
        failures.append(
            f"Tc = {chamber_temperature:.1f} K 落在物理区间 "
            f"[{low:.0f}, {high:.0f}] 之外（R-29 静默错解特征）"
        )
    if abs(chamber_temperature - CEA_ANCHOR_TC_K) > CEA_ANCHOR_TC_TOL_K:
        failures.append(f"Tc 偏离锚点 {CEA_ANCHOR_TC_K} K 超过 {CEA_ANCHOR_TC_TOL_K} K")
    if abs(isp_vacuum_s - CEA_ANCHOR_ISP_VAC_S) > CEA_ANCHOR_ISP_TOL_S:
        failures.append(f"Isp_vac 偏离锚点 {CEA_ANCHOR_ISP_VAC_S} s 超过 {CEA_ANCHOR_ISP_TOL_S} s")

    if failures:
        print(
            f"[3/3] CEA 点火自检    失败"
            f"（Tc = {chamber_temperature:.1f} K，Isp_vac = {isp_vacuum_s:.1f} s）"
        )
        for failure in failures:
            print(f"        ✗ {failure}")
        return False

    print(
        f"[3/3] CEA 点火自检    通过"
        f"（LOX/CH4 O/F={CEA_O_F} Pc={CEA_PC_BAR} bar ε={CEA_AREA_RATIO:.0f}"
        f" → Tc = {chamber_temperature:.1f} K，Isp_vac = {isp_vacuum_s:.1f} s）"
    )
    return True


def main() -> int:
    """按依赖顺序执行全部检查，返回进程退出码。"""
    print(f"AeroForge 环境预检  Python {sys.version.split()[0]} on {platform.system()}")
    print("-" * 72)

    # 顺序不可调换：字体检查必须早于任何触发 build123d 导入的步骤。
    if not check_fonts():
        return 1
    if not check_occt():
        return 1
    if not check_cea():
        return 1

    print("-" * 72)
    print("全部通过。")
    return 0
