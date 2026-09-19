"""GCAT ETL 门禁（§7.5）：单位换算、缺失策略、unit_uncertain 标记、行数对账。

全部离线：快照由 ``fetch_snapshot`` + 假下载器在临时目录现造。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest

from aeroforge.data.etl import RECORDS_PARQUET, run_etl, table_frame
from aeroforge.data.snapshot import SNAPSHOT_TABLES, fetch_snapshot

# ── 夹具：三张业务表各造 2 行，专测换算 / 缺失 / 疑点；三张关联表给空表头 ──

FAKE_LV = (
    "#LV_Name\tLV_Family\tLV_Variant\tLV_Manufacturer\tLV_Min_Stage\tLength\tDiameter"
    "\tLaunch_Mass\tLEO_Capacity\tGTO_Capacity\tTO_Thrust\tClass\tLFlag\tMFlag\tDFlag\n"
    "# Updated 2026 Sep 18\n"
    "N-1 11A52\tN-1\t-\tOKB1\t1\t105.3\t14.00\t2,788.0\t70000\t-\t45300\tO\t-\t-\t-\n"
    "BadRocket\tX\t-\tY\t1\t99999\t2.0\t12ab\t5\t5\t5\tO\t1\t-\t-\n"
)

FAKE_STAGES = (
    "#Stage_Name\tStage_Family\tStage_Manufacturer\tLength\tDiameter\tLaunch_Mass"
    "\tDry_Mass\tThrust\tThrustSL\tDuration\tEngine\tNEng\tLength_Flag\tDiameter_Flag"
    "\tLaunch_Mass_Flag\tDry_Mass_Flag\tThrust_Flag\tThrustSL_Flag\n"
    "# Updated 2026 Sep 18\n"
    "N-1 B1\tN-1\tOKB1\t22.0\t17.0\t1300.0\t65000\t42000\t38000\t130\tNK-15\t30\t-\t-\t-\t-\t-\t-\n"
    "S-IC\tSaturn V\tBoeing\t42.0\t10.0\t2300.0\t-\t?-kN\t?\t168\tF-1\t5\t-\t-\t-\t-\t-\t-\n"
)

FAKE_ENGINES = (
    "#Name\tManufacturer\tFamily\tAlt_Name\tOxidizer\tFuel\tMass\tMFlag\tImpulse"
    "\tImpFlag\tThrust\tTFlag\tIsp\tIspFlag\tDuration\tDurFlag\tChambers\tDate\tUsage\tGroup\n"
    "# Updated 2026 Sep 18\n"
    "NK-15\tOKB6\tNK\t-\tLOX\tKerosene\t1200\t-\t-\t-\t1400\t-\t327\t-\t120\t-\t1\t1969\tN-1B1\t-\n"
    "WeirdMotor\tX\tY\t-\tLOX\tCH4\t500\t-\t100\t-\t50\t-\t132800\t-\t60\t-\t1\t2020\tZ (1)\t-\n"
)

FAKE_EMPTY = "#Code\n"


def per_file_payload() -> dict[str, bytes]:
    return {
        "lv.tsv": FAKE_LV.encode("utf-8"),
        "stages.tsv": FAKE_STAGES.encode("utf-8"),
        "engines.tsv": FAKE_ENGINES.encode("utf-8"),
        "family.tsv": FAKE_EMPTY.encode("utf-8"),
        "orgs.tsv": FAKE_EMPTY.encode("utf-8"),
        "lvs.tsv": FAKE_EMPTY.encode("utf-8"),
    }


def downloader(payloads: dict[str, bytes]) -> Callable[[str], bytes]:
    def download(url: str) -> bytes:
        for filename, payload in payloads.items():
            if url.endswith(f"/{filename}"):
                return payload
        raise AssertionError(f"未知下载地址：{url}")

    return download


@pytest.fixture()
def snapshot_dir(tmp_path: Path) -> Path:
    """现造快照并跑完 ETL——多数用例直接消费 records.parquet。"""
    out = tmp_path / "gcat-2026Q3"
    fetch_snapshot(out, release="1.8.7", downloader=downloader(per_file_payload()))
    run_etl(out)
    return out


def read_records(snapshot_dir: Path) -> pd.DataFrame:
    return pd.read_parquet(snapshot_dir / RECORDS_PARQUET)


def test_si_conversions_declared_units(snapshot_dir: Path) -> None:
    """§7.5 规则 1：换算因子只来自 FIELD 声明表——吨→kg、kN→N、kNs→N·s、kg/s/m 原样。"""
    df = read_records(snapshot_dir)

    n1 = df[(df["table"] == "lv") & (df["name"] == "N-1 11A52")].iloc[0]
    assert n1["launch_mass_kg"] == pytest.approx(2_788_000.0)  # t → kg（千分位逗号已剥）
    assert n1["liftoff_thrust_n"] == pytest.approx(45_300_000.0)  # kN → N
    assert n1["length_m"] == pytest.approx(105.3)
    assert n1["payload_leo_kg"] == pytest.approx(70_000.0)  # kg 原样
    assert n1["payload_gto_kg"] is None or pd.isna(n1["payload_gto_kg"])  # "-" → 缺失
    # 无量纲计数（单位 "1"）：必须正常换算且**不得**误标——"1" 漏登记 UNIT_FACTORS 曾
    # 使真实快照全表被标 unit_uncertain（KeyError 被吞成标记）
    assert n1["min_stages"] == 1.0
    assert "min_stages" not in str(n1["unit_uncertain"])

    b1 = df[(df["table"] == "stages") & (df["name"] == "N-1 B1")].iloc[0]
    assert b1["full_mass_kg"] == pytest.approx(1_300_000.0)  # 吨 → kg
    assert b1["dry_mass_kg"] == pytest.approx(65_000.0)  # 官方口径本就是 kg，不换算
    assert b1["thrust_vacuum_n"] == pytest.approx(42_000_000.0)
    assert b1["thrust_sea_level_n"] == pytest.approx(38_000_000.0)  # 海平面另存字段（§7.5 规则 3）

    nk15 = df[(df["table"] == "engines") & (df["name"] == "NK-15")].iloc[0]
    assert nk15["isp_vacuum_s"] == pytest.approx(327.0)  # 真空比冲（官方定义）
    assert nk15["loaded_mass_kg"] == pytest.approx(1200.0)


def test_unit_uncertain_marks_but_keeps_values(snapshot_dir: Path) -> None:
    """§7.5 规则 2：解析失败（12ab）与出合理区间（99999 m / 132800 s）→ 标记 + 保留值。"""
    df = read_records(snapshot_dir)

    bad = df[(df["table"] == "lv") & (df["name"] == "BadRocket")].iloc[0]
    assert "length_m" in str(bad["unit_uncertain"])  # 99999 m 出 0.1–150 区间
    assert "launch_mass_kg" in str(bad["unit_uncertain"])  # 12ab 解析失败
    assert bad["length_m"] == pytest.approx(99999.0)  # 值保留，交给拟合环节排除

    weird = df[(df["table"] == "engines") & (df["name"] == "WeirdMotor")].iloc[0]
    # 132800 是"把 s 读成 m/s"的典型错值——isp 区间门禁（100–500）必须揪出
    assert "isp_vacuum_s" in str(weird["unit_uncertain"])
    assert weird["isp_vacuum_s"] == pytest.approx(132800.0)


def test_unparseable_thrust_flagged(snapshot_dir: Path) -> None:
    """'-kN' 这类带单位的脏值：解析失败 → None + 标记，绝不静默当 0。"""
    df = read_records(snapshot_dir)
    sic = df[(df["table"] == "stages") & (df["name"] == "S-IC")].iloc[0]
    assert "thrust_vacuum_n" in str(sic["unit_uncertain"])
    assert pd.isna(sic["thrust_vacuum_n"])


def test_quality_is_literature_with_flags_preserved(snapshot_dir: Path) -> None:
    """§7.6：GCAT 全部按 literature 入账；官方未文档化的 *Flag 原样留证。"""
    df = read_records(snapshot_dir)
    assert set(df["quality"].unique()) == {"literature"}

    n1 = df[(df["table"] == "lv") & (df["name"] == "N-1 11A52")].iloc[0]
    assert str(n1["source_flags"]) == ""  # 全 "-" 的 flag 不留噪声

    bad = df[(df["table"] == "lv") & (df["name"] == "BadRocket")].iloc[0]
    assert "LFlag=1" in str(bad["source_flags"])


def test_row_count_parity_and_idempotency(snapshot_dir: Path) -> None:
    """重跑覆盖幂等：两次 ETL 的 parquet 逐字节一致、行数一致。"""
    stats = run_etl(snapshot_dir)
    assert stats.rows == {"lv": 2, "stages": 2, "engines": 2}
    assert stats.total_records == 6

    first = (snapshot_dir / RECORDS_PARQUET).read_bytes()
    stats_again = run_etl(snapshot_dir)
    assert stats_again.rows == stats.rows
    assert (snapshot_dir / RECORDS_PARQUET).read_bytes() == first


def test_row_count_parity_fails_when_parsed_rows_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """manifest 登记 N 行、解析只得 N-1 行 → 必须抛错（防注释误判吞行那类回归）。"""
    out = tmp_path / "gcat-2026Q3"
    fetch_snapshot(out, release="1.8.7", downloader=downloader(per_file_payload()))

    from aeroforge.data.snapshot import read_table as real_read_table

    def dropping_read_table(text: str) -> tuple[list[str], list[dict[str, str]]]:
        header, rows = real_read_table(text)
        return header, rows[:-1]  # 偷偷丢一行，模拟"解析吞行"

    import aeroforge.data.etl as etl

    # 打补丁的目标是 etl 命名空间里的名字（run_etl 经模块全局调用它）；
    # 真函数本体取自 snapshot（单一来源，etl 只是导入使用，不做隐式再导出）。
    monkeypatch.setattr(etl, "read_table", dropping_read_table)
    with pytest.raises(ValueError, match="解析丢行"):
        run_etl(out)


def test_unknown_unit_is_a_config_error_not_a_data_flag() -> None:
    """FIELD 声明表里写了未登记的单位 → 当场 KeyError（配置错误必须炸成噪声）。

    曾经"KeyError → 吞成 unit_uncertain"，结果 `"1"` 漏登记让真实快照
    lv 全表 1836 行被误标——配置错伪装成数据疑点，两者必须分开。
    """
    from aeroforge.data.etl import FieldMap, _convert

    bogus = FieldMap("X", "x_m", "furlong")
    with pytest.raises(KeyError):
        _convert("10", bogus, set())


def test_table_frame_carries_provenance(snapshot_dir: Path) -> None:
    """每行必须能回查：来自哪张表、快照哪一批、原始第几行（红线 §1.4-4）。"""
    frame = table_frame(snapshot_dir, "lv")
    assert list(frame.columns[:5]) == [
        "table",
        "source_row",
        "snapshot",
        "quality",
        "unit_uncertain",
    ]
    assert set(frame["snapshot"].unique()) == {"gcat-2026Q3"}
    assert list(frame["source_row"]) == [1, 2]


def test_snapshot_tables_untouched(snapshot_dir: Path) -> None:
    """ETL 只读快照：跑完之后 TSV 的 sha256 必须仍与 manifest 一致。"""
    from aeroforge.data.snapshot import verify_snapshot

    run_etl(snapshot_dir)
    assert verify_snapshot(snapshot_dir) == []


def test_all_snapshot_tables_are_known() -> None:
    """防 SNAPSHOT_TABLES 增表而 ETL/夹具漏跟进的哨兵。"""
    assert set(SNAPSHOT_TABLES) == {"family", "orgs", "lv", "lvs", "stages", "engines"}
