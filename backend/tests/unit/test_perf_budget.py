"""性能预算门禁（规格 §13.5.1 / §13.8）。

由来（教训 A/C 类：没有门禁的"预算"等于没有预算）
-----------------------------------------------------
M1 收尾时量出了一串数字——前端单 chunk 1173.22 kB、冻结产物 451.72 MB、首帧 4.74 s、
改参数→预览 237 ms——但它们**只存在于叙述里**：文档写了目标、记录写了实测，却没有任何
测试会因"下次不小心翻倍"而失败。

本文件把 §13.5.1 抽出的预算变成三类判定：

1. **CI 实测**（包体积、`validate` p95）——每次 `uv run pytest` 都会重新量，超限即失败；
2. **产物实测**（冻结产物体积）——有产物就量，没有则**显式 skip 留痕**（跳过而非静默通过）；
3. **人工实测 / 产物实测但 CI 不覆盖**（端到端预览、首帧 `--probe`）——**必须**在
   :data:`BUDGETS` 里登记并写明理由，否则最后一项测试会失败。

最后一条是本文件的真正价值：**不允许存在"无人测量"的预算**——新增预算若不表态归谁测，
门禁直接报出来（同 §13.8"夹具须显式归类"的口径）。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_MD = REPO_ROOT / "AeroForge-Spec.md"
FRONTEND_ASSETS = REPO_ROOT / "frontend" / "dist" / "assets"
FROZEN_PRODUCT = REPO_ROOT / "dist-desktop" / "AeroForge"

_KB = 1000.0
"""前端包体积的口径：**十进制** kB，与 `vite build` 输出的 `1,173.22 kB` 一致。

