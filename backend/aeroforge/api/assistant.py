"""AI 助手通道（规格 §10.2 / OI-10 / §11.5 ④）。

REST
----
- ``POST /api/assistant/config`` —— 写入 ``base_url / api_key / model``。
  ``api_key`` 为**只写**字段：请求里 ``null`` = 保留现有值，空串 = 清除（回到
  未配置）；任何响应都只回**脱敏形态**（``sk-…abcd``），明文不回传、不落日志。
- ``GET /api/assistant/status`` —— ``{configured, reachable, …}``。
  ``reachable`` 是**最近一次对话**的连通性标记（惰性：不主动探测、不阻塞启动），
  ``None`` = 从未尝试。

``/ws/assistant`` 对话流（§10.2 的可选通道）
--------------------------------------------
客户端 → 服务端（JSON）::

    {"type": "user_message", "text": "<自然语言>", "vehicle": {…当前参数…}}
    {"type": "apply_diff",  "diff_id": "…"}      # 确认制：用户点了「确认应用」
    {"type": "reject_diff", "diff_id": "…"}

服务端 → 客户端（JSON，按序）::

    {"type": "ready", "configured": bool}        # 连接即发
    {"type": "assistant_delta", "text": …}       # 上游流式增量
    {"type": "assistant_text", "text": …}        # 最终整段文本（覆盖渲染）
    {"type": "param_diff", "diff_id": …, "changes": [{path, old, new}], "vehicle": {…候选…}}
    {"type": "diff_applied" / "diff_rejected", "diff_id": …}
    {"type": "diagnosis", "constraints" | "diagnostics" | "rules" | "note"}   # 本地规则集，零 LLM
    {"type": "instruction_rejected" | "instruction_invalid", …}  # AI 指令被拒 / 形状非法
    {"type": "degraded", "code": "ASSISTANT_NOT_CONFIGURED" |
        "ASSISTANT_UPSTREAM_UNREACHABLE" | "ASSISTANT_UPSTREAM_ERROR", …}
    {"type": "error", …}

AI 的输出契约（系统提示词强制）：回复必须是**一个** JSON 指令对象
（``set_params`` / ``diagnose`` / ``reply`` 三选一）——AI 只产出**指令**，
改参数的权力在用户手里：diff 必须先过参数 Schema 与约束引擎，硬违反打回
（六字段裁定随事件下发），通过后以 ``param_diff`` 结构化块呈现，用户确认
（``apply_diff``）才生效，生效后的建模刷新走前端既有 store 通路。

降级三态（§10.2，R-35：三条任一成立即降级）
--------------------------------------------
① 未配置（无 key）→ ``ASSISTANT_NOT_CONFIGURED``；② 断网 / 超时 →
``ASSISTANT_UPSTREAM_UNREACHABLE``；③ 上游错误 → ``ASSISTANT_UPSTREAM_ERROR``。
前端据 ``degraded`` 事件隐藏 AI 入口；本通道**不持有任何全局状态**（每连接
独立的 history 与 pending diff），任何异常都收敛为事件，绝不拖垮其余端点。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from aeroforge.assistant import upstream
from aeroforge.assistant.config import (
    AssistantConfig,
    last_reachable,
    load_assistant_config,
    mark_reachable,
    save_assistant_config,
)
from aeroforge.assistant.upstream import (
    CODE_UNREACHABLE,
    CODE_UPSTREAM_ERROR,
    DEADLINE_S,
    UpstreamFailure,
)
from aeroforge.params.constraints import check_vehicle
from aeroforge.params.diagnostics import run_diagnostics
from aeroforge.params.report import Diagnostic, has_hard
from aeroforge.params.schema import Vehicle, errors_to_diagnostics
from aeroforge.params.thresholds import load_thresholds
from aeroforge.paths import assistant_config_file as _config_file_path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assistant"])

_NOT_CONFIGURED = "ASSISTANT_NOT_CONFIGURED"

_NOT_CONFIGURED_SUGGESTION = (
    "在「AI 设置」中填写 base_url / api_key / model（平台不自带密钥，OI-10）；"
    "未配置不影响任何手工建模功能"
)

#: 单条 diff 的字段上限（呼应系统提示词；防 AI 一次冲动改掉整套参数）。
_MAX_DIFF_FIELDS = 20

#: 待确认参数 diff 的**进程级**存取表——确认制语义要求 diff 跨 WS 连接存活
#: （前端面板关闭再展开会换一条连接，用户稍后确认的 diff 不能丢）。
#: 单用户桌面应用（ADR-015），进程级即会话级；超上限丢最旧（防无界增长）。
_PENDING_DIFFS: dict[str, _PendingDiff] = {}
_PENDING_DIFFS_MAX = 32

#: 对话历史保留的条目数（6 轮往返；车辆现状每次都随消息重发，历史只需承载语境）。
_HISTORY_MESSAGES = 12


# ---------------------------------------------------------------------------
# REST 契约模型
# ---------------------------------------------------------------------------


class AssistantConfigRequest(BaseModel):
    """``POST /api/assistant/config`` 的请求体（OI-10 三项）。

    ``api_key`` 语义：``null`` = 保留现有值（编辑 base_url / model 不必重填
    密钥）；空串 = 清除（回到未配置）；非空 = 设置新值。**没有**读回明文的
    通路——设置界面显示的是 status 的脱敏形态。
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(
        default="",
        max_length=2048,
        description="OpenAI 兼容服务地址，如 https://api.example.com/v1",
    )
    model: str = Field(default="", max_length=256, description="模型名，如 gpt-4o / deepseek-chat")
    api_key: str | None = Field(
        default=None,
        max_length=4096,
        description="null=保留现有；空串=清除；非空=设置（只写，永不回传）",
    )

    @field_validator("base_url")
    @classmethod
    def _base_url_scheme(cls, value: str) -> str:
        trimmed = value.strip()
        if trimmed and not (trimmed.startswith("http://") or trimmed.startswith("https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return trimmed


class AssistantStatusResponse(BaseModel):
    """``GET /api/assistant/status`` 与 ``POST /api/assistant/config`` 的响应体。

    ⚠ 任何字段都**不携带明文 api_key**（脱敏形态见 ``api_key_masked``）。
    """

    configured: bool = Field(description="是否已启用（三项齐备；未启用 = §10.2 降级①）")
    base_url: str | None = Field(description="当前 base_url（非敏感，供设置界面回显）")
    model: str | None = Field(description="当前模型名（非敏感）")
    api_key_masked: str | None = Field(description="密钥脱敏形态（sk-…abcd）；未设置时为 null")
    reachable: bool | None = Field(
        description="最近一次对话的连通性标记；null = 从未尝试（惰性，不主动探测、不阻塞启动）"
    )
    config_file: str = Field(description="配置文件路径（data/assistant.json，§15）")


def _status_of(config: AssistantConfig) -> AssistantStatusResponse:
    return AssistantStatusResponse(
        configured=config.configured,
        base_url=config.base_url or None,
        model=config.model or None,
        api_key_masked=config.masked_key,
        reachable=last_reachable(),
        config_file=str(_config_file_path()),
    )


@router.get("/api/assistant/status", response_model=AssistantStatusResponse)
def assistant_status() -> AssistantStatusResponse:
    """AI 通道状态（前端据 ``configured=false`` 隐藏入口——§10.2 降级①机检）。"""
    return _status_of(load_assistant_config())


@router.post("/api/assistant/config", response_model=AssistantStatusResponse)
def set_assistant_config(request: AssistantConfigRequest) -> AssistantStatusResponse:
    """写入 AI 通道配置并返回**写入后**的状态。

    日志纪律：只记 base_url / model / 是否已配置——``api_key`` 的明文
    **不允许**出现在任何日志行（结构化排障靠 status 的脱敏形态即可）。
    """
    current = load_assistant_config()
    config = AssistantConfig(
        base_url=request.base_url,
        model=request.model,
        api_key=current.api_key if request.api_key is None else request.api_key,
    )
    save_assistant_config(config)
    logger.info(
        "assistant 配置已更新：base_url=%s model=%s configured=%s",
        config.base_url,
        config.model,
        config.configured,
    )
    return _status_of(config)


# ---------------------------------------------------------------------------
# 字段路径（§6.3 词汇，与前端 store/fieldPath.ts 同构）上的 diff 应用
# ---------------------------------------------------------------------------

_PATH_TOKEN = re.compile(r"([^.[\]]+)|\[(\d+)\]")


class DiffApplyError(ValueError):
    """diff 无法施加（路径不可达 / 形状不符 / 为空）→ ``instruction_invalid``。"""


class DiffRejected(Exception):
    """候选参数未过校验（Schema 或约束引擎）→ ``instruction_rejected``（打回）。"""

    def __init__(self, message: str, *, suggestion: str, diagnostics: list[Diagnostic]) -> None:
        super().__init__(message)
        self.message = message
        self.suggestion = suggestion
        self.diagnostics = diagnostics


#: 路径整体形状：标识符段（`.` 连接）与数组下标（``[n]`` 后缀）交替，别无其他。
_PATH_SHAPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*$")


def parse_field_path(path: str) -> tuple[str | int, ...]:
    """``stages[0].engine.thrust_vacuum_n`` → ``("stages", 0, "engine", …)``。

    与前端 ``store/fieldPath.ts`` 的 ``parseFieldPath`` 同口径：标识符与
    ``[数字]`` 交替。先过整体形状门再拆词——路径里混入其他字符（空格、引号、
    运算符）即拒绝，**绝不**静默截断（截断 = 改错字段）。
    """
    if _PATH_SHAPE.match(path) is None:
        raise DiffApplyError(
            f"字段路径形状非法：{path!r}（应为 stages[0].length_m 一类的标识符 + 数组下标）"
        )
    tokens: list[str | int] = []
    for match in _PATH_TOKEN.finditer(path):
        name, index = match.group(1), match.group(2)
        tokens.append(int(index) if index is not None else str(name))
    if not tokens:
        raise DiffApplyError(f"字段路径为空或无法解析：{path!r}")
    return tuple(tokens)


def _read_path(data: object, tokens: tuple[str | int, ...]) -> object | None:
    cursor: object = data
    for token in tokens:
        if isinstance(cursor, dict) and isinstance(token, str):
            cursor = cursor.get(token)
        elif isinstance(cursor, list) and isinstance(token, int) and 0 <= token < len(cursor):
            cursor = cursor[token]
        else:
            return None
    return cursor


def _write_path(data: object, tokens: tuple[str | int, ...], value: object, path: str) -> None:
    cursor: object = data
    for token in tokens[:-1]:
        if isinstance(cursor, dict) and isinstance(token, str):
            nxt = cursor.get(token)
            if nxt is None:
                raise DiffApplyError(f"字段路径不可达：{path}")
            cursor = nxt
        elif isinstance(cursor, list) and isinstance(token, int) and 0 <= token < len(cursor):
            cursor = cursor[token]
        else:
            raise DiffApplyError(f"字段路径不可达：{path}")
    last = tokens[-1]
    if isinstance(cursor, dict) and isinstance(last, str):
        cursor[last] = value
        return
    if isinstance(cursor, list) and isinstance(last, int) and 0 <= last < len(cursor):
        cursor[last] = value
        return
    raise DiffApplyError(f"字段路径的父节点不是可写容器或下标越界：{path}")


@dataclass(frozen=True, slots=True)
class ParamChange:
    """一个字段的改动（结构化 diff 块的行；前端渲染前后值对照）。"""

    path: str
    old: object
    new: object


def apply_field_diff(
    vehicle: Vehicle, diff: Mapping[str, object]
) -> tuple[Vehicle, tuple[ParamChange, ...]]:
    """把 AI 提议的 ``{字段路径: 新值}`` 施加到当前参数上，产出**候选** Vehicle。

    两道关（禁绕过，任务口径）：
    1. 整体重验 ``Vehicle.model_validate``——路径写入后的 dict 回炉 Schema，
       缺字段 / 越界 / 类型错当场拦截（pydantic 错误翻成六字段裁定）；
    2. ``check_vehicle`` 约束引擎——硬违反一律打回（:class:`DiffRejected`），
       **绝不静默应用**。
    """
    if len(diff) == 0:
        raise DiffApplyError("diff 为空：至少给出一个要修改的字段")
    if len(diff) > _MAX_DIFF_FIELDS:
        raise DiffApplyError(f"diff 一次最多修改 {_MAX_DIFF_FIELDS} 个字段（当前 {len(diff)} 个）")

    data = vehicle.model_dump(mode="json")
    changes: list[ParamChange] = []
    for path, value in diff.items():
        if not isinstance(path, str) or path == "":
            raise DiffApplyError(f"diff 的键必须是字段路径字符串（得到：{path!r}）")
        tokens = parse_field_path(path)
        old = _read_path(data, tokens)
        _write_path(data, tokens, value, path)
        changes.append(ParamChange(path=path, old=old, new=value))

    try:
        candidate = Vehicle.model_validate(data)
    except ValidationError as exc:
        diagnostics = errors_to_diagnostics(exc.errors())
        raise DiffRejected(
            f"AI 提议的参数修改未通过参数 Schema 校验（{len(diagnostics)} 条）",
            suggestion="按 details.diagnostics 的字段级提示修正后重试",
            diagnostics=diagnostics,
        ) from None

    violations = check_vehicle(candidate)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        raise DiffRejected(
            f"AI 提议的参数修改违反 {len(hard)} 条硬约束，已打回（未应用）",
            suggestion=hard[0].suggestion,
            diagnostics=violations,
        )
    return candidate, tuple(changes)


# ---------------------------------------------------------------------------
# AI 输出契约：JSON 指令块的提取与分派
# ---------------------------------------------------------------------------

_VALID_ACTIONS = ("set_params", "diagnose", "reply")

_SYSTEM_RULES = f"""\
你是 AeroForge（航天飞行器参数化建模与性能评估平台）的建模助手。用户会用自然语言\
描述需求，你的职责是把它转成**平台指令**，而不是自由发挥。

输出契约：每条回复必须是**一个** JSON 对象（不要输出 JSON 以外的任何文字），动作三选一：
1. {{"action": "reply", "text": "<面向用户的中文说明>"}}
2. {{"action": "set_params", "diff": {{"<字段路径>": <新值>}}, "explanation": "<中文说明>"}}\
 —— 提议修改参数；diff 必须包含**全部**要改的字段，一次最多 {_MAX_DIFF_FIELDS} 个
3. {{"action": "diagnose", "explanation": "<中文说明>"}} —— 请求平台对当前参数跑本地方案诊断

字段路径口径（与平台诊断 field_path 一致，数组下标从 0 起）：
payload_mass_kg、stages[0].length_m、stages[0].diameter_m、stages[0].engine_count、\
stages[0].engine.thrust_vacuum_n、stages[0].engine.isp_vacuum_s、boosters[0].count、\
mission.orbit_type、mission.altitude_m 等（详见 OpenAPI schema 的 Vehicle 层）。

硬规则：
- 数值必须用 SI 口径：长度 m、质量 kg、推力 N、比冲 s、压力 Pa；
- 诊断结论与 impact 数值只能由平台本地规则集产出——你**不得**编造诊断结果或任何\
  敏感度数值（OI-11）；
- 你提议的 diff 在应用前会经过平台的参数 Schema 与硬约束引擎校验，硬违反会被打回；
- 无法映射为上述三种动作时，一律用 reply。"""


def _vehicle_summary(vehicle: Vehicle) -> dict[str, object]:
    """当前参数的紧凑摘要（进系统提示词；只挑与建模相关的行，不整包塞）。"""
    return {
        "name": vehicle.name,
        "payload_mass_kg": vehicle.payload_mass_kg,
        "stages": [
            {
                "index": stage.index,
                "propellant": stage.propellant,
                "diameter_m": stage.diameter_m,
                "length_m": stage.length_m,
                "engine_count": stage.engine_count,
                "engine_model": stage.engine.model,
                "thrust_vacuum_n": stage.engine.thrust_vacuum_n,
                "isp_vacuum_s": stage.engine.isp_vacuum_s,
            }
            for stage in sorted(vehicle.stages, key=lambda item: item.index)
        ],
        "booster_groups": [booster.count for booster in vehicle.boosters],
        "mission": {
            "orbit_type": vehicle.mission.orbit_type,
            "altitude_m": vehicle.mission.altitude_m,
        },
    }


def _system_prompt(vehicle: Vehicle) -> str:
    summary = json.dumps(_vehicle_summary(vehicle), ensure_ascii=False)
    return f"{_SYSTEM_RULES}\n\n当前飞行器参数摘要（JSON）：{summary}"


def extract_instruction(reply: str) -> dict[str, Any] | None:
    """从 AI 回复里提取 JSON 指令对象；提不出来 → ``None``（按纯文本处理）。

    兼容三种形态：整体就是 JSON；JSON 前后带说明文字（扫首个 ``{`` 到配对
    ``}``，字符串内的花括号不计数）；其余一律 ``None``。
    """
    stripped = reply.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    start = stripped.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(stripped)):
        char = stripped[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    candidate = json.loads(stripped[start : position + 1])
                except json.JSONDecodeError:
                    return None
                return candidate if isinstance(candidate, dict) else None
    return None


# ---------------------------------------------------------------------------
# WS 对话流
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PendingDiff:
    """一个待确认的参数修改（确认制的后端账：确认前不生效、应用/拒绝即销账）。"""

    candidate: Vehicle
    changes: tuple[ParamChange, ...]


def _event(kind: str, **fields: object) -> dict[str, object]:
    return {"type": kind, **fields}


def _degraded_event(code: str, message: str, suggestion: str) -> dict[str, object]:
    return _event("degraded", code=code, message=message, suggestion=suggestion)


def _error_event(code: str, message: str, suggestion: str) -> dict[str, object]:
    return _event("error", code=code, message=message, suggestion=suggestion)


async def _safe_send(websocket: WebSocket, payload: Mapping[str, object]) -> None:
    """发送失败（对端已断开）不外抛——连接清理交给外层的 Disconnect 分支。"""
    try:
        await websocket.send_json(dict(payload))
    except Exception:  # 发送失败只可能是连接层问题（对端已断开），无救可施
        logger.debug("assistant WS 发送失败（对端可能已断开）", exc_info=True)


def _diagnostic_payloads(items: Sequence[Diagnostic]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in items]


async def _send_diagnosis(websocket: WebSocket, vehicle: Vehicle) -> None:
    """本地方案诊断（零 LLM）：复用 §6.3 约束引擎 + §6.5 规则集，结构与
    ``POST /api/params/diagnose`` 同形（前端一套渲染两处用）。"""
    violations = check_vehicle(vehicle)
    if has_hard(violations):
        hard = [item for item in violations if item.level == "hard"]
        await _safe_send(
            websocket,
            _event(
                "diagnosis",
                constraints=_diagnostic_payloads(violations),
                diagnostics=[],
                rules=[],
                note=(
                    f"存在 {len(hard)} 条硬约束违反，逐规则诊断被拒绝"
                    "（与 POST /api/params/diagnose 的 422 同判据）；先修正硬违反再诊断"
                ),
            ),
        )
        return
    report = run_diagnostics(vehicle, thresholds=load_thresholds())
    await _safe_send(
        websocket,
        _event(
            "diagnosis",
            constraints=_diagnostic_payloads(violations),
            diagnostics=_diagnostic_payloads(report.diagnostics),
            rules=[rule.model_dump(mode="json") for rule in report.rules],
            note="诊断由平台本地规则集执行（零 LLM），阈值见 GET /api/params/thresholds",
        ),
    )


async def _dispatch_instruction(websocket: WebSocket, reply: str, vehicle: Vehicle) -> None:
    """把 AI 的 JSON 指令翻成 WS 结构化块。提不出指令 → 纯文本。"""
    instruction = extract_instruction(reply)
    if instruction is None:
        await _safe_send(websocket, _event("assistant_text", text=reply))
        return

    action = instruction.get("action")
    explanation = instruction.get("explanation")
    if isinstance(explanation, str) and explanation.strip():
        await _safe_send(websocket, _event("assistant_text", text=explanation))

    if action == "reply":
        text = instruction.get("text")
        await _safe_send(
            websocket,
            _event(
                "assistant_text",
                text=text if isinstance(text, str) and text.strip() else reply,
            ),
        )
        return

    if action == "diagnose":
        await _send_diagnosis(websocket, vehicle)
        return

    if action == "set_params":
        diff = instruction.get("diff")
        if not isinstance(diff, dict) or len(diff) == 0:
            await _safe_send(
                websocket,
                _event(
                    "instruction_invalid",
                    message="set_params 指令缺少非空的 diff 对象",
                    suggestion=(
                        'diff 应为 {字段路径: 新值} 形状（如 {"stages[0].length_m": 45.0}）；'
                        "已按纯文本忽略本条指令"
                    ),
                ),
            )
            return
        try:
            candidate, changes = apply_field_diff(vehicle, diff)
        except DiffRejected as exc:
            await _safe_send(
                websocket,
                _event(
                    "instruction_rejected",
                    message=exc.message,
                    suggestion=exc.suggestion,
                    diagnostics=_diagnostic_payloads(exc.diagnostics),
                ),
            )
            return
        except DiffApplyError as exc:
            await _safe_send(
                websocket,
                _event(
                    "instruction_invalid",
                    message=str(exc),
                    suggestion=(
                        "字段路径口径与诊断 field_path 一致（如 stages[0].length_m，下标从 0 起）；"
                        "已按纯文本忽略本条指令"
                    ),
                ),
            )
            return
        diff_id = uuid.uuid4().hex
        while len(_PENDING_DIFFS) >= _PENDING_DIFFS_MAX:  # 上限丢最旧（防无界增长）
            _PENDING_DIFFS.pop(next(iter(_PENDING_DIFFS)))
        _PENDING_DIFFS[diff_id] = _PendingDiff(candidate=candidate, changes=changes)
        await _safe_send(
            websocket,
            _event(
                "param_diff",
                diff_id=diff_id,
                changes=[
                    {"path": change.path, "old": change.old, "new": change.new}
                    for change in changes
                ],
                vehicle=candidate.model_dump(mode="json"),
            ),
        )
        return

    await _safe_send(
        websocket,
        _event(
            "instruction_invalid",
            message=f"未知的 action {action!r}（可用：{_VALID_ACTIONS}）",
            suggestion="已按纯文本忽略本条指令；可重新描述需求",
        ),
    )


async def _handle_user_message(
    websocket: WebSocket,
    message: dict[str, Any],
    history: deque[dict[str, str]],
) -> None:
    """一条用户消息 → 上游对话 → 指令分派。三降级在此收口。"""
    text = message.get("text")
    vehicle_payload = message.get("vehicle")
    if not isinstance(text, str) or not text.strip():
        await _safe_send(
            websocket,
            _error_event(
                "REQUEST_INVALID",
                "user_message 缺少非空的 text 字段",
                "在输入框输入内容后发送",
            ),
        )
        return
    if not isinstance(vehicle_payload, dict):
        await _safe_send(
            websocket,
            _error_event(
                "REQUEST_INVALID",
                "user_message 缺少 vehicle 字段（当前参数是 AI 的上下文）",
                "由前端随消息附带当前 Vehicle 参数；若持续出现请检查前端版本",
            ),
        )
        return
    try:
        vehicle = Vehicle.model_validate(vehicle_payload)
    except ValidationError as exc:
        diagnostics = errors_to_diagnostics(exc.errors())
        await _safe_send(
            websocket,
            _event(
                "error",
                code="REQUEST_INVALID",
                message="随消息下发的 vehicle 参数不合法",
                suggestion="刷新页面重载参数后重试；若持续出现请检查前端版本",
                diagnostics=_diagnostic_payloads(diagnostics),
            ),
        )
        return

    config = load_assistant_config()
    if not config.configured:
        # §10.2 降级①：未配置。不给部分服务——通道整体降级。
        await _safe_send(
            websocket,
            _degraded_event(
                _NOT_CONFIGURED,
                "AI 助手未配置（缺少 base_url / api_key / model）",
                _NOT_CONFIGURED_SUGGESTION,
            ),
        )
        return

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _system_prompt(vehicle)},
        *history,
        {"role": "user", "content": text},
    ]
    parts: list[str] = []
    failure: UpstreamFailure | None = None
    try:
        # 整体死线（R-35）：流式回复再长也不能把 WS 拖住；到点按断网语义降级
        async with asyncio.timeout(DEADLINE_S):
            async for delta in upstream.stream_chat(config, messages):
                parts.append(delta)
                await _safe_send(websocket, _event("assistant_delta", text=delta))
    except TimeoutError:
        failure = UpstreamFailure(
            code=CODE_UNREACHABLE,
            message=f"AI 上游在 {DEADLINE_S:.0f} 秒内未完成回复",
            suggestion="检查网络与 base_url；确认后 AI 面板将自动恢复",
        )
    except UpstreamFailure as exc:
        failure = exc
    except Exception:  # AI 通道异常兜底：收敛为事件，不拖垮连接与其余端点
        logger.exception("assistant 对话流未预期异常")
        await _safe_send(
            websocket,
            _error_event(
                "ASSISTANT_INTERNAL",
                "AI 通道内部错误",
                "重试一次；若持续出现请查看后端日志",
            ),
        )
        return

    if failure is not None:
        # 连通性标记：上游**应答过**（HTTP 错误）说明网络通；其余（连接/超时/断流）算断网
        mark_reachable(failure.code == CODE_UPSTREAM_ERROR)
        await _safe_send(
            websocket,
            _degraded_event(failure.code, failure.message, failure.suggestion),
        )
        return

    mark_reachable(True)
    reply = "".join(parts).strip()
    if not reply:
        await _safe_send(
            websocket,
            _error_event(
                "ASSISTANT_EMPTY_REPLY",
                "AI 未返回内容",
                "核对 model 名称是否正确，或重试一次",
            ),
        )
        return

    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    await _dispatch_instruction(websocket, reply, vehicle)


