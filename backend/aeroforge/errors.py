"""统一错误模型（规格 §10.3）。

规格硬要求：**错误必须包含可操作的修复建议**，"未知错误"式响应视为实现缺陷。
因此 :class:`AeroForgeError` 把 ``suggestion`` 设为必填，并对所有未预期的异常
在 :func:`to_error_body` 中给出兜底建议（含上报指引），而不是留空白。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    """§10.3 规定的错误响应体。"""

    code: str = Field(description="机器可读的错误码，如 GEOMETRY_G1_DISCONTINUITY")
    stage: str = Field(description="发生阶段：params / meridian / solid / mesh / step / …")
    message: str = Field(description="面向用户的中文说明")
    details: dict[str, Any] = Field(default_factory=dict, description="结构化上下文")
    suggestion: str = Field(description="可操作的修复建议（规格 §10.3 要求必填）")


class AeroForgeError(Exception):
    """带错误码与修复建议的应用异常基类。"""

    code = "INTERNAL_ERROR"
    stage = "unknown"

    def __init__(
        self,
        message: str,
        *,
        suggestion: str,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        stage: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion
        self.details: dict[str, Any] = details or {}
        if code is not None:
            self.code = code
        if stage is not None:
            self.stage = stage


class GeometryError(AeroForgeError):
    """几何域错误（母线不自洽、参数越界等）。"""

    code = "GEOMETRY_INVALID"
    stage = "meridian"


class KernelError(AeroForgeError):
    """几何内核（OCCT）失败。"""

    code = "GEOMETRY_KERNEL_FAILED"
    stage = "solid"


class ParamsError(AeroForgeError):
    """参数域错误：**硬约束**被违反（§6.3「拒绝并返回字段级错误」）。

    与 :class:`GeometryError` 分开，是因为二者虽同为 422，但阶段标签不同——
    §10.3 的 ``stage`` 要能定位到"哪一层拒绝了这次请求"。
    """

    code = "PARAMS_CONSTRAINT_VIOLATION"
    stage = "params"


class ConfigError(AeroForgeError):
    """配置文件（``config.toml``）不可用：语法错 / 阈值越界 / 出现未声明的键。

    自 §1.7.3 OI-31 起 ``config.toml`` 是**用户可见可编辑**的文件（界面设置项会写它，
    仓库里也有 ``config.example.toml`` 作参照），故它出错时必须走 §10.3 的带建议结构，
    而不是让 ``tomllib`` / pydantic 的原生异常以 ``INTERNAL_ERROR`` 冒到界面上——
    那样用户拿到的是一句"未预期的内部错误"，与"配置文件第 3 行写错了"差之千里。
    """

    code = "CONFIG_INVALID"
    stage = "config"


class ArtifactNotFoundError(AeroForgeError):
    """产物不存在。"""

    code = "ARTIFACT_NOT_FOUND"
    stage = "artifacts"

    def __init__(self, message: str, *, suggestion: str | None = None) -> None:
        super().__init__(
            message,
            suggestion=suggestion
            or "先调用 POST /api/geometry/build 生成产物；若刚清理过缓存请重新构建",
        )


class JobNotFoundError(AeroForgeError):
    """作业不存在（服务重启即清空作业表，属正常情形而非缺陷）。"""

    code = "JOB_NOT_FOUND"
    stage = "jobs"

    def __init__(self, message: str, *, suggestion: str | None = None) -> None:
        super().__init__(
            message, suggestion=suggestion or "确认 job_id 来自 POST /api/geometry/build 的响应"
        )


class CatalogNotFoundError(AeroForgeError):
    """GCAT 目录库未构建（``data/aeroforge.db`` 缺失）。"""

    code = "CATALOG_NOT_FOUND"
    stage = "data"

    def __init__(self, message: str, *, suggestion: str | None = None) -> None:
        super().__init__(
            message,
            suggestion=suggestion
            or (
                "先离线构建目录库（仓库根执行）："
                "uv run python tools/gcat_etl.py --out data/snapshots/gcat-2026Q3 && "
                "uv run python tools/gcat_db.py --snapshot data/snapshots/gcat-2026Q3"
            ),
        )


class CeaTableNotFoundError(AeroForgeError):
    """CEA 预计算表缺失或推进剂组合未收录（规格 §8.2 / ADR-004）。"""

    code = "CEA_TABLE_NOT_FOUND"
    stage = "perf"


class SizingError(AeroForgeError):
    """定尺求解域错误（§8.5）：输入不可解（Isp/σ/ΔV 组合无物理意义）或迭代不收敛。

    与 :class:`ParamsError` 分开：参数**结构**合法性归参数域；这里是「结构合法、
    但作为求解问题无解或解不出」——阶段标签 ``sizing`` 让 §10.3 的 ``stage``
    能定位到求解层。默认码 ``SIZING_SOLVE_FAILED``；不收敛路径用
    ``code="SIZING_NO_CONVERGENCE"``（details 携带残差轨迹，不静默给半收敛结果）；
    目标 ΔV 物理无解（低于构型可达下限 / 超出可达上限）用
    ``code="SIZING_INFEASIBLE_DV"``——调用方应「修目标」而非「重试」。
    """

    code = "SIZING_SOLVE_FAILED"
    stage = "sizing"


class CeaTableIntegrityError(AeroForgeError):
    """CEA 预计算表完整性校验失败：sha256 不符或 manifest 与表内容错位。

    ``data/`` 下的表是**不可变数据**（§15，禁止手改）；校验失败即拒绝加载，
    绝不静默降级为常数或旧表（静默回退正是 R-29 同族的"没报错 ≠ 正确"）。
    """

    code = "CEA_TABLE_INTEGRITY"
    stage = "perf"


class PerfError(AeroForgeError):
    """性能评估域错误（§8.6 / §8.8）：轨道要素缺失、构型对目标 ΔV 不可达等。

    与 :class:`SizingError` 分开：定尺是「给定载荷求质量」的设计问题（§8.5），
    性能评估是「给定火箭求运力 / ΔV 瀑布」的分析问题（§8.6）——阶段标签
    ``perf`` 让 §10.3 的 ``stage`` 能定位到评估层。
    """

    code = "PERF_EVALUATE_FAILED"
    stage = "perf"


class ExportError(AeroForgeError):
    """导出域错误（§5.8）：格式能力缺失（内核无该 writer）、写出失败等。

    阶段标签 ``export`` 定位到导出层；几何构建本身的问题仍以几何域错误
    （:class:`GeometryError` / :class:`AssemblyError`）冒出，不在此冒名。
    """

    code = "EXPORT_FAILED"
    stage = "export"


class ExportValidationError(AeroForgeError):
    """§5.8 规则 4 的拒绝：精确格式导出前 §5.7 验证未通过。

    ``details.diagnostics`` 携带验证失败的诊断摘要（约束裁定 / 装配校验的
    fail 项）；按规格**只允许导出网格格式并附警告**——本错误只针对被请求的
    精确格式（STEP / IGES）。
    """

    code = "EXPORT_VALIDATION_FAILED"
    stage = "export"


def to_error_body(exc: BaseException) -> ErrorBody:
    """把任意异常收敛为 §10.3 响应体。"""
    if isinstance(exc, AeroForgeError):
        return ErrorBody(
            code=exc.code,
            stage=exc.stage,
            message=exc.message,
            details=exc.details,
            suggestion=exc.suggestion,
        )
    return ErrorBody(
        code="INTERNAL_ERROR",
        stage="unknown",
        message=f"{type(exc).__name__}: {exc}",
        details={"exception": type(exc).__name__},
        suggestion=(
            "这是未预期的内部错误。请附上复现步骤与所选参数提交 issue；"
            "若为冻结产物，请同时附上 AeroForge.exe --preflight 的输出"
        ),
    )
