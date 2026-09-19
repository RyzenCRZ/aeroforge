"""导入端点（规格 §7.5 / §10.1：``POST /api/import/motor``）。

按文件扩展名分派到 :mod:`aeroforge.data.parsers`（纯解析层），本模块只负责
multipart 的字节读取与错误映射：

- 解析失败（``IMPORT_PARSE_FAILED``）→ 422，错误体带行号/XPath（R-21）
- 扩展名不支持（``IMPORT_FORMAT_UNSUPPORTED``）→ 400
- 响应字段名带 SI 单位后缀（``_ns`` / ``_s`` / ``_n`` / ``_kg``），``.ork``
  结果整体标注 ``derived=True``

multipart/form-data 用**标准库** ``email.parser`` 解析（本端点只需要单个
``file`` 文件字段）：starlette/FastAPI 的表单解析在缺失 ``python-multipart``
时不可用，而 §3.4 规定新增依赖必须先走审计流程——故在此做最小 stdlib 实现。
"""

from __future__ import annotations

from email import policy
from email.parser import BytesParser

from fastapi import APIRouter, HTTPException, Request

from aeroforge.data.parsers import (
    MotorParseResult,
    RocketParseResult,
    decode_import_bytes,
    parse_import_text,
)

router = APIRouter(tags=["import"])


@router.post("/api/import/motor", response_model=MotorParseResult | RocketParseResult)
async def import_motor(request: Request) -> MotorParseResult | RocketParseResult:
    """导入发动机/火箭模型文件，返回带单位后缀的 SI 字段与 warnings。

    同步响应端点（规格 §10.1：非作业、无 job_id）。multipart 表单须含名为
    ``file`` 的文件字段；解析失败显式报错（R-21），不静默容错。
    """
    body = await request.body()
    found = _extract_upload(body, request.headers.get("content-type", ""))
    if found is None:
        raise HTTPException(
            status_code=422,
            detail="multipart 表单缺少名为 file 的文件字段（.eng / .rse / .ork）",
        )
    filename, data = found
    text = decode_import_bytes(data, filename)
    return parse_import_text(text, filename)


def _extract_upload(body: bytes, content_type: str) -> tuple[str, bytes] | None:
    """从 multipart/form-data 请求体取出 ``file`` 文件字段（filename, 内容）。

    结构异常（非 multipart、无该字段）返回 ``None``，由调用方统一报 422。
    """
    if "multipart/form-data" not in content_type.lower():
        return None
    # 借 email 包解析 MIME：给请求体补上 Content-Type 头即可当作报文解析。
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + body
    )
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        if part.get_param("name", header="content-disposition") != "file":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            return None
        return part.get_filename() or "", payload
    return None