@router.websocket("/ws/assistant")
async def assistant_ws(websocket: WebSocket) -> None:
    """AI 助手对话流（§10.2 可选通道）。连接即发 ``ready``；三降级发 ``degraded``。"""
    await websocket.accept()
    await _safe_send(websocket, _event("ready", configured=load_assistant_config().configured))
    history: deque[dict[str, str]] = deque(maxlen=_HISTORY_MESSAGES)
    try:
        while True:
            try:
                message: Any = await websocket.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:  # 坏 JSON / 二进制帧：报错继续，不断连
                await _safe_send(
                    websocket,
                    _error_event(
                        "REQUEST_INVALID",
                        "消息不是合法的 JSON 对象",
                        "检查客户端消息编码；连接保持",
                    ),
                )
                continue
            if not isinstance(message, dict):
                await _safe_send(
                    websocket,
                    _error_event(
                        "REQUEST_INVALID",
                        "消息必须是 JSON 对象",
                        '形如 {"type": "user_message", …}',
                    ),
                )
                continue
            kind = message.get("type")
            if kind == "user_message":
                await _handle_user_message(websocket, message, history)
            elif kind == "apply_diff":
                diff_id = message.get("diff_id")
                record = _PENDING_DIFFS.pop(diff_id, None) if isinstance(diff_id, str) else None
                if record is None:
                    await _safe_send(
                        websocket,
                        _error_event(
                            "DIFF_NOT_FOUND",
                            "找不到该参数修改（不存在、已应用或已拒绝）",
                            "确认流程：AI 下发 param_diff 后，用同一 diff_id "
                            "发送 apply_diff；或重新发起对话",
                        ),
                    )
                    continue
                # 确认制的唯一生效口：只有 apply_diff 能产出 diff_applied（机检锚点）
                await _safe_send(
                    websocket,
                    _event(
                        "diff_applied",
                        diff_id=diff_id,
                        vehicle=record.candidate.model_dump(mode="json"),
                    ),
                )
            elif kind == "reject_diff":
                diff_id = message.get("diff_id")
                record = _PENDING_DIFFS.pop(diff_id, None) if isinstance(diff_id, str) else None
                if record is None:
                    await _safe_send(
                        websocket,
                        _error_event(
                            "DIFF_NOT_FOUND",
                            "找不到该参数修改（不存在、已应用或已拒绝）",
                            "该 diff 可能已被处理；无需重复操作",
                        ),
                    )
                    continue
                await _safe_send(websocket, _event("diff_rejected", diff_id=diff_id))
            else:
                await _safe_send(
                    websocket,
                    _error_event(
                        "REQUEST_INVALID",
                        f"未知的消息类型 {kind!r}",
                        "可用类型：user_message / apply_diff / reject_diff",
                    ),
                )
    except WebSocketDisconnect:
        return
    except Exception:  # 连接层兜底：本通道的任何异常都不外溢
        logger.exception("assistant WS 连接异常终止")
        # 必须**显式关闭**再返回：吞掉异常后若连接悬着，客户端会永远等不到
        # 下一条事件（测试挂死形态）；关闭帧让对端立即失败而非悬挂。
        with contextlib.suppress(Exception):
            await websocket.close()
        return
