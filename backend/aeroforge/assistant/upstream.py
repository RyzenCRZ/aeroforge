"""OpenAI 兼容 ``chat/completions``（SSE 流式）的代理客户端（§10.2 OI-10）。

为什么是 stdlib ``urllib`` 而不是 httpx
--------------------------------------
仓库红线「不要引入新依赖（须先走规格 §3.4 审计）」。本模块只做一次
``POST + 逐行读 SSE``，stdlib 足够：

- 阻塞调用整个跑在**工作线程**里（``asyncio`` 禁阻塞，§9.1 同族纪律），
  逐条 delta 经 ``loop.call_soon_threadsafe`` 投回异步侧的队列；
- ``urlopen(timeout=…)`` 的超时**同时**覆盖连接与每一次 socket 读——上游
  半途挂起最多 ``UPSTREAM_TIMEOUT_S`` 必然报错，不存在"既不成功也不失败"
  的无限等待（R-35 同源）；
- 外层再加 ``asyncio.timeout(DEADLINE_S)`` 整体兜底（API 层负责）。

密钥纪律
--------
``api_key`` 只出现在 ``Authorization`` 请求头里，发给**用户自己配置**的
``base_url``。错误信息、日志、异常字符串一律不携带请求头；URL 只含
``base_url``（非敏感，用户自己填的地址）。

失败分类（§10.2 三降级的后两条）
--------------------------------
- :class:`UpstreamFailure` 且 ``code=ASSISTANT_UPSTREAM_UNREACHABLE``：
  连接失败 / DNS / TLS / 读超时 / 半途断流——断网语义；
- ``code=ASSISTANT_UPSTREAM_ERROR``：上游**应答了**但状态码非 2xx——
  服务端错误语义（此时网络是通的，连通性标记记 True）。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass

from aeroforge.assistant.config import AssistantConfig

logger = logging.getLogger(__name__)

#: 连接与单次 socket 读的超时（秒）。实测本机握手 <10 ms，公网 5–10 s；超过即按
#: 断网处理——快速失败优先于成功率（§10.2：断网时对话请求要快，不悬挂 WS）。
UPSTREAM_TIMEOUT_S = 30.0

#: 单次对话的**整体**死线（秒）：流式回复再长也不能把 WS 拖住（R-35）。
DEADLINE_S = 120.0

#: 失败码（§10.2 三降级的②③；API 层据此发 ``degraded`` 事件）。
CODE_UNREACHABLE = "ASSISTANT_UPSTREAM_UNREACHABLE"
CODE_UPSTREAM_ERROR = "ASSISTANT_UPSTREAM_ERROR"

_UNREACHABLE_SUGGESTION = (
    "检查网络与 base_url 是否可达（可先在浏览器打开该地址）；确认后 AI 面板将自动恢复"
)
_UPSTREAM_ERROR_SUGGESTION = (
    "上游服务已应答但返回错误：核对 api_key / model 拼写与账户额度，或稍后重试"
)


@dataclass(frozen=True, slots=True)
class UpstreamFailure(Exception):
    """上游调用失败（§10.2 三降级的②③两种语义，由 ``code`` 区分）。

    ``message`` / ``suggestion`` 面向用户，直接进 WS 的 ``degraded`` 事件；
    **绝不**包含请求头或 key。
    """

    code: str
    message: str
    suggestion: str

    def __str__(self) -> str:
        return self.message


class _ResponseHolder:
    """持有当前 ``HTTPResponse``，供异步侧在取消 / 超时时强制关闭阻塞中的读。"""

    def __init__(self) -> None:
        self._response: http.client.HTTPResponse | None = None
        self._lock = threading.Lock()

    def set(self, response: http.client.HTTPResponse) -> None:
        with self._lock:
            self._response = response

    def close(self) -> None:
        with self._lock:
            if self._response is not None:
                # 关闭底层 socket 会让阻塞中的 recv 立刻抛 OSError，泵线程随之收尾
                self._response.close()
                self._response = None


def chat_completions_url(base_url: str) -> str:
    """由用户填的 ``base_url`` 推出 ``chat/completions`` 端点。

    兼容两种填法：``https://host/v1``（自动补路径）与已写到
    ``https://host/v1/chat/completions`` 的完整端点（原样使用，不重复拼接）。
    """
    trimmed = base_url.strip().rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    return f"{trimmed}/chat/completions"


def _extract_delta(payload: dict[str, object]) -> str:
    """SSE chunk → 增量文本；形状不符的 chunk 按空处理（不中断流）。"""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        return content if isinstance(content, str) else ""
    # 上游忽略 stream=True 时的非流式形状（choices[0].message.content）
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        return content if isinstance(content, str) else ""
    return ""


def _iter_sse_sync(
    config: AssistantConfig,
    messages: Sequence[Mapping[str, str]],
    *,
    timeout_s: float,
    holder: _ResponseHolder,
) -> Iterator[str]:
    """同步泵：发请求、逐行解析 SSE、yield 增量文本。只在工作线程里跑。"""
    url = chat_completions_url(config.base_url)
    body = json.dumps({"model": config.model, "messages": list(messages), "stream": True})
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {config.api_key}",
            "User-Agent": "AeroForge-Assistant/1.0",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        # 上游应答了 → 网络通；错误体只取前 4 KB 排障（其中不应有 key，上游也不会回显它）
        detail = exc.read(4096).decode("utf-8", "replace").strip()
        logger.info("assistant 上游返回 HTTP %s（base_url=%s）", exc.code, config.base_url)
        raise UpstreamFailure(
            code=CODE_UPSTREAM_ERROR,
            message=f"AI 上游服务返回 HTTP {exc.code}" + (f"：{detail[:200]}" if detail else ""),
            suggestion=_UPSTREAM_ERROR_SUGGESTION,
        ) from None
    except OSError as exc:
        # URLError / SSLError / socket.timeout 均属 OSError 族：断网、TLS、读超时一网打尽
        logger.info("assistant 上游不可达（%s：%s）", type(exc).__name__, exc)
        raise UpstreamFailure(
            code=CODE_UNREACHABLE,
            message=f"无法连接 AI 上游服务（{type(exc).__name__}）",
            suggestion=_UNREACHABLE_SUGGESTION,
        ) from None

    # 类型口径：urlopen 对 http/https 恒返 HTTPResponse（typeshed 的重载亦如此）
    assert isinstance(response, http.client.HTTPResponse)
    holder.set(response)
    saw_data_line = False
    raw_lines: list[str] = []
    try:
        for raw in response:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("data:"):
                saw_data_line = True
            raw_lines.append(line)
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue  # 心跳 / 注释行 / 半截行：跳过不中断
            if isinstance(payload, dict):
                delta = _extract_delta(payload)
                if delta:
                    yield delta
    except OSError as exc:
        raise UpstreamFailure(
            code=CODE_UNREACHABLE,
            message=f"AI 上游连接中断（{type(exc).__name__}）",
            suggestion=_UNREACHABLE_SUGGESTION,
        ) from None
    finally:
        response.close()

    # 上游无视 stream=True 返回了普通 JSON：整体解析为一次非流式补全
    if not saw_data_line:
        text = "\n".join(raw_lines).strip()
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return
            if isinstance(payload, dict):
                delta = _extract_delta(payload)
                if delta:
                    yield delta


async def stream_chat(
    config: AssistantConfig, messages: Sequence[Mapping[str, str]]
) -> AsyncIterator[str]:
    """异步侧接口：增量文本流。失败以 :class:`UpstreamFailure` 抛出。

    线程桥：泵线程逐条 ``call_soon_threadsafe`` 投队列；生成器被关闭（客户端
    断开 / 整体超时）时关闭响应，阻塞中的 socket 读随即报错收线——不留悬挂线程。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str | BaseException | None] = asyncio.Queue()
    holder = _ResponseHolder()

    def pump() -> None:
        try:
            for delta in _iter_sse_sync(
                config, messages, timeout_s=UPSTREAM_TIMEOUT_S, holder=holder
            ):
                loop.call_soon_threadsafe(queue.put_nowait, delta)
        except BaseException as exc:
            loop.call_soon_threadsafe(queue.put_nowait, exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    thread = threading.Thread(target=pump, name="aeroforge-assistant-upstream", daemon=True)
    thread.start()
    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        holder.close()
