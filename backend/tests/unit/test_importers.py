"""导入器单元测试（规格 §7.5 / R-21）：样例驱动 + 显式失败路径。

覆盖四层：纯解析函数（``.eng`` 的 lb→N 精确换算、总冲=曲线积分、warnings
触发、行号/XPath 报错）、扩展名分派与字节解码、``POST /api/import/motor``
契约、``tools/model_import.py`` CLI 薄壳。样例文件在 ``testdata/ref_models/``
（规格 §15 指定的回归目录）。
"""

from __future__ import annotations

import gzip
import importlib.util
import re
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.data.parsers import (
    MotorParseResult,
    ParserError,
    RocketParseResult,
    ThrustPoint,
    UnsupportedFormatError,
    decode_import_bytes,
    parse_import_text,
)
from aeroforge.data.parsers.eng import LB_TO_N, parse_eng
from aeroforge.data.parsers.ork import parse_ork
from aeroforge.data.parsers.rse import parse_rse

#: 仓库根（本文件位于 backend/tests/unit/ 下）。
REPO_ROOT = Path(__file__).resolve().parents[3]
REF_MODELS = REPO_ROOT / "testdata" / "ref_models"


def _read(name: str) -> str:
    return (REF_MODELS / name).read_text(encoding="utf-8")


def _trapezoid(points: list[ThrustPoint]) -> float:
    """与解析器同一口径的梯形积分（用于总冲交叉断言）。"""
    return sum((b.thrust_n + a.thrust_n) * 0.5 * (b.time_s - a.time_s) for a, b in pairwise(points))


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """进入 lifespan 的客户端（与 test_api_params 同款）。"""
    with TestClient(app) as instance:
        yield instance


# ------------------------------------------------------------------ .eng 解析器


def test_eng_sample_converts_pounds_to_newtons() -> None:
    """样例数据行是磅力：换算必须精确体现 4.4482216152605 系数，且无警告。"""
    result = parse_eng(_read("sample_solid.eng"))

    assert isinstance(result, MotorParseResult)
    assert result.format == "eng"
    assert result.name == "SampleSolid"
    assert result.delays == ["P"]
    assert result.total_mass_kg == pytest.approx(0.040)
    assert result.propellant_mass_kg == pytest.approx(0.020)
    assert result.burn_time_s == pytest.approx(2.0)
    assert result.derived is False

    # t=0.2 行推力 1.2 lb、峰值 1.8 lb —— 逐点核对换算系数
    assert result.thrust_curve[2].time_s == pytest.approx(0.2)
    assert result.thrust_curve[2].thrust_n == pytest.approx(1.2 * LB_TO_N)
    assert result.peak_thrust_n == pytest.approx(1.8 * LB_TO_N)

    # 总冲 = 曲线积分，且与手算的 2.78 lb·s 换算值一致
    assert result.total_impulse_ns == pytest.approx(_trapezoid(result.thrust_curve))
    assert result.total_impulse_ns == pytest.approx(2.78 * LB_TO_N, rel=1e-9)
    assert result.avg_thrust_n == pytest.approx(result.total_impulse_ns / result.burn_time_s)

    assert result.warnings == []
    assert result.unit_conversions
    joined = "；".join(result.unit_conversions)
    assert "lb" in joined and "4.4482216152605" in joined


def test_eng_inline_negative_thrust_triggers_warning() -> None:
    """负推力必须进 warnings（不静默、不修正原值），且指回行号。"""
    text = "\n".join(
        [
            "BadMotor 0 10Ns 5.0 2.0",
            "0.0 0.0",
            "0.5 -1.5",
            "1.0 5.0",
            "2.0 0.0",
        ]
    )
    result = parse_eng(text)

    assert result.thrust_curve[1].thrust_n == pytest.approx(-1.5)  # 原值保留
    assert any("负推力" in w and "第 3 行" in w for w in result.warnings)