⚠ 不要改成 1024：那会让本门禁的数字与构建日志、与 §16.3 记录对不上（实测 1173.22 kB
按 1024 算得 1145.73，差 2.4%），属"看起来对、其实换了单位"的一类错误。
"""

_MIB = 1024.0 * 1024.0
"""冻结产物体积的口径：**二进制** MiB，与资源管理器显示的 `451.72 MB` 一致。"""

#: 量级 sanity 下限：目录存在但内容异常（如 rglob 空集、构建半途失败）时不得算作"在预算内"。
_BUNDLE_FLOOR_KB = 100.0
_PRODUCT_FLOOR_MB = 50.0

#: 延迟测量的样本数（§13.5.1 表内已声明 n = 40，改这里必须同步改表）。
_LATENCY_SAMPLES = 40

#: 计入 p95 前先空跑几次，避免把导入/首次序列化的冷启动算进热路径。
_LATENCY_WARMUP = 3

_PROFILE = {
    "name": "perf-budget-capsule",
    "base_radius": 0.0,
    "segments": [
        {"type": "arc", "length": 1.0, "end_radius": 1.0},
        {"type": "line", "length": 2.0, "end_radius": 1.0},
    ],
}


@dataclass(frozen=True)
class Budget:
    """单条预算。

    ``gate`` 为执行它的测试函数名；为 ``None`` 表示**CI 无法测量**，此时 ``reason``
    必须写明为何不能测、以及由谁承担（人工 / 冻结产物 / 里程碑验收记录）。
    """

    key: str
    limit: float
    unit: str
    #: 与规格 §13.5.1 表内**逐字一致**的上限写法（用于文档↔代码同步断言）。
    limit_text: str
    gate: str | None
    reason: str = ""


BUDGETS: tuple[Budget, ...] = (
    Budget(
        key="前端产物 JS 总体积（min，未 gzip）",
        limit=1400.0,
        unit="kB",
        limit_text="1400 kB",
        gate="test_frontend_bundle_within_budget",
    ),
    Budget(
        key="冻结产物总体积（PyInstaller `onedir`）",
        limit=550.0,
        unit="MB",
        limit_text="550 MB",
        gate="test_frozen_product_within_budget",
    ),
    Budget(
        key="`POST /api/geometry/validate` p95（`TestClient`，n = 40）",
        limit=100.0,
        unit="ms",
        limit_text="100 ms",
        gate="test_validate_latency_within_budget",
    ),
    Budget(
        key="改参数 → 预览更新（含 200 ms 防抖）",
        limit=500.0,
        unit="ms",
        limit_text="500 ms",
        gate=None,
        reason=(
            "必须由真实浏览器取得（方法见 §16.3 验收记录），CI 无图形环境；"
            "且该量含 200 ms 防抖这一固定项，机器判定的价值低于其后端段。"
            "由 §16.3 记录承载：凡改动防抖窗口或预览链路，须按该节方法重跑并回写记录。"
        ),
    ),
    Budget(
        key="冷启动 → 首帧渲染完成（冻结产物 `--probe`）",
        limit=7.0,
        unit="s",
        limit_text="7.0 s",
        gate=None,
        reason=(
            "需冻结产物 + 真实 WebView2 窗口（§16.2 的 `AeroForge.exe --probe`），"
            "属交付链验证而非单元门禁，故随里程碑在本地执行；数字回写 §16.2 复验表。"
        ),
    ),
)

_BY_KEY = {budget.key: budget for budget in BUDGETS}

_LIMIT_NUMBER = re.compile(r"^([\d.]+)\s*(\S+)$")


def _budget(key: str) -> Budget:
    return _BY_KEY[key]


def _spec_budget_rows() -> dict[str, str]:
    """规格 §13.5.1 的预算表：预算项名 → 上限单元格原文。"""
    lines = SPEC_MD.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("#### 13.5.1"))
    stop = next(index for index in range(start + 1, len(lines)) if lines[index].startswith("### "))
    rows: dict[str, str] = {}
    for line in lines[start:stop]:
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or not cells[0] or set(cells[0]) <= set("-: "):
            continue
        rows[cells[0]] = cells[1]
    return rows


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, round(0.95 * len(ordered)) - 1))
    return ordered[index]


def test_budget_table_is_self_consistent() -> None:
    """表内的显示写法与数值必须一致，且不得重复登记同一条预算。"""
    keys = [budget.key for budget in BUDGETS]
    assert len(set(keys)) == len(keys), f"预算项名重复：{keys}"

    for budget in BUDGETS:
        match = _LIMIT_NUMBER.match(budget.limit_text)
        assert match is not None, f"{budget.key} 的 limit_text 写法无法解析：{budget.limit_text!r}"
        assert float(match.group(1)) == budget.limit, f"{budget.key} 的 limit 与 limit_text 不一致"
        assert match.group(2) == budget.unit, f"{budget.key} 的 unit 与 limit_text 不一致"


def test_budget_table_matches_spec() -> None:
    """预算表与规格 §13.5.1 必须一致——改了代码不改文档（或反之）即失败。"""
    rows = _spec_budget_rows()
    assert rows, "未能从规格 §13.5.1 解析到任何预算行（章节标题或表头可能已改）"

    missing = [budget.key for budget in BUDGETS if budget.key not in rows]
    assert not missing, f"以下预算未登记在规格 §13.5.1 表中：{missing}"

    drifted = [
        f"{budget.key}：规格写 {rows[budget.key]!r}，代码要 **{budget.limit_text}**"
        for budget in BUDGETS
        if f"**{budget.limit_text}**" not in rows[budget.key]
    ]
    assert not drifted, "预算上限与规格 §13.5.1 不一致：\n" + "\n".join(
        f"  - {item}" for item in drifted
    )


def test_every_budget_is_gated_or_declared_manual() -> None:
    """不允许存在"无人测量"的预算。

    要么 ``gate`` 指向一个真实存在的测试函数，要么显式登记为人工/产物实测并写明理由。
    """
    provided = {name for name in globals() if name.startswith("test_")}
    ungated: list[str] = []
    dangling: list[str] = []
    for budget in BUDGETS:
        if budget.gate is None:
            if not budget.reason.strip():
                ungated.append(budget.key)
            continue
        if budget.gate not in provided:
            dangling.append(f"{budget.key} → {budget.gate}")

    assert not ungated, (
        f"以下预算既没有机器门禁，也没有登记为人工/产物实测（规格 §13.5.1 规则 1）：{ungated}"
    )
    assert not dangling, f"以下预算声明的门禁测试并不存在：{dangling}"


def test_frontend_bundle_within_budget() -> None:
    """前端产物 JS 总体积（§13.5.1）。"""
    if not FRONTEND_ASSETS.is_dir():
        pytest.skip(
            f"未找到 {FRONTEND_ASSETS}，先执行 npm run build（本门禁在 CI 前置构建后自动生效）"
        )

    chunks = sorted(FRONTEND_ASSETS.glob("*.js"))
    if not chunks:
        pytest.skip(f"{FRONTEND_ASSETS} 内没有 .js 产物，构建可能未完成")

    budget = _budget("前端产物 JS 总体积（min，未 gzip）")
    sizes = {chunk.name: chunk.stat().st_size / _KB for chunk in chunks}
    total = sum(sizes.values())
    detail = "、".join(f"{name} {size:.2f} kB" for name, size in sizes.items())

    assert total > _BUNDLE_FLOOR_KB, (
        f"前端 JS 总体积仅 {total:.2f} kB（{detail}）——低于 {_BUNDLE_FLOOR_KB:.0f} kB 的"
        "量级下限，几乎一定是构建不完整或产物被清空。此时'未超预算'毫无意义，故判失败。"
    )
    assert total <= budget.limit, (
        f"前端 JS 总体积 {total:.2f} kB 超出预算 {budget.limit_text}"
        f"（{detail}）。请先确认是新增依赖还是重复打包；确需上调预算须在规格修订记录中说明理由。"
    )


def test_frozen_product_within_budget() -> None:
    """冻结产物总体积（§13.5.1）。无产物时显式跳过——跳过也是留痕，不算通过。"""
    if not FROZEN_PRODUCT.is_dir():
        pytest.skip(
            f"未找到冻结产物 {FROZEN_PRODUCT}；按里程碑冻结后本门禁自动生效（命令见规格 §16.2）"
        )

    budget = _budget("冻结产物总体积（PyInstaller `onedir`）")
    size = sum(path.stat().st_size for path in FROZEN_PRODUCT.rglob("*") if path.is_file())
    total = size / _MIB

    assert total > _PRODUCT_FLOOR_MB, (
        f"冻结产物总体积仅 {total:.2f} MB——低于 {_PRODUCT_FLOOR_MB:.0f} MB 的量级下限。"
        "本产物含 OCCT 与 CEA 的原生库，正常应在 450 MB 量级；此结果更像是目录不全或"
        "读取失败（历史上 hook 静默未触发时产物只有 88.4 MB）。"
    )
    assert total <= budget.limit, (
        f"冻结产物总体积 {total:.2f} MB 超出预算 {budget.limit_text}。"
        "体积异常增大通常意味着收集了多余的原生库或数据（见 §16.2 必收资源清单），"
        "缩水则可能是 hook 静默未触发——两种方向都要查。"
    )


def test_validate_latency_within_budget() -> None:
    """`POST /api/geometry/validate` 的 p95（§13.5.1）。

    这是预览链路里**唯一能在 CI 里量到**的一段（前端防抖与浏览器绘制不计入）。
    预算给得宽（实测约 5 ms，上限 100 ms），目的是拦量级回归而非卡具体毫秒。
    """
    budget = _budget("`POST /api/geometry/validate` p95（`TestClient`，n = 40）")

    with TestClient(app) as client:
        for _ in range(_LATENCY_WARMUP):
            assert client.post("/api/geometry/validate", json=_PROFILE).status_code == 200

        samples: list[float] = []
        for _ in range(_LATENCY_SAMPLES):
            started = time.perf_counter()
            response = client.post("/api/geometry/validate", json=_PROFILE)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            assert response.status_code == 200
            samples.append(elapsed_ms)

    p95 = _p95(samples)
    assert p95 <= budget.limit, (
        f"validate p95 = {p95:.2f} ms 超出预算 {budget.limit_text}"
        f"（n = {_LATENCY_SAMPLES}，最快 {min(samples):.2f} ms / 最慢 {max(samples):.2f} ms）。"
        "该接口是每次改参数都会打的热路径，退化会直接反映为用户感知的预览卡顿。"
    )
