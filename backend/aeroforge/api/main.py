"""应用装配与路由注册。

启动命令（规格附录 D）：
    uv run uvicorn aeroforge.api.main:app --reload

**注册顺序即匹配优先级**：API 路由在前，静态兜底路由在后（由
:func:`aeroforge.api.static.mount_frontend` 在启动器里最后挂载）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from aeroforge import __version__
from aeroforge.api import artifacts, geometry, jobs
from aeroforge.api.deps import get_runner, reset_singletons
from aeroforge.errors import AeroForgeError, ErrorBody, to_error_body
from aeroforge.geometry.meridian import MeridianError

#: 错误码 → HTTP 状态。未列出的按 500 处理（未知即内部缺陷，不做猜测）。
_STATUS_BY_CODE: dict[str, int] = {
    "CONTOUR_NOT_FOUND": 404,
    "ARTIFACT_NOT_FOUND": 404,
    "JOB_NOT_FOUND": 404,
    "GEOMETRY_INVALID": 422,
    "GEOMETRY_G1_DISCONTINUITY": 422,
    "GEOMETRY_KERNEL_FAILED": 500,
}

_STATUS_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    422: "REQUEST_INVALID",
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """绑定事件循环给作业执行器，退出时收线（规格 §9.1 硬规则 1）。

    工作线程需要 ``loop.call_soon_threadsafe`` 才能把进度投给 WS 订阅者，
    故必须在启动时把当前运行循环交出去；停机时收线程并丢弃单例，
    保证进程干净退出（§16.2「退出」契约），且"启动→停机→再启动"能拿到新线程。
    """
    runner = get_runner()
    runner.bind_loop(asyncio.get_running_loop())
    try:
        yield
    finally:
        reset_singletons()


app = FastAPI(
    title="AeroForge",
    version=__version__,
    description="航天飞行器参数化建模与性能评估平台",
    lifespan=lifespan,
)

app.include_router(geometry.router)
app.include_router(jobs.router)
app.include_router(artifacts.router)


def _error_response(status_code: int, body: ErrorBody) -> JSONResponse:
    """§10.3 契约：错误体统一包在 ``error`` 键下。"""
    return JSONResponse(status_code=status_code, content={"error": body.model_dump(mode="json")})


@app.exception_handler(AeroForgeError)
async def handle_app_error(request: Request, exc: AeroForgeError) -> JSONResponse:
    """应用域异常：错误码决定状态；未登记的码按 500 处理（不猜测语义）。"""
    return _error_response(_STATUS_BY_CODE.get(exc.code, 500), to_error_body(exc))


@app.exception_handler(MeridianError)
async def handle_meridian_error(request: Request, exc: MeridianError) -> JSONResponse:
    """母线域错误（在 pydantic 校验之外抛出时，如 validate 的 resolve 阶段）。"""
    return _error_response(
        422,
        ErrorBody(
            code="GEOMETRY_INVALID",
            stage="meridian",
            message=str(exc),
            suggestion=(
                "请检查母线剖面：每段 length 必须为正；穹顶段（arc / ellipse）两端中"
                "**恰有一端**必须落在轴线上（半径 0）"
            ),
        ),
    )


@app.exception_handler(RequestValidationError)
async def handle_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    """请求体不符合契约——把 pydantic 的报错翻译成 §10.3 结构，而非默认的 detail 列表。"""
    return _error_response(
        422,
        ErrorBody(
            code="REQUEST_INVALID",
            stage="params",
            message="请求体不符合接口契约",
            details={"errors": exc.errors()},
            suggestion="按 OpenAPI schema 校正字段名与类型（前端类型由 npm run gen:api 生成）",
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """HTTP 异常也走 §10.3 结构，避免同一前端要解析两种错误形状。"""
    return _error_response(
        exc.status_code,
        ErrorBody(
            code=_STATUS_CODES.get(exc.status_code, "HTTP_ERROR"),
            stage="api",
            message=str(exc.detail),
            suggestion="检查请求路径与方法；API 列表见 GET /openapi.json",
        ),
    )


@app.exception_handler(Exception)
async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """兜底：未预期异常也必须给出**可操作**的建议（§10.3 禁止"未知错误"）。"""
    return _error_response(500, to_error_body(exc))


class HealthResponse(BaseModel):
    """``GET /api/health`` 的响应体。

    作为 OpenAPI schema 的来源，前端 TS 类型由此生成（规格 §18.2：禁止手写重复类型）。
    """

    status: str
    name: str
    version: str


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """存活探针。

    M0 验收项（规格 §16）：必须返回版本号，供前端与 CI 核对后端已就绪。
    """
    return HealthResponse(status="ok", name="aeroforge", version=__version__)
