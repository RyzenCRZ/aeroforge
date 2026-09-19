"""几何同步端点（规格 §10.1 / §16.3）。

- ``POST /api/geometry/contour`` / ``GET /api/geometry/contour/{id}`` —— 母线保存/载入
- ``POST /api/geometry/validate`` —— 纯 Python 校验（**不触 OCCT**，可高频调用）
- ``POST /api/geometry/build`` —— 缓存命中同步返回，否则建异步作业

硬约束（规格 §9.1 规则 1）：**本模块的任何 handler 都不得调用 OCCT**。
故 ``validate`` 走纯 Python 的 :func:`~aeroforge.geometry.validate.validate_meridian`，
``build`` 只做键计算 + 入队。
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field

from aeroforge.api.deps import get_runner, get_store
from aeroforge.cache.store import ArtifactStore, compute_key
from aeroforge.errors import GeometryError
from aeroforge.geometry.meridian import (
    MeridianProfile,
    canonical_json,
    parse_profile,
)
from aeroforge.geometry.validate import ValidationReport, validate_meridian
from aeroforge.paths import contours_root, ensure_dir
from aeroforge.worker.jobs import GeometryJobRunner

router = APIRouter(tags=["geometry"])

#: 母线标识白名单：直接参与文件名拼接，故必须限定字符集（防目录穿越）。
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

ContourId = Annotated[str, Path(description="母线标识", pattern=_ID_PATTERN.pattern)]


class ContourSaveRequest(BaseModel):
    """保存母线的请求体。"""

    id: str | None = Field(default=None, description="母线标识；省略时按 name 派生或随机生成")
    profile: MeridianProfile = Field(description="母线剖面（段链，单位米）")


class ContourResponse(BaseModel):
    """母线响应体。``canonical`` 即落盘内容，往返一致的判据（规格 §16.3）。"""

    id: str
    canonical: str = Field(description="canonical JSON：键序固定、浮点定量、无多余空白")
    profile: MeridianProfile


class BuildResponse(BaseModel):
    """``POST /api/geometry/build`` 的响应体（§9.3 缓存优先）。"""

    cache_hit: bool = Field(description="True 表示产物已存在，metrics 直接可用，未创建作业")
    key: str = Field(description="内容寻址缓存键（sha256 十六进制）")
    job_id: str | None = Field(
        default=None, description="未命中时返回；用 /ws/jobs/{job_id} 订阅进度"
    )
    metrics: dict[str, Any] | None = Field(default=None, description="命中时的产物指标")


def _slug(name: str) -> str:
    """把剖面名转成可作文件名的标识；无可保留字符时回退随机串。"""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")[:48]
    return slug or uuid.uuid4().hex[:12]


def _path_for(contour_id: str) -> str:
    if not _ID_PATTERN.match(contour_id):
        msg = f"非法的母线标识：{contour_id!r}"
        raise GeometryError(
            msg,
            suggestion=(
                "标识只允许字母、数字、点、下划线与连字符，且以字母或数字开头（最长 64 字符）"
            ),
            details={"id": contour_id},
        )
    return f"{contour_id}.json"


@router.post("/api/geometry/contour", response_model=ContourResponse)
def save_contour(request: ContourSaveRequest) -> ContourResponse:
    """保存母线：归一化为 canonical JSON 落盘 ``data/contours/<id>.json``。

    落盘内容就是 canonical JSON 本体（不含包装元数据），因此
    "读取 → 再规范化" 必须与原文件**逐字节相同**——这正是往返一致的判据。
    """
    profile = request.profile
    contour_id = request.id or _slug(profile.name)
    filename = _path_for(contour_id)

    canonical = canonical_json(profile)
    target = ensure_dir(contours_root()) / filename
    target.write_text(canonical, encoding="utf-8")

    return ContourResponse(id=contour_id, canonical=canonical, profile=profile)


@router.get("/api/geometry/contour/{contour_id}", response_model=ContourResponse)
def load_contour(contour_id: ContourId) -> ContourResponse:
    """载入母线。文件不存在时返回 404（由 :class:`ArtifactNotFoundError` 映射）。"""
    filename = _path_for(contour_id)
    path = contours_root() / filename
    if not path.is_file():
        msg = f"母线 {contour_id!r} 不存在"
        raise GeometryError(
            msg,
            code="CONTOUR_NOT_FOUND",
            suggestion="先用 POST /api/geometry/contour 保存，或确认标识拼写",
            details={"id": contour_id, "path": str(path)},
        )

    raw = path.read_text(encoding="utf-8")
    profile = parse_profile(raw)
    # 以重新规范化的结果作为返回值：若磁盘内容被外部改动，此处会暴露差异而非静默接受。
    return ContourResponse(id=contour_id, canonical=canonical_json(profile), profile=profile)


@router.post("/api/geometry/validate", response_model=ValidationReport)
def validate_profile(profile: MeridianProfile) -> ValidationReport:
    """母线层校验（纯 Python，无内核）：几何自洽 + G1 + 尖点 + 设计意图断言。

    响应**同时返回分段采样点**（``sample`` / ``outline``），供前端 2D 剖面与示意通道消费。
    这是 ADR-011 的落实方式：端点数值全部来自后端，前端不做任何几何计算。
    """
    return validate_meridian(profile)


@router.post("/api/geometry/build", response_model=BuildResponse)
def build_geometry(
    profile: MeridianProfile,
    runner: Annotated[GeometryJobRunner, Depends(get_runner)],
    store: Annotated[ArtifactStore, Depends(get_store)],
) -> BuildResponse:
    """构建回转几何：**命中即同步返回**，未命中建异步作业（规格 §9.3）。"""
    key = compute_key(profile).key
    if store.is_cached(key):
        metrics = store.load_metrics(key)
        if metrics is not None:
            return BuildResponse(cache_hit=True, key=key, metrics=metrics)

    record = runner.submit(profile)
    return BuildResponse(cache_hit=False, key=key, job_id=record.job_id)
