"""§5.8 各格式 writer——**独立模块，不与建模职责耦合**（规格 §5.8 规则 2）。

每个 writer 只做一件事：把一个已构建的对象写成一个文件。构建（装配树）、
验证（§5.7 链）、缓存键与产物登记都归 :mod:`aeroforge.geometry.exportmod`
的编排层（``__init__.run_export``）——writer 不 import 装配 / 作业 / API 层。

格式与精度口径（§5.8 表）
--------------------------
- **STEP（AP214）**：``build123d export_step``（内核实测写 ``AUTOMOTIVE_DESIGN``
  = AP214），显式 ``unit=Unit.M``（§5.3 的 1000× 漂移教训）——**唯一权威交换格式**。
- **IGES**：build123d **无** ``export_iges``——直用内核 OCCT 的
  ``IGESControl_Writer``（``write.iges.unit = M``）。「内核缺该 API 则如实报错
  不硬凑」的守卫落在 :func:`write_iges` 顶部：内核若真缺 ``IGESControl``，
  以 :class:`~aeroforge.errors.ExportError` 报「能力缺失」，绝不用 STEP 改名
  冒充 IGES。
- **STL**：ASCII 形态（``ascii_format=True``，文件头 ``solid`` 可机检），
  网格容差显式声明并写入 provenance——STL 是**网格近似**，不是精确几何。
- **GLB**：复用既有 tessellate 链（:func:`aeroforge.geometry.revolve.export_glb`，
  LOD2 容差）。
- **报告类**（不走 OCCT）：参数 JSON（Vehicle canonical + 模板 sourced_fields
  若有）、CSV 质量预算（逐级 m_prop / m_dry + GLOW，与 evaluate 同一份质量账）、
  性能 JSON（``/api/perf/evaluate`` 点值输出原样）。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import build123d as bd

from aeroforge import SPEC_VERSION
from aeroforge.errors import ExportError
from aeroforge.geometry.meridian import round_floats
from aeroforge.geometry.revolve import (
    LOD2_ANGULAR,
    LOD2_DEFLECTION,
    export_glb,
    export_step,
)
from aeroforge.params.schema import Vehicle
from aeroforge.params.templates import match_template
from aeroforge.perf.capacity import vehicle_ledger
from aeroforge.perf.evaluate import compute_point_evaluation

#: STL 网格容差（§5.8：容差必须显式声明并记入 provenance）。取 LOD2 档——
#: STL 用于快速成型 / 快速验证，与交付级 GLB 同精度档。
STL_DEFLECTION = 0.001
STL_ANGULAR = 0.1

#: GLB 导出容差（复用 §5.4 的 LOD2 档：交付与精修）。
GLB_DEFLECTION = LOD2_DEFLECTION
GLB_ANGULAR = LOD2_ANGULAR


# ---------------------------------------------------------------------------
# 精确几何格式（BREP）
# ---------------------------------------------------------------------------


def write_step(shape: bd.Compound, path: Path) -> None:
    """STEP AP214（权威交换格式）：委托 :func:`aeroforge.geometry.revolve.export_step`。

    ``unit=Unit.M`` 在该函数内显式传递（§5.3 教训）；本 wrapper 的存在使
    编排层对全部格式持同一「格式名 → writer」分派形态。
    """
    export_step(shape, path)


def write_iges(shape: bd.Compound, path: Path) -> None:
    """IGES 5.x：直用 OCCT ``IGESControl_Writer``（build123d 无 ``export_iges``）。

    单位经 ``write.iges.unit = M`` 显式设定（内核默认 MM——与 STEP 的
    ``unit=Unit.M`` 教训同源）。内核若缺 ``IGESControl``（裁剪版 OCCT），
    如实报能力缺失，不用其它格式冒充（§1.4-4）。
    """
    try:
        from OCP.IFSelect import IFSelect_ReturnStatus  # type: ignore[import-untyped]
        from OCP.IGESControl import (  # type: ignore[import-untyped]
            IGESControl_Controller,
            IGESControl_Writer,
        )
        from OCP.Interface import Interface_Static  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - 内核裁剪防御（当前发行版全量带 IGES）
        raise ExportError(
            "几何内核缺少 IGES 写出能力（OCP.IGESControl 不可用）",
            suggestion="IGES 仅按需导出（§5.8）；STEP（AP214）是唯一权威交换格式，请改用 step",
        ) from exc

    IGESControl_Controller.Init_s()
    Interface_Static.SetCVal_s("write.iges.unit", "M")
    writer = IGESControl_Writer()
    if not writer.AddShape(shape.wrapped):
        raise ExportError(
            f"IGES 形状传输失败：{path.name}",
            suggestion="请先经 /api/geometry/validate 排查几何有效性，再重试导出",
        )
    if writer.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ExportError(
            f"IGES 写出失败：{path.name}",
            suggestion="检查产物目录可写性与磁盘空间后重试",
        )


# ---------------------------------------------------------------------------
# 网格格式（近似几何；§5.8 规则 4 下验证未通过时仍允许，附警告）
# ---------------------------------------------------------------------------


def write_stl(shape: bd.Compound, path: Path) -> None:
    """ASCII STL（文件头 ``solid``）：线性容差 / 角容差显式声明（网格近似）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = bd.export_stl(
        shape,
        str(path),
        tolerance=STL_DEFLECTION,
        angular_tolerance=STL_ANGULAR,
        ascii_format=True,
    )
    if not ok:
        raise ExportError(
            f"STL 导出失败：{path.name}",
            suggestion="检查产物目录可写性与磁盘空间后重试",
        )


