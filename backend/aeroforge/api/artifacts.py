"""产物与溯源端点（规格 §10.1 / §16.3）。

- ``GET /api/artifacts/{key}/{file}`` —— 取产物（GLB / STEP / JSON）
- ``GET /api/provenance/{key}`` —— 产物清单（缓存键构成 + 各阶段耗时）

**只服务白名单内的文件**：文件名不参与路径拼接后再校验，而是先在
:data:`~aeroforge.cache.store.ALLOWED_ARTIFACTS` 里查表，查不到即 404。
缓存键同理限定为 64 位十六进制。两道限制合起来使目录穿越在本端点上不可表达。
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from fastapi.responses import FileResponse

from aeroforge.api.deps import get_store
from aeroforge.cache.store import ALLOWED_ARTIFACTS, ARTIFACT_MEDIA_TYPES, ArtifactStore
from aeroforge.errors import ArtifactNotFoundError

router = APIRouter(tags=["artifacts"])

#: 缓存键形状：sha256 十六进制（构建产物），或带语义前缀的同长摘要（§5.8 报告类
#: ``report-``——几何 kernel 版本不参与报告键）。限定形状即可排除 ``..`` 等穿越尝试。
_KEY_PATTERN = re.compile(r"^(report-)?[0-9a-f]{64}$")

ArtifactKey = Annotated[str, Path(description="内容寻址缓存键", pattern=_KEY_PATTERN.pattern)]


@router.get("/api/artifacts/{key}/{file}")
def get_artifact(
    key: ArtifactKey,
    file: str,
    store: Annotated[ArtifactStore, Depends(get_store)],
) -> FileResponse:
    """返回产物文件；不在白名单或文件缺失时 404。"""
    if file not in ALLOWED_ARTIFACTS:
        raise ArtifactNotFoundError(
            f"产物 {file!r} 不在允许列表中",
            suggestion=f"可取的产物为：{', '.join(sorted(ALLOWED_ARTIFACTS))}",
        )
    path = store.file_path(key, file)
    if path is None:
        raise ArtifactNotFoundError(
            f"产物不存在：{key}/{file}",
            suggestion="该缓存键下尚无产物。先调用 POST /api/geometry/build，命中后即可取用",
        )
    return FileResponse(path, media_type=ARTIFACT_MEDIA_TYPES[file], filename=file)


@router.get("/api/provenance/{key}")
def get_provenance(
    key: ArtifactKey,
    store: Annotated[ArtifactStore, Depends(get_store)],
) -> dict[str, object]:
    """返回产物清单（provenance.json 内容），供溯源与排障（§9.2 / R-14）。"""
    manifest = store.load_manifest(key)
    if manifest is None:
        raise ArtifactNotFoundError(
            f"缓存键 {key} 无溯源清单",
            suggestion="先调用 POST /api/geometry/build 生成产物；清单与产物同时写入",
        )
    return manifest.model_dump(mode="json")
