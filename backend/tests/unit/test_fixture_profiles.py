"""测试夹具合法性门禁：夹具必须先过产品自己的校验器。

由来（教训 D 类：夹具自身违反契约，失败被误判成产品缺陷）
------------------------------------------------------
M1 期间测试夹具曾用 ``base_radius=1.0`` 配 ``arc(1.0 → 0.0)``——在轴线处产生退化，
导致两项测试失败。当时的正确处置是**修夹具而不是放宽断言**，本文件把这条处置固定下来。

规则
----
- 自动发现测试包内所有「可零参调用且返回 ``MeridianProfile``」的工厂函数；
- 每个工厂**必须**在下表显式归类：出现未归类的新夹具即失败——逼迫作者表态，
  而不是让新夹具悄悄溜过（这正是当初那个非法夹具的形态）；
- ``LEGAL``：``validate_meridian().ok`` 必须为真；
- ``LEGAL_WITH_WARNING``：``ok`` 为真**且**报告里确实存在 ``warn`` 项——``warn``
  计入通过，但必须留痕，不允许变成"悄悄通过"；
- ``ILLEGAL``：``ok`` 必须为假，否则对应的负向测试已失去意义。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType

import pytest

from aeroforge.geometry.meridian import MeridianProfile
from aeroforge.geometry.validate import validate_meridian


def _test_modules() -> tuple[ModuleType, ...]:
    """自动发现本包内全部 ``test_*`` 模块。

    曾用一张**手写的模块清单**——那等于给"新模块里的夹具"留了后门：不登记就永不检查，
    与"未归类的新夹具即失败"自相矛盾。改为按包扫描后，新增测试文件自动纳入。
    """
    package_name = __package__ or "tests.unit"
    package = importlib.import_module(package_name)
    discovered: list[ModuleType] = []
    for info in pkgutil.iter_modules(package.__path__, prefix=f"{package_name}."):
        if not info.name.rsplit(".", 1)[-1].startswith("test_"):
            continue
        discovered.append(importlib.import_module(info.name))
    return tuple(discovered)


#: 必须通过校验的夹具
LEGAL = frozenset(
    {
        "_cylinder",
        "_cone",
        "_sphere_cap",
        "_ellipsoid_cap",
        "_common_bulkhead",
        "_profile",
        "_g1_clean",
        "_axial_cylinder",
        "_axial_capsule",
    }
)

#: 通过但必须留痕的夹具（轴处反向尖点：数学合法，OCCT 会判退化）
LEGAL_WITH_WARNING = frozenset({"_pinch"})

#: 必须被拒的夹具
ILLEGAL = frozenset({"_g1_violation"})


def _factories() -> dict[str, MeridianProfile]:
    """发现测试模块顶层的剖面工厂，并即时调用取一个实例。"""
    found: dict[str, MeridianProfile] = {}
    for module in _test_modules():
        for name, member in inspect.getmembers(module, inspect.isfunction):
            if not name.startswith("_"):
                continue
            signature = inspect.signature(member)
            if signature.return_annotation not in (MeridianProfile, MeridianProfile.__name__):
                continue
            required = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                and parameter.default is inspect.Parameter.empty
            ]
            if required:
                continue
            assert name not in found, f"夹具工厂 {name} 在多个测试模块中重名，无法唯一归类"
            found[name] = member()
    return found


def _failures(report: object) -> list[str]:
    """把不通过的检查项缩成可读清单（失败信息里要直接看到原因）。"""
    checks = getattr(report, "checks", [])
    return [f"{check.check}:{check.severity}({check.detail})" for check in checks if not check.ok]


def test_all_profile_factories_are_classified() -> None:
    """未归类的新夹具必须让门禁失败——否则新夹具会绕开合法性检查。"""
    discovered = set(_factories())
    classified = LEGAL | LEGAL_WITH_WARNING | ILLEGAL
    assert discovered, "未发现任何剖面工厂——扫描规则可能已失效（静默漏检比漏测更危险）"
    assert discovered == classified, (
        f"未归类的夹具：{sorted(discovered - classified)}；"
        f"已归类但已不存在（改名后忘记同步）：{sorted(classified - discovered)}"
    )


@pytest.mark.parametrize("name", sorted(LEGAL))
def test_legal_fixture_passes_product_validation(name: str) -> None:
    """正例夹具必须过产品自己的校验器——夹具非法时应修夹具，而不是放宽断言。"""
    report = validate_meridian(_factories()[name])
    assert report.ok, f"{name} 应通过校验，实际失败项：{_failures(report)}"


@pytest.mark.parametrize("name", sorted(LEGAL_WITH_WARNING))
def test_legal_with_warning_fixture_leaves_a_trace(name: str) -> None:
    """``warn`` 计入通过，但必须在报告里留痕——不允许变成"悄悄通过"。"""
    report = validate_meridian(_factories()[name])
    assert report.ok, f"{name} 应通过校验，实际失败项：{_failures(report)}"
    assert any(check.severity == "warn" for check in report.checks), (
        f"{name} 必须以 warn 留痕，否则它就不再是「有解释的例外」而是被静默放过"
    )


@pytest.mark.parametrize("name", sorted(ILLEGAL))
def test_illegal_fixture_is_rejected(name: str) -> None:
    """负例夹具必须真的被拒——否则对应的负向测试形同虚设。"""
    report = validate_meridian(_factories()[name])
    assert not report.ok, f"{name} 本应被拒却通过了，其负向测试已失去意义"