def test_eng_reports_curve_anomalies() -> None:
    """首点时间≠0、时间倒退、积分总冲与表头偏差 >2% 都要报告。"""
    text = "\n".join(
        [
            "Weird 0 20Ns",
            "0.3 1.0",
            "0.6 1.0",
            "0.4 1.0",
            "1.2 1.0",
        ]
    )
    result = parse_eng(text)
    joined = "\n".join(result.warnings)

    assert "首点时间" in joined
    assert "非单调" in joined
    # 积分 ≈ 0.9 N·s，表头声明 20 N·s，偏差远超 2%
    assert "总冲" in joined and "偏差" in joined


def test_broken_eng_raises_with_line_number() -> None:
    """只有注释无表头 → ParserError，信息必须带行号（R-21）。"""
    with pytest.raises(ParserError) as excinfo:
        parse_eng(_read("broken.eng"))
    assert re.search(r"第 \d+ 行", str(excinfo.value))
    assert "表头" in str(excinfo.value)


def test_eng_without_data_points_raises() -> None:
    """有表头但无数据点 → ParserError（空曲线走失败路径，不产出结果）。"""
    with pytest.raises(ParserError, match="数据点"):
        parse_eng("Lonely 0 5Ns\n")


# ------------------------------------------------------------------ .rse 解析器


def test_rse_sample_parses_motor_fields() -> None:
    result = parse_rse(_read("sample_motor.rse"))

    assert result.format == "rse"
    assert result.name == "AF-R1"
    assert result.manufacturer == "AeroForge"
    assert result.motor_type == "single-use"
    assert result.delays == ["0", "2", "4", "P"]
    assert result.total_mass_kg == pytest.approx(0.041)
    assert result.propellant_mass_kg == pytest.approx(0.019)
    assert len(result.thrust_curve) == 15
    assert result.burn_time_s == pytest.approx(2.0)
    assert result.peak_thrust_n == pytest.approx(9.0)
    assert result.total_impulse_ns == pytest.approx(_trapezoid(result.thrust_curve))
    assert result.total_impulse_ns == pytest.approx(15.15, abs=1e-9)
    assert result.warnings == []
    assert result.derived is False


def test_rse_bad_root_or_missing_attr_raise_with_xpath() -> None:
    """根元素不对 → ParserError；数据点缺属性 → XPath 定位（R-21）。"""
    with pytest.raises(ParserError, match="根元素"):
        parse_rse("<?xml version='1.0'?><wrong-root/>")
    text = _read("sample_motor.rse").replace('<point t="0.0" f="0.0"/>', '<point t="0.0"/>', 1)
    with pytest.raises(ParserError, match=r"point\[1\]"):
        parse_rse(text)


# ------------------------------------------------------------------ .ork 解析器


def test_ork_sample_is_derived_with_stages() -> None:
    result = parse_ork(_read("sample_rocket.ork"))

    assert isinstance(result, RocketParseResult)
    assert result.format == "ork"
    assert result.derived is True
    assert result.name == "AF 两级样例箭"
    assert result.stage_count == 2
    first, second = result.stages
    assert first.name == "一级"
    assert first.length_m == pytest.approx(1.20)
    assert first.diameter_m == pytest.approx(0.098)
    assert first.mass_kg == pytest.approx(1.850)
    assert first.engines == ["AF-R1"]
    assert second.engines == ["AF-R2"]
    # 样例刻意带的 <referencetype> 必须被报告（R-21 不静默）
    assert any("referencetype" in w for w in result.warnings)


def test_ork_reports_unknown_elements_without_stopping() -> None:
    """顶层与 <rocket> 下的未识别元素都进 warnings，解析不中断。"""
    text = _read("sample_rocket.ork")
    text = text.replace("<rocket>", "<rocket>\n    <bogus/>", 1).replace(
        "</openrocket>", "<simulator/>\n</openrocket>", 1
    )
    result = parse_ork(text)
    joined = "\n".join(result.warnings)

    assert "bogus" in joined
    assert "simulator" in joined
    assert result.stage_count == 2


def test_ork_bad_root_raises() -> None:
    with pytest.raises(ParserError, match="根元素"):
        parse_ork("<?xml version='1.0'?><not-openrocket/>")