def write_glb(shape: bd.Compound, path: Path) -> None:
    """二进制 GLB：复用既有 tessellate 链（LOD2 容差，``unit=Unit.M``）。"""
    export_glb(shape, path, deflection=GLB_DEFLECTION, angular=GLB_ANGULAR)


# ---------------------------------------------------------------------------
# 报告类（不走 OCCT）
# ---------------------------------------------------------------------------


def write_params_json(vehicle: Vehicle, path: Path) -> None:
    """参数 JSON：Vehicle canonical（键序固定 / 浮点定量 / 省略缺省）+ 出处标注。

    ``sourced_fields`` **若有**：车辆名命中内置模板（OI-34 规范化等值匹配）时
    附该模板的逐字段出处表——标注是「该参数的来源」，不担保当前值仍与出处一致
    （用户改动后出处语义转为「用户修改」，由前端溯源链维护，§11.5 ⑤ 规则 5）。
    """
    template = match_template(vehicle.name)
    payload = {
        "name": vehicle.name,
        "spec_version": SPEC_VERSION,
        "vehicle": round_floats(
            vehicle.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
        ),
        "sourced_fields": dict(template.sourced_fields) if template is not None else {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def write_mass_csv(vehicle: Vehicle, path: Path) -> None:
    """CSV 质量预算：逐级 m_prop / m_dry（+ 助推器合计 + 载荷）与 GLOW。

    质量账与 ``/api/perf/evaluate`` **同源**（:func:`vehicle_ledger`：几何解析
    推进剂 + σ 派生干重，助推器走 0 级段账）——CSV 的 GLOW 与 evaluate 的
    ``point.glow_kg`` 逐位一致是本报告的验收判据（§5.8 表「数据导出」）。
    """
    ledger = vehicle_ledger(vehicle)
    rows: list[list[str]] = [["kind", "stage_index", "propellant_kg", "dry_kg", "glow_kg"]]
    for stage in ledger.stages:
        rows.append(
            ["stage", str(stage.index), _num(stage.m_propellant_kg), _num(stage.m_dry_kg), ""]
        )
    prop_total = sum(stage.m_propellant_kg for stage in ledger.stages)
    dry_total = sum(stage.m_dry_kg for stage in ledger.stages)
    if ledger.zero_stage is not None:
        zero = ledger.zero_stage
        rows.append(
            ["booster", "", _num(zero.booster_propellant_kg), _num(zero.booster_dry_kg), ""]
        )
        prop_total += zero.booster_propellant_kg
        dry_total += zero.booster_dry_kg
    rows.append(["payload", "", "", "", ""])
    glow = ledger.glow_kg(vehicle.payload_mass_kg)
    rows.append(["total", "", _num(prop_total), _num(dry_total), _num(glow)])

    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def write_perf_json(vehicle: Vehicle, path: Path) -> None:
    """性能 JSON：``/api/perf/evaluate`` 的点值输出**原样**（阶段①载荷）。

    与 evaluate 的落盘缓存同形态（``cache_hit`` / ``interval_pending`` /
    ``mc_job_id`` 是请求期标记，不属于数据本体）；MC 区间走 ``/api/uncertainty/mc``
    作业通道，不在本报告重复。
    """
    response = compute_point_evaluation(vehicle)
    payload = response.model_dump(
        mode="json", exclude={"cache_hit", "interval_pending", "mc_job_id"}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def _num(value: float) -> str:
    """CSV 数值的确定性格式（6 位小数——kg 级质量的微克分辨率，足够对拍）。"""
    return f"{value:.6f}"


__all__ = [
    "GLB_ANGULAR",
    "GLB_DEFLECTION",
    "STL_ANGULAR",
    "STL_DEFLECTION",
    "write_glb",
    "write_iges",
    "write_mass_csv",
    "write_params_json",
    "write_perf_json",
    "write_step",
    "write_stl",
]
