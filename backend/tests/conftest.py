"""测试夹具：把可写数据目录重定向到临时目录（规格 §15 / §16.3）。

必须**重定向**而非写入仓库 ``data/``：产物目录是内容寻址的，测试若不隔离就会污染
开发机的缓存，使"新构建"与"缓存命中"两条路径随运行顺序而变，测试结论不再可信。
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from aeroforge.params.constraints import check_vehicle
from aeroforge.params.propellants import PropellantCombination
from aeroforge.params.report import has_hard
from aeroforge.params.schema import (
    Aero,
    Engine,
    Geometry,
    LaunchSite,
    Mission,
    Stage,
    StageSeparationType,
    Tank,
    Vehicle,
)
from aeroforge.paths import ENV_DATA_DIR


@pytest.fixture(scope="session", autouse=True)
def isolated_data_dir() -> Iterator[Path]:
    """整个测试会话共用一个临时数据根，退出时清理。

    ⚠ 数据根必须是临时目录**内的一层子目录**（``<tmp>/data``），不能直接用 ``<tmp>``：
    ``artifacts_root()`` 取 ``data_root().parent / "artifacts"``，若数据根就是
    ``<tmp>``，产物目录会落到**系统临时目录**里（``%TEMP%\\artifacts``）并跨会话留存，
    使"新构建"与"缓存命中"两条路径随历史运行而变（实测症状：第二次跑测试时
    ``cache_hit`` 意外为 True）。
    """
    with tempfile.TemporaryDirectory(prefix="aeroforge-tests-") as tmp:
        # ⚠ 必须先 resolve 再写入环境变量：GitHub Actions 的 Windows runner 里
        # %TEMP% 是 8.3 短名（C:\Users\RUNNER~1\AppData\Local\Temp），而
        # data_root() 会把该值 resolve 成长名（C:\Users\runneradmin\...）——
        # 同一物理目录、两种字符串形态，test_test_isolation 的三个门禁会
        # 误报"可写根逃出临时区"（CI 三连红即此因；本机用户名无短名差异，
        # 故本地从未复现）。在夹具边界统一成规范形，两侧口径天然一致。
        data_dir = Path(tmp).resolve() / "data"
        data_dir.mkdir()
        os.environ[ENV_DATA_DIR] = str(data_dir)
        from aeroforge.api import deps

        deps.reset_singletons()
        try:
            yield data_dir
        finally:
            deps.reset_singletons()
            os.environ.pop(ENV_DATA_DIR, None)


def make_stage(
    index: int,
    *,
    length_m: float,
    propellant: PropellantCombination = "LOX/RP-1",
    structure_coefficient: float = 0.05,
    tank_wall_thickness_m: float = 0.005,
    interstage_type: StageSeparationType = "none",
) -> Stage:
    """造一级合法参数（各层都填满，避免测试里到处 ``model_copy`` 补字段）。

    默认值刻意落在**全部工程判据的合格区间内**（长径比、结构质量比、壁厚），
    这样"正例夹具"与"负例只改一处"的写法都成立。
    """
    engine = Engine(
        model=f"test-engine-{index}",
        cycle="gas_generator",
        chamber_pressure_pa=9.7e6,
        expansion_ratio=16.0,
        efficiency_factor=0.98,
        thrust_sea_level_n=845_000.0,
        thrust_vacuum_n=981_000.0,
        isp_sea_level_s=282.0,
        isp_vacuum_s=311.0,
        mixture_ratio=2.36,
    )
    tank = Tank(
        tank_type="separate",
        wall_thickness_m=tank_wall_thickness_m,
        material="Al-2219",
        fill_fraction=0.95,
        feed_system="pump_fed",
    )
    return Stage(
        index=index,
        propellant=propellant,
        diameter_m=3.7,
        length_m=length_m,
        wall_thickness_m=0.005,
        material="Al-2219",
        structure_coefficient=structure_coefficient,
        fill_fraction=0.95,
        engine_count=9,
        engine=engine,
        engine_height_m=2.9,
        # 级间段类型描述的是"该级与其**上级**之间的分离段"（§5.9 共性 2），
        # 故最上级只能是 none。
        interstage_type=interstage_type,
        isp_vacuum_s=311.0,
        isp_sea_level_s=282.0,
        isp_source="default",
        geometry=Geometry(oxidizer_tank=tank, fuel_tank=tank.model_copy()),
    )


def _legal_vehicle(name: str, stages: tuple[Stage, ...]) -> Vehicle:
    """把若干级装成一架飞行器，并**就地断言它没有硬违反**。

    夹具必须过产品自己的校验器（教训 D 类：夹具自身违约，失败被误判成产品缺陷）。
    把断言放在夹具里而不是每条测试里，是为了让"夹具被改坏了"在任何一条测试上都可见。
    """
    vehicle = Vehicle(
        name=name,
        stages=stages,
        payload_mass_kg=22_800.0,
        material="Al-2219",
        propellant="LOX/RP-1",
        # 显式给出气动，避免正例夹具无端带一条 ENGINEER_AERO_DEFAULTED 警告
        aero=Aero(drag_coefficient=0.3),
        mission=Mission(
            orbit_type="LEO",
            altitude_m=200_000.0,
            inclination_deg=28.5,
            launch_site=LaunchSite(
                name="Cape Canaveral", latitude_deg=28.5, altitude_m=3.0, azimuth_deg=90.0
            ),
        ),
    )
    violations = check_vehicle(vehicle)
    assert not has_hard(violations), f"夹具 {name} 违反硬约束：{violations}"
    return vehicle


@pytest.fixture
def single_stage_vehicle() -> Vehicle:
    """合法单级飞行器（§6.1 Schema 的正例）。"""
    return _legal_vehicle("单级测试箭", (make_stage(1, length_m=41.2),))


@pytest.fixture
def two_stage_vehicle() -> Vehicle:
    """合法两级飞行器——用于验证逐级节点命名空间与整箭聚合（§6.2）。"""
    return _legal_vehicle(
        "两级测试箭",
        (make_stage(1, length_m=41.2, interstage_type="hot_staging"), make_stage(2, length_m=12.5)),
    )
