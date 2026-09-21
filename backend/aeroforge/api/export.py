"""多格式导出端点（规格 §5.8 / §10.1 ``POST /api/export``，M5 第三片）。

异步走既有几何作业体系（OCCT 专用单线程，§9.1 硬规则 3：本 handler **不触
OCCT**——只做微秒级的参数校验 / 布局复核与入队）。

同步拦截（§5.8 规则 4 的 API 侧，全部 4xx + diagnostics）：
- 未知格式 → 422（请求体枚举校验）；
- 参数硬约束违反 + 请求含**非网格格式**（精确或报告）→ 422
  ``PARAMS_CONSTRAINT_VIOLATION``（diagnostics 摘要在响应）——「只允许导出
  网格格式并附警告」的字面口径；
- 布局不可行（分区铺不满 / 矢高干涉，plan_stage 纯数值复核）+ 请求含几何
  格式 → 422 ``GEOMETRY_INVALID``（网格同样需要可构建的几何，故一并拒绝）。

纯网格请求（stl / glb）在硬约束违反时**放行**——作业结果附警告（规则 4 的
降级路径：快速验证场景合法）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from aeroforge.api.deps import get_runner
from aeroforge.cache.store import (
    ARTIFACT_EXPORT_GLB,
    ARTIFACT_EXPORT_IGES,
    ARTIFACT_EXPORT_MASS,
    ARTIFACT_EXPORT_PARAMS,
    ARTIFACT_EXPORT_PERF,
    ARTIFACT_EXPORT_STEP,
    ARTIFACT_EXPORT_STL,
)
from aeroforge.errors import GeometryError, ParamsError
from aeroforge.geometry.assembly import AssemblyError, plan_stage
from aeroforge.geometry.exportmod import (
    MESH_FORMATS,
    REPORT_FORMATS,
    ExportFormat,
)
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.report import has_hard
from aeroforge.params.schema import Vehicle
from aeroforge.worker.jobs import GeometryJobRunner

router = APIRouter(tags=["export"])


class ExportRequest(BaseModel):
    """``POST /api/export`` 的请求体（§5.8 表的格式枚举 + 飞行器参数）。"""

    model_config = ConfigDict(extra="forbid")

    vehicle: Vehicle = Field(description="飞行器参数（与 /api/geometry/build 车辆形态同构）")
    formats: tuple[ExportFormat, ...] = Field(
        min_length=1,
        description=(
            "导出格式：step / iges（精确 BREP，受 §5.8 规则 4 验证门禁）；"
            "stl / glb（网格近似，验证未通过时附警告导出）；"
            "params_json / mass_csv / perf_json（报告类，不走 OCCT）"
        ),
    )


class ExportResponse(BaseModel):
    """``POST /api/export`` 的响应体（异步受理；作业完成经 /api/jobs/{id} 取回）。"""

    job_id: str = Field(description="导出作业 id（GET /api/jobs/{id} / WS 订阅进度）")
    formats: tuple[ExportFormat, ...] = Field(
        description="本次受理的格式清单（去重保序）；产物经 /api/artifacts/{key}/{file} 取回"
    )
    files: dict[ExportFormat, str] = Field(
        description=(
            "格式 → 产物文件名映射（/api/artifacts/{key}/{file} 的 file 段）。"
            "命名单一事实源在后端（cache.store 的 ARTIFACT_EXPORT_* 常量），"
            "前端按此映射下载，禁止自行拼接（ADR-011 同族纪律）"
        )
    )


#: 格式 → 产物文件名（与 cache.store 的 ARTIFACT_EXPORT_* 常量同源——
#: 单处定义，此处消费；新增格式必须两处同步并由白名单测试钉住）。
_EXPORT_FILENAMES: dict[ExportFormat, str] = {
    "step": ARTIFACT_EXPORT_STEP,
    "iges": ARTIFACT_EXPORT_IGES,
    "stl": ARTIFACT_EXPORT_STL,
    "glb": ARTIFACT_EXPORT_GLB,
    "params_json": ARTIFACT_EXPORT_PARAMS,
    "mass_csv": ARTIFACT_EXPORT_MASS,
    "perf_json": ARTIFACT_EXPORT_PERF,
}


@router.post("/api/export", response_model=ExportResponse)
def export_formats(
    request: ExportRequest,
    runner: Annotated[GeometryJobRunner, Depends(get_runner)],
) -> ExportResponse:
    """多格式导出（异步）：STEP/IGES/STL/GLB + 参数/质量/性能报告（§5.8）。

    产物落位：几何格式 → ``artifacts/<构建键>/export/``；报告类 →
    ``artifacts/report-<hash>/export/``（不新建几何缓存键——导出是构建产物的
    衍生，§9.2）。文件名 ``export.step`` / ``export.stl`` / ``export.mass.csv``…
    沿用 artifacts 白名单通道取回。
    """
    formats = _dedupe(request.formats)
    vehicle = request.vehicle

    # ── 规则 4 同步门禁 1：参数硬约束（§6.3——验证链第一环）──
    violations = check_vehicle(vehicle)
    if has_hard(violations) and set(formats) - MESH_FORMATS:
        hard = [item for item in violations if item.level == "hard"]
        raise ParamsError(
            f"参数违反 {len(hard)} 条硬约束，拒绝导出非网格格式（§5.8 规则 4）",
            suggestion=(
                f"{hard[0].suggestion}；修复后重试。"
                "仅请求网格格式（stl / glb）可带警告导出（快速验证场景）"
            ),
            details={"diagnostics": [item.model_dump(mode="json") for item in violations]},
        )

    # ── 规则 4 同步门禁 2：布局复核（纯数值，与 sections 端点同判据）──
    #    报告类不消费几何，不做布局门禁（与 /api/perf/evaluate 的口径一致）。
    if any(fmt not in REPORT_FORMATS for fmt in formats):
        try:
            for stage in sorted(vehicle.stages, key=lambda item: item.index):
                plan_stage(stage)
            for group in vehicle.boosters:
                plan_stage(group.stage)
        except AssemblyError as exc:
            raise GeometryError(
                str(exc),
                suggestion="分区必须恰好铺满级长且封头不干涉（§5.9 / §5.5）；"
                "请调整级长、扁度或发动机高度",
            ) from exc

    record = runner.submit_export(vehicle, formats)
    return ExportResponse(
        job_id=record.job_id,
        formats=formats,
        files={fmt: _EXPORT_FILENAMES[fmt] for fmt in formats},
    )


def _dedupe(formats: tuple[ExportFormat, ...]) -> tuple[ExportFormat, ...]:
    """去重保序（同一格式只写一次产物）。"""
    seen: set[str] = set()
    ordered: list[ExportFormat] = []
    for fmt in formats:
        if fmt not in seen:
            seen.add(fmt)
            ordered.append(fmt)
    return tuple(ordered)


__all__ = [
    "ExportRequest",
    "ExportResponse",
    "export_formats",
    "router",
]
