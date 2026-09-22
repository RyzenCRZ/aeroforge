"""作业状态端点（规格 §9.3 / §10.2 / §16.3）。

- ``GET /api/jobs/{job_id}`` —— 轮询式状态查询（WS 不可用时的降级路径）
- ``WS /ws/jobs/{job_id}`` —— 进度流

WS 消息体是 :class:`~aeroforge.worker.jobs.JobRecord` 的完整 JSON 快照
（字段为 §10.2 示例 ``{stage, progress}`` 的**超集**：额外含 ``status`` / ``result_key`` /
``error`` 等，前端无需另开一次 HTTP 才能拿到终态结果）。

**竞态说明**：先 ``subscribe`` 再读快照，因此不会丢事件；代价是可能重复收到
某个中间态（快照与队列里的事件重叠）。前端按"最新到达者胜"渲染即可，无需去重。
"""

from __future__ import annotations

import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState

from aeroforge.api.deps import get_runner
from aeroforge.errors import JobNotFoundError
from aeroforge.worker.jobs import JobRecord, JobStatus

router = APIRouter(tags=["jobs"])

#: 终态集合：到达即关闭通道（前端据此停止重连）。
_TERMINAL = {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}

#: WS 关闭码 1008 = Policy Violation（作业不存在，属于请求方错误而非服务端故障）。
_WS_POLICY_VIOLATION = 1008


@router.get("/api/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str) -> JobRecord:
    """查询作业快照（几何 / MC 两级执行器共用同一份作业簿，§9.1）。"""
    record = get_runner().get(job_id)
    if record is None:
        raise JobNotFoundError(
            f"作业 {job_id!r} 不存在",
            suggestion=(
                "确认 job_id 来自 POST /api/geometry/build 或 POST /api/uncertainty/mc"
                "（/api/perf/evaluate 的 mc_job_id）的响应；服务重启会清空作业表"
            ),
        )
    return record


@router.websocket("/ws/jobs/{job_id}")
async def stream_job(websocket: WebSocket, job_id: str) -> None:
    """把作业进度推给客户端，终态后主动关闭。"""
    runner = get_runner()
    await websocket.accept()

    target = runner.subscribe(job_id)
    if target is None:
        await websocket.send_json(
            {
                "code": "JOB_NOT_FOUND",
                "message": f"作业 {job_id!r} 不存在",
                "suggestion": (
                    "确认 job_id 来自 POST /api/geometry/build 或 POST /api/uncertainty/mc 的响应"
                ),
            }
        )
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    try:
        snapshot = runner.get(job_id)
        if snapshot is not None:
            await websocket.send_json(snapshot.model_dump(mode="json"))
            if snapshot.status.value in _TERMINAL:
                return
        while True:
            payload = await target.get()
            await websocket.send_json(payload)
            if payload.get("status") in _TERMINAL:
                return
    except WebSocketDisconnect:
        return
    finally:
        runner.unsubscribe(job_id, target)
        if websocket.client_state is WebSocketState.CONNECTED:
            # M7 探针实测竞态：客户端断开与 uvicorn 侧已发 close 之间存在窗口，
            # close() 会抛 RuntimeError（"Cannot call send once a close message has
            # been sent"）——订阅已摘除，此处兜底吞掉即可，通道异常不外溢（§10.2 同款纪律）。
            with contextlib.suppress(Exception):
                await websocket.close()
