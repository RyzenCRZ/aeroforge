"""M4 基准回归（§13.2 / §16 M4 验收判据）：Falcon 9 / 长征五号 / 土星五号 LEO
运力误差 **< 15%**。

三个基准各覆盖一条链路形态：

- Falcon 9：两级纯串联 + LOX/RP-1（点值链冒烟已到 -0.6%，此处为正式门禁）；
- 长征五号：**含并联助推器**——0 级段（OI-36）+ 芯一级跨段贮箱账全链验收；
- 土星五号：**三级** + 两种推进剂组合（LOX/RP-1 与 LOX/LH₂）——中间级分配错误
  只有它测得出（§13.2 补它的原始理由）。

发射场取各模板自带的 ``mission.launch_site``（F9 / 土星五号 = 卡角，CZ-5 = 文昌），
不得用测试场替换——纬度是运力的一阶输入（§8.6）。

⚠ 公开运力对照值与 §13.2 基准表同源（OI-29：禁止两套数字）；模板 σ 由公开干重 /
推进剂反算，误差是「模板参数 + L1 损失模型」的合成误差——正是 M4 要验收的对象。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.params.templates import cz5_vehicle, falcon9_vehicle, saturnv_vehicle

#: M4 验收阈值（§16 M4 行：三基准运力误差 < 15%）
M4_TOLERANCE = 0.15

#: §13.2 基准表公开 LEO 运力（kg）——与模板 sourced_fields 同源
_PUBLIC_LEO_KG = {
    "falcon-9": 22_800.0,
    "cz-5": 25_000.0,
    "saturn-v": 140_000.0,
}


def _leo_payload_kg(vehicle: object) -> tuple[float, str]:
    """对模板跑一次点值 evaluate（不投 MC），返回 (LEO 运力, 使用的 ΔV 来源)。"""
    client = TestClient(app)
    response = client.post(
        "/api/perf/evaluate",
        json={"vehicle": vehicle.model_dump(mode="json"), "mc": False},  # type: ignore[attr-defined]
    )
    assert response.status_code == 200, response.text
    row = response.json()["point"]["payload_by_orbit"]["LEO"]
    assert row["attainable"] is True, "基准火箭必须可达 LEO（不可达即链路缺陷）"
    return float(row["payload_kg"]), str(row["dv_source"])


@pytest.mark.parametrize(
    ("template_id", "factory"),
    [
        ("falcon-9", falcon9_vehicle),
        ("cz-5", cz5_vehicle),
        ("saturn-v", saturnv_vehicle),
    ],
)
def test_m4_benchmark_leo_within_15_percent(template_id: str, factory: object) -> None:
    """§16 M4 验收：三基准 LEO 运力误差 < 15%（点值链，mc=false 不投区间）。"""
    vehicle = factory()  # type: ignore[operator]
    site = vehicle.mission.launch_site
    assert site is not None, "模板必须自带发射场（纬度是一阶输入，不得用测试场替换）"

    payload_kg, dv_source = _leo_payload_kg(vehicle)
    public_kg = _PUBLIC_LEO_KG[template_id]
    error = abs(payload_kg - public_kg) / public_kg

    assert error < M4_TOLERANCE, (
        f"{template_id} LEO 运力 {payload_kg:,.0f} kg vs 公开 {public_kg:,.0f} kg"
        f"（误差 {error:.1%} ≥ {M4_TOLERANCE:.0%}，dv_source={dv_source}）"
    )


def test_benchmarks_use_anchored_dv_and_real_sites() -> None:
    """基准链路的 ΔV 来源必须是量级锚定（不是用户 Mission 覆写），且三场纬度正确。"""
    for factory, expected_lat in (
        (falcon9_vehicle, 28.56),
        (cz5_vehicle, 19.61),
        (saturnv_vehicle, 28.57),
    ):
        vehicle = factory()
        site = vehicle.mission.launch_site
        assert site is not None
        assert site.latitude_deg == pytest.approx(expected_lat, abs=0.5), (
            f"{type(vehicle).__name__} 发射场纬度 {site.latitude_deg} 偏离公开值 {expected_lat}"
        )
        _, dv_source = _leo_payload_kg(vehicle)
        assert "锚定" in dv_source, f"基准必须用锚定 ΔV（实测 {dv_source}）"
