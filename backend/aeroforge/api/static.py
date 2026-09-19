"""前端构建产物的静态托管（ADR-015）。

打包后不存在 Vite 开发服务器：后端必须自己把 ``frontend/dist/`` 供出去，
并对未知路径回落 ``index.html``，否则前端 SPA 的深链与刷新会 404。

本模块只负责"托管"，不负责"构建"。开发期该目录可能不存在（未跑 ``npm run build``），
此时静默跳过挂载，API 仍可正常工作。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

#: API 前缀。该前缀下的未匹配路径必须返回 404，**不得**回落到 index.html——
#: 否则前端把 HTML 当 JSON 解析，会得到难以定位的报错而不是清晰的 404。
API_PREFIX = "api/"


def mount_frontend(app: FastAPI, dist_dir: Path) -> bool:
    """把前端构建产物挂到 ``app`` 上，返回是否实际挂载。

    Args:
        app: FastAPI 应用实例。调用前其所有 API 路由必须已注册完毕——
            本函数添加的是兜底路由，注册顺序决定匹配优先级。
        dist_dir: ``frontend/dist`` 目录（构建产物根）。

    Returns:
        实际完成挂载返回 ``True``；目录不存在（如开发期未构建）返回 ``False``。
    """
    if not dist_dir.is_dir():
        return False

    index_file = dist_dir / "index.html"
    if not index_file.is_file():
        return False

    # 幂等保护：重复调用会叠加多条兜底路由，导致同一请求被多次匹配。
    if getattr(app.state, "frontend_mounted", False):
        return False

    # resolve() 一次即可：用于后续的目录穿越校验，避免每个请求重复解析。
    root = dist_dir.resolve()

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str) -> FileResponse:
        """优先返回真实存在的静态文件，否则回落 index.html。"""
        if full_path.startswith(API_PREFIX):
            # 未匹配的 API 路径必须得到 JSON 404，而不是被 HTML 回落掩盖成解析错误。
            raise HTTPException(status_code=404, detail="Not Found")

        candidate = (root / full_path).resolve()
        # 目录穿越防护：请求路径经 URL 解码后可能含 ../，必须确认仍落在 dist 内。
        if candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)

        return FileResponse(index_file)

    app.state.frontend_mounted = True
    return True