# ---------------------------------------------------------------- 扩展名分派


@pytest.mark.parametrize(
    ("filename", "expected_format"),
    [("sample_solid.eng", "eng"), ("sample_motor.rse", "rse"), ("sample_rocket.ork", "ork")],
)
def test_dispatch_by_extension(filename: str, expected_format: str) -> None:
    result = parse_import_text(_read(filename), filename)
    assert result.format == expected_format


def test_dispatch_is_case_insensitive_and_rejects_unknown() -> None:
    result = parse_import_text(_read("sample_solid.eng"), "MOTOR.ENG")
    assert result.format == "eng"

    with pytest.raises(UnsupportedFormatError):
        parse_import_text("hello", "note.txt")


def test_decode_handles_gzip_and_rejects_empty() -> None:
    """.ork 常见 gzip 压缩：按魔数识别解压；空内容显式报错。"""
    assert decode_import_bytes(gzip.compress(b"0.0 0.0"), "x.eng") == "0.0 0.0"
    with pytest.raises(ParserError, match="为空"):
        decode_import_bytes(b"   ", "x.eng")


# ------------------------------------------------------ POST /api/import/motor


def _upload(client: TestClient, name: str) -> Any:
    data = (REF_MODELS / name).read_bytes()
    return client.post(
        "/api/import/motor", files={"file": (name, data, "application/octet-stream")}
    )


def test_endpoint_imports_eng_sample(client: TestClient) -> None:
    response = _upload(client, "sample_solid.eng")

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "eng"
    assert body["derived"] is False
    assert body["name"] == "SampleSolid"
    for key in ("total_impulse_ns", "burn_time_s", "avg_thrust_n", "peak_thrust_n", "thrust_curve"):
        assert key in body
    assert body["total_impulse_ns"] > 0
    assert len(body["thrust_curve"]) == 15
    assert body["warnings"] == []


def test_endpoint_imports_rse_sample(client: TestClient) -> None:
    response = _upload(client, "sample_motor.rse")

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "rse"
    assert body["derived"] is False
    assert body["manufacturer"] == "AeroForge"
    assert body["delays"] == ["0", "2", "4", "P"]
    assert body["peak_thrust_n"] == pytest.approx(9.0)


def test_endpoint_imports_ork_sample_as_derived(client: TestClient) -> None:
    response = _upload(client, "sample_rocket.ork")

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "ork"
    assert body["derived"] is True
    assert body["stage_count"] == 2
    assert body["stages"][0]["engines"] == ["AF-R1"]
    assert body["stages"][0]["length_m"] == pytest.approx(1.20)


def test_endpoint_broken_file_is_4xx_with_line_number(client: TestClient) -> None:
    response = _upload(client, "broken.eng")

    assert 400 <= response.status_code < 500
    error = response.json()["error"]
    assert error["code"] == "IMPORT_PARSE_FAILED"
    assert error["stage"] == "import"
    assert re.search(r"第 \d+ 行", error["message"])
    assert error["suggestion"]


def test_endpoint_rejects_unknown_extension(client: TestClient) -> None:
    response = client.post(
        "/api/import/motor", files={"file": ("note.txt", b"hello", "text/plain")}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IMPORT_FORMAT_UNSUPPORTED"


# ------------------------------------------------------------------ CLI 薄壳


def _load_cli() -> Any:
    """tools/ 不是包：按路径加载 model_import.py 后直接调 main（冒烟）。"""
    path = REPO_ROOT / "tools" / "model_import.py"
    spec = importlib.util.spec_from_file_location("model_import_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_smoke_prints_summary(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _load_cli().main([str(REF_MODELS / "sample_solid.eng")])
    out = capsys.readouterr().out

    assert rc == 0
    assert "SampleSolid" in out
    assert "N·s" in out
    assert "警告" in out


def test_cli_failure_exits_nonzero_with_line_number(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _load_cli().main([str(REF_MODELS / "broken.eng")])
    captured = capsys.readouterr()

    assert rc == 1
    assert re.search(r"第 \d+ 行", captured.err)
