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


class CeaTableIntegrityError(AeroForgeError):
    """CEA 预计算表完整性校验失败：sha256 不符或 manifest 与表内容错位。

    ``data/`` 下的表是**不可变数据**（§15，禁止手改）；校验失败即拒绝加载，
    绝不静默降级为常数或旧表（静默回退正是 R-29 同族的"没报错 ≠ 正确"）。
    """

    code = "CEA_TABLE_INTEGRITY"
    stage = "perf"


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
