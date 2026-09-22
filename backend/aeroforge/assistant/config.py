"""AI 助手配置的读写与脱敏（规格 §10.2 OI-10）。

存储方案（本轮裁决，报告口径）
------------------------------
存**后端侧**文件 ``data/assistant.json``，不存前端 localStorage：

1. **key 不出后端边界**（任务口径）：代理直连 ``base_url`` 必须在后端持有 key；
   若 key 只存前端 localStorage，则每次对话都得把 key 经 WS 再传一遍后端——
   敏感项在两处落地、在链路上多跑一趟，暴露面更大；
2. 桌面单机应用（ADR-015），key 与用户的 ``config.toml`` 同盘同权限，明文落盘
   是**可接受的取舍**（已报告）；文件路径随 :func:`aeroforge.paths.data_root`
   走（测试重定向自动隔离，不污染开发机）；
3. GET /api/assistant/status 只回**脱敏形态**（``sk-…abcd``），POST 响应同——
   key 写进去之后，进程里再没有任何一条路径把它原样吐出来。

线程安全：桌面壳的启动器线程与 uvicorn 事件循环都可能碰它，用一把模块级锁
把"读-改-写"包住。文件损坏按**未配置**处理（记 warning，不抛）——AI 是可选
能力（§10.2：不得因 AI 不可用阻断任何流程），一份写坏的配置文件没有资格
拦住主程序。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from aeroforge.paths import assistant_config_file

logger = logging.getLogger(__name__)

#: 并发保护（进程内单文件，锁即够；无跨进程写方）。
_LOCK = threading.Lock()

#: 最近一次上游连通性标记（``None`` = 从未尝试；HTTP 层报错视为"网络通"）。
_REACHABLE: bool | None = None


@dataclass(frozen=True, slots=True)
class AssistantConfig:
    """用户在设置里填的三项（OI-10）。``api_key`` 为空串 = 未配置。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""

    @property
    def configured(self) -> bool:
        """是否已启用：三项齐备（缺 key 即 §10.2 降级触发①）。"""
        return self.base_url != "" and self.api_key != "" and self.model != ""

    @property
    def masked_key(self) -> str | None:
        """脱敏形态（前端展示用）：只留前 3 与后 4 位；本身太短则整段打码。"""
        if self.api_key == "":
            return None
        if len(self.api_key) <= 8:
            return "********"
        return f"{self.api_key[:3]}…{self.api_key[-4:]}"


_EMPTY = AssistantConfig()


def load_assistant_config() -> AssistantConfig:
    """读配置；文件缺失或损坏一律按**未配置**处理（AI 可选能力，不阻断主流程）。"""
    with _LOCK:
        return _load_locked()


def _load_locked() -> AssistantConfig:
    path = assistant_config_file()
    if not path.is_file():
        return _EMPTY
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("assistant.json 不可读，按未配置处理：%s", type(exc).__name__)
        return _EMPTY
    if not isinstance(raw, dict):
        logger.warning("assistant.json 结构不符（应为对象），按未配置处理")
        return _EMPTY
    base_url = raw.get("base_url")
    api_key = raw.get("api_key")
    model = raw.get("model")
    return AssistantConfig(
        base_url=base_url if isinstance(base_url, str) else "",
        api_key=api_key if isinstance(api_key, str) else "",
        model=model if isinstance(model, str) else "",
    )


def save_assistant_config(config: AssistantConfig) -> Path:
    """原子落盘（tmp + replace）：写一半的密钥文件比没有文件更糟。"""
    path = assistant_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"base_url": config.base_url, "api_key": config.api_key, "model": config.model},
        ensure_ascii=False,
        indent=2,
    )
    with _LOCK:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(f"{payload}\n", encoding="utf-8")
        tmp.replace(path)
    return path


def clear_assistant_config() -> None:
    """清空配置（回到「未配置」；删除文件而非写空串——缺文件就是明确的未配置态）。"""
    global _REACHABLE
    with _LOCK:
        path = assistant_config_file()
        if path.is_file():
            path.unlink()
        _REACHABLE = None


def last_reachable() -> bool | None:
    """最近一次上游连通性标记（``None`` = 从未尝试；不阻塞启动、不主动探测）。"""
    return _REACHABLE


def mark_reachable(value: bool) -> None:
    """对话流结束（成功或失败）后更新连通性标记。"""
    global _REACHABLE
    with _LOCK:
        _REACHABLE = value
