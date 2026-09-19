"""§6.5 阈值配置的读写与端点（规格 §1.7.3 OI-31 / §18.4 / §10.1）。

本文件管三件事：

1. **优先级与来源**——``环境变量 > config.toml > 默认值``，且界面拿到的必须是**生效值**
   与它的来源（``origin``）。若只回"文件里的值"，被环境变量压住时界面会显示一个
   "保存了却不生效"的数字，而用户无从判断（正是 OI-31 要消灭的形态）。
2. **写入是否最小改动**——只动涉及的键，注释与其余内容原样保留；键不存在时插在
   第一个 ``[table]`` **之前**（插到末尾会落进那个表里，读不到）。
3. **拒绝而不是猜**——未知键、越界值、语法坏的现有文件，一律拒绝并把文件留在原样。

⚠ 与 ``test_params_spec_contract.py`` 的分工：那个文件比对**规格表 ↔ 默认值**，
本文件比对**配置层 ↔ 生效值**。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.main import app
from aeroforge.params.diagnostics import CODE_TANK_WALL, DiagnosticThresholds
from aeroforge.params.schema import Vehicle
from aeroforge.params.thresholds import (
    ORIGIN_DEFAULT,
    ORIGIN_ENV,
    ORIGIN_FILE,
    ORIGIN_UNSET,
    THRESHOLD_SPECS,
    threshold_entries,
)
from aeroforge.paths import config_file


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(app) as instance:
        yield instance


@pytest.fixture(autouse=True)
def clean_config_file() -> Iterator[None]:
    """每条用例前后都清掉 ``config.toml``（会话共用一个数据根，**必须**自己收尾）。

    实测症状：一条「故意把文件写坏」的用例把 config.toml 留在了临时数据根里，
    其后每条 ``diagnose`` 都变成 422（``load_thresholds`` 读到坏文件）——
    而失败信息指向的是那些用例本身，完全看不出根因在上一个用例。
    同族教训：断言"被拒绝的请求不得留下半个文件"时，先得保证**进去之前没有文件**。
    """
    config_file().unlink(missing_ok=True)
    yield
    config_file().unlink(missing_ok=True)


def _entries() -> dict[str, dict[str, object]]:
    return {entry.key: entry.model_dump(mode="json") for entry in threshold_entries()}


# ---------------------------------------------------------------------------
# 模块级：键集一致性
# ---------------------------------------------------------------------------


def test_threshold_specs_cover_exactly_the_model_fields() -> None:
    """``THRESHOLD_SPECS`` 与模型字段必须**一一对应**。

    两处各写一份清单就有两份真相：模型加了字段而这里没加，界面就少一个设置项，
    而"少一个"看起来与"这一项不需要配"一模一样。
    """
    keys = [spec.key for spec in THRESHOLD_SPECS]

    assert len(keys) == len(set(keys)), f"清单里有重复键：{keys}"
    assert set(keys) == set(DiagnosticThresholds.model_fields)


#: 顶层赋值行的键名（英文标识符，故中文注释里的"…… = ……"不会被误认为键）。
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _example_keys(text: str) -> tuple[set[str], set[str]]:
    """``(生效行里的键, 提及到的键)``。

    注释掉的赋值行也计入"提及"：**「留空 = 不判定」这一项必须能写成注释**
    （§18.4 明确要求模板不得把它改成"留空即用某默认值"，而 TOML 没有 null，
    故该项只能以注释形式逐项列出）。反向断言只针对**生效行**——注释是散文，
    里面出现环境变量名（如 ``AEROFORGE_TWR_LIQUID_MIN``）是正常的。
    """
    live: set[str] = set()
    mentioned: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        body = stripped.lstrip("#").strip() if stripped.startswith("#") else stripped
        match = _ASSIGN.match(body)
        if match is None:
            continue
        mentioned.add(match.group(1))
        if not stripped.startswith("#"):
            live.add(match.group(1))
    return live, mentioned


def test_config_example_documents_every_threshold() -> None:
    """仓库根的 ``config.example.toml`` 必须逐项列出全部阈值键（§18.4 的硬要求）。

    只钉"文件里有这些键"是不够的——模板若多出模型没有的键，用户照着填会得到一个
    被拒绝的配置，故正反两向都断言。
    """
    example = Path(__file__).resolve().parents[3] / "config.example.toml"
    keys = {spec.key for spec in THRESHOLD_SPECS}
    live, mentioned = _example_keys(example.read_text(encoding="utf-8"))

    assert keys <= mentioned, f"模板漏了：{sorted(keys - mentioned)}"
    assert live <= keys, f"模板里出现了模型没有的顶层键：{sorted(live - keys)}"


def test_config_example_does_not_present_a_default_for_the_process_limit() -> None:
    """「最小工艺厚度」在模板里**不得**以生效行的形式给出数值（§18.4）。

    该项的默认状态是"未配置"。模板若写成 `min_tank_wall_thickness_m = 0.00051`，
    复制模板的人就得到一个**自称有来源、实则抄自量级参照**的工艺下限——
    这正是 §1.4-4 溯源红线要拦的形态。
    """
    example = Path(__file__).resolve().parents[3] / "config.example.toml"
    live, _ = _example_keys(example.read_text(encoding="utf-8"))

    assert "min_tank_wall_thickness_m" not in live


# ---------------------------------------------------------------------------
# 模块级：来源（origin）四态
# ---------------------------------------------------------------------------


def test_default_values_report_the_default_origin() -> None:
    entry = _entries()["twr_liquid_min"]

    assert entry["value"] == 1.2
    assert entry["origin"] == ORIGIN_DEFAULT
    assert entry["source"], "来源标注必须随每项下发（§6.5 强制）"


def test_unconfigured_process_limit_is_an_explicit_state() -> None:
    """「最小工艺厚度」未配置时必须显式呈现为「未配置」，而不是一个数字。

    这是 OI-31 的核心：写死一个默认毫米数会把"没人配置过"这一状态**抹掉**，
    而它恰恰是用户需要知道的事。
    """
    entry = _entries()["min_tank_wall_thickness_m"]

    assert entry["value"] is None
    assert entry["origin"] == ORIGIN_UNSET
    assert "未配置" in str(entry["source"])


def test_config_file_value_reports_the_file_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AEROFORGE_TWR_LIQUID_MIN", raising=False)
    config_file().write_text("twr_liquid_min = 1.3\n", encoding="utf-8")

    entry = _entries()["twr_liquid_min"]

    assert entry["value"] == pytest.approx(1.3)
    assert entry["origin"] == ORIGIN_FILE


def test_environment_variable_wins_over_the_config_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """优先级不得被写入功能改变（§18.4）：环境变量仍在 ``config.toml`` 之上。"""
    config_file().write_text("twr_liquid_min = 1.3\n", encoding="utf-8")
    monkeypatch.setenv("AEROFORGE_TWR_LIQUID_MIN", "1.25")

    entry = _entries()["twr_liquid_min"]

    assert entry["value"] == pytest.approx(1.25)
    assert entry["origin"] == ORIGIN_ENV


# ---------------------------------------------------------------------------
# 模块级：写入
# ---------------------------------------------------------------------------


def test_put_writes_into_the_isolated_config_file(client: TestClient) -> None:
    """写入必须落在 ``config_file()`` 给出的路径上——且该路径在会话临时区内。

    「设了根目录就要断言实际写盘路径」是本工程踩过的坑（产物曾落到 ``%TEMP%``
    并跨会话留存）：这里同样不能只断言"调用成功"。
    """
    response = client.put("/api/params/thresholds", json={"min_tank_wall_thickness_m": 0.0006})

    assert response.status_code == 200
    body = response.json()
    assert body["config_file"] == str(config_file())
    written = Path(body["config_file"])
    assert written == config_file()
    assert tomllib.loads(written.read_text(encoding="utf-8"))[
        "min_tank_wall_thickness_m"
    ] == pytest.approx(0.0006)

    entry = {item["key"]: item for item in body["thresholds"]}["min_tank_wall_thickness_m"]
    assert entry["value"] == pytest.approx(0.0006)
    assert entry["origin"] == ORIGIN_FILE


def test_put_with_null_clears_the_key(client: TestClient) -> None:
    """``null`` = 清空该键：行被删除，状态回到「未配置」（而不是写入一个 0）。"""
    client.put("/api/params/thresholds", json={"min_tank_wall_thickness_m": 0.0006})

    response = client.put("/api/params/thresholds", json={"min_tank_wall_thickness_m": None})

    assert response.status_code == 200
    entry = {item["key"]: item for item in response.json()["thresholds"]}[
        "min_tank_wall_thickness_m"
    ]
    assert entry["value"] is None
    assert entry["origin"] == ORIGIN_UNSET
    assert "min_tank_wall_thickness_m" not in config_file().read_text(encoding="utf-8")


def test_put_preserves_comments_and_other_content(client: TestClient) -> None:
    """最小改动：注释、空行与无关的键都不得被抹掉。

    整文件重写会静默丢掉用户写下的注释——"没报错"且"值也对"，只是他写的说明没了。
    """
    config_file().write_text(
        "# 我自己的说明\ntwr_liquid_min = 1.3\n\n# 另一段\nlength_to_diameter_max = 25.0\n",
        encoding="utf-8",
    )

    client.put("/api/params/thresholds", json={"twr_liquid_min": 1.35})

    text = config_file().read_text(encoding="utf-8")
    assert "# 我自己的说明" in text
    assert "# 另一段" in text
    assert "length_to_diameter_max = 25.0" in text
    assert "twr_liquid_min = 1.35" in text
    assert tomllib.loads(text) == {"twr_liquid_min": 1.35, "length_to_diameter_max": 25.0}


def test_new_keys_are_inserted_before_the_first_table(client: TestClient) -> None:
    """新键必须插在第一个 ``[table]`` 之前——插到末尾等于把它写进了那个表里。"""
    config_file().write_text("[my_section]\nfoo = 1\n", encoding="utf-8")

    client.put("/api/params/thresholds", json={"twr_liquid_min": 1.4})

    text = config_file().read_text(encoding="utf-8")
    assert text.index("twr_liquid_min") < text.index("[my_section]")
    assert tomllib.loads(text)["twr_liquid_min"] == pytest.approx(1.4)


def test_unknown_key_is_rejected(client: TestClient) -> None:
    response = client.put("/api/params/thresholds", json={"twr_liquid_minimum": 1.2})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "CONFIG_INVALID"
    assert error["stage"] == "config"
    assert error["suggestion"]
    assert not config_file().exists(), "被拒绝的请求不得留下半个文件"


def test_out_of_range_value_is_rejected_with_field_level_detail(client: TestClient) -> None:
    response = client.put("/api/params/thresholds", json={"twr_liquid_min": -1.0})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "CONFIG_INVALID"
    diagnostics = error["details"]["diagnostics"]
    assert [item["field_path"] for item in diagnostics] == ["twr_liquid_min"]
    assert not config_file().exists()


def test_broken_existing_file_is_reported_not_overwritten(client: TestClient) -> None:
    """现有文件语法坏掉时：报一条可操作错误，并**保持文件原样**。

    若此处放行，一次误写会把用户手工维护的内容整体吃掉，而界面上还会显示"保存成功"。
    """
    config_file().write_text("twr_liquid_min = = 1.2\n", encoding="utf-8")

    response = client.put("/api/params/thresholds", json={"twr_liquid_min": 1.2})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"
    assert config_file().read_text(encoding="utf-8") == "twr_liquid_min = = 1.2\n"


def test_read_reports_broken_file_instead_of_internal_error(client: TestClient) -> None:
    """读也要走 §10.3：配置文件坏掉不该变成一句「未预期的内部错误」。"""
    config_file().write_text("这不是 TOML", encoding="utf-8")

    response = client.get("/api/params/thresholds")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONFIG_INVALID"


# ---------------------------------------------------------------------------
# 与诊断联动：配置真的生效
# ---------------------------------------------------------------------------


def test_configuring_the_process_limit_makes_the_rule_judgeable(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """显式配置后，「贮箱壁厚」规则必须从「未判定」变成**真判定**。

    这是 OI-31 的验收实质：新增的不只是一个设置项，而是"一个之前判不了的硬规则
    现在判得了"。只断言 PUT 返回 200 会漏掉整条链。
    """
    before = client.post(
        "/api/params/diagnose", json=single_stage_vehicle.model_dump(mode="json")
    ).json()
    deferred_before = next(r for r in before["rules"] if r["code"] == CODE_TANK_WALL)
    assert deferred_before["deferred_reason"], "未配置时该规则本应不判定"

    client.put("/api/params/thresholds", json={"min_tank_wall_thickness_m": 0.0006})

    after = client.post(
        "/api/params/diagnose", json=single_stage_vehicle.model_dump(mode="json")
    ).json()
    judgeable = next(r for r in after["rules"] if r["code"] == CODE_TANK_WALL)
    assert judgeable["deferred_reason"] is None
    assert "0.0006" in judgeable["threshold"]


def test_lowering_the_limit_below_the_wall_produces_a_hard_finding(
    client: TestClient, single_stage_vehicle: Vehicle
) -> None:
    """把工艺下限设在壁厚之上，诊断必须报出硬违反——否则"配置生效"就是空话。"""
    payload = single_stage_vehicle.model_dump(mode="json")
    wall = payload["stages"][0]["geometry"]["fuel_tank"]["wall_thickness_m"]
    client.put("/api/params/thresholds", json={"min_tank_wall_thickness_m": wall * 2.0})

    response = client.post("/api/params/diagnose", json=payload)

    assert response.status_code == 200
    rule = next(r for r in response.json()["rules"] if r["code"] == CODE_TANK_WALL)
    assert [item["level"] for item in rule["diagnostics"]] == ["hard", "hard"]
    assert rule["diagnostics"][0]["field_path"] == (
        "stages[0].geometry.oxidizer_tank.wall_thickness_m"
    )
