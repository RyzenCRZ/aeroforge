"""AI 助手通道测试（规格 §10.2 / OI-10 / 任务 M7 交付 3）。

红线：**mock 上游，绝不真连外网**——所有对话流测试都把
``aeroforge.assistant.upstream.stream_chat`` 替换为进程内异步生成器。

覆盖面（对应任务清单）：
- config 存取（``api_key`` **不回传**、不落日志）；
- status 三态（未配置 / 已配置 / 连通性标记）；
- WS 对话流（mock 上游的流式增量 → 结构化块）；
- ``set_params`` 硬违打回（Schema 与约束引擎两层）；
- **确认制**（无 ``apply_diff`` 不产 ``diff_applied``；应用 / 拒绝即销账）；
- 断网快速失败（上游即刻失败 → ``degraded`` 事件及时到达，不悬挂 WS）；
- AI 通道异常不影响其他端点。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aeroforge.api.assistant import (
    DiffApplyError,
    DiffRejected,
    apply_field_diff,
    extract_instruction,
    parse_field_path,
)
from aeroforge.api.main import app
from aeroforge.assistant import upstream as upstream_module
from aeroforge.assistant.config import clear_assistant_config
from aeroforge.assistant.upstream import (
    CODE_UNREACHABLE,
    CODE_UPSTREAM_ERROR,
    UpstreamFailure,
)
from aeroforge.params.schema import Vehicle

#: 测试用密钥（只用于断言"它不出现"，不用于任何真实请求）。
_SECRET_KEY = "sk-test-secret-12345"


@pytest.fixture(autouse=True)
def clean_assistant_state() -> Any:
    """每条测试前后清空 AI 配置（conftest 已把数据根重定向到临时区）。"""
    clear_assistant_config()
    yield
    clear_assistant_config()


@pytest.fixture()
def client() -> Any:
    with TestClient(app) as instance:
        yield instance


def configure(client: TestClient) -> None:
    """把通道配置成"已配置"（base_url 指向不可达的本地地址；上游一律被 mock）。"""
    response = client.post(
        "/api/assistant/config",
        json={
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": _SECRET_KEY,
            "model": "test-model",
        },
    )
    assert response.status_code == 200


def vehicle_payload(vehicle: Vehicle) -> dict[str, Any]:
    return vehicle.model_dump(mode="json")


def user_message(vehicle: Vehicle, text: str) -> dict[str, Any]:
    return {"type": "user_message", "text": text, "vehicle": vehicle_payload(vehicle)}


def mock_upstream(
    monkeypatch: pytest.MonkeyPatch,
    chunks: Sequence[str],
    *,
    error: UpstreamFailure | None = None,
) -> list[list[Mapping[str, str]]]:
    """把上游替换为进程内异步生成器；返回收到的 messages 供断言上下文形状。"""
    seen: list[list[Mapping[str, str]]] = []

    async def fake_stream(config: Any, messages: Sequence[Mapping[str, str]]) -> AsyncIterator[str]:
        seen.append(list(messages))
        for chunk in chunks:
            yield chunk
        if error is not None:
            raise error

    monkeypatch.setattr(upstream_module, "stream_chat", fake_stream)
    return seen


def receive_until(ws: Any, event_type: str, limit: int = 12) -> list[dict[str, Any]]:
    """逐条收 WS 事件直到指定类型（避免按固定条数数错增量数）。"""
    events: list[dict[str, Any]] = []
    for _ in range(limit):
        event = ws.receive_json()
        events.append(event)
        if event["type"] == event_type:
            return events
    raise AssertionError(f"{limit} 条内未收到 {event_type} 事件：{[e['type'] for e in events]}")


# ---------------------------------------------------------------------------
# config 存取与 status 三态
# ---------------------------------------------------------------------------


class TestConfig:
    def test_config_roundtrip_and_key_never_returned(self, client: TestClient) -> None:
        """写配置 → status 回**脱敏**形态；明文 key 不出现在任何响应里。"""
        response = client.post(
            "/api/assistant/config",
            json={"base_url": "http://127.0.0.1:9/v1", "api_key": _SECRET_KEY, "model": "m1"},
        )
        assert response.status_code == 200
        body_text = json.dumps(response.json())
        assert _SECRET_KEY not in body_text  # 明文不回传（任务红线）
        assert response.json()["api_key_masked"] == "sk-…2345"

        status = client.get("/api/assistant/status")
        assert status.status_code == 200
        assert _SECRET_KEY not in json.dumps(status.json())
        payload = status.json()
        assert payload["configured"] is True
        assert payload["base_url"] == "http://127.0.0.1:9/v1"
        assert payload["model"] == "m1"

    def test_config_keep_and_clear_key_semantics(self, client: TestClient) -> None:
        """api_key=null 保留现有值；空串清除（回到未配置）。"""
        first = client.post(
            "/api/assistant/config",
            json={"base_url": "http://a/v1", "api_key": _SECRET_KEY, "model": "m"},
        )
        assert first.json()["configured"] is True

        # 只改 model：key 保留（configured 仍为 True）
        second = client.post(
            "/api/assistant/config",
            json={"base_url": "http://a/v1", "model": "m2"},
        )
        assert second.status_code == 200
        assert second.json()["configured"] is True
        assert second.json()["model"] == "m2"

        # 清空 key → 回到未配置
        third = client.post(
            "/api/assistant/config",
            json={"base_url": "http://a/v1", "api_key": "", "model": "m2"},
        )
        assert third.json()["configured"] is False
        assert third.json()["api_key_masked"] is None

    def test_config_rejects_bad_base_url(self, client: TestClient) -> None:
        """base_url 必须是 http(s)——否则 422（§10.3 结构）。"""
        response = client.post(
            "/api/assistant/config",
            json={"base_url": "ftp://not-http", "api_key": _SECRET_KEY, "model": "m"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "REQUEST_INVALID"

    def test_config_unknown_field_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/assistant/config",
            json={"base_url": "http://a/v1", "model": "m", "hacker_field": 1},
        )
        assert response.status_code == 422

    def test_key_never_logged(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """key 的明文不允许出现在任何日志行（含 DEBUG 级）。"""
        configure(client)
        with caplog.at_level(logging.DEBUG):
            client.get("/api/assistant/status")
            client.post(
                "/api/assistant/config",
                json={"base_url": "http://127.0.0.1:9/v1", "model": "m3"},
            )
        assert _SECRET_KEY not in caplog.text


class TestStatus:
    def test_status_three_states(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """未配置 → 已配置+连通 → 断网标记 False → 上游错误（网络通）标记 True。"""
        # ① 未配置
        fresh = client.get("/api/assistant/status").json()
        assert fresh["configured"] is False
        assert fresh["reachable"] is None  # 从未尝试（惰性，不阻塞启动）

        # ② 已配置 + 对话成功 → reachable True
        configure(client)
        mock_upstream(monkeypatch, ['{"action": "reply", "text": "好的"}'])
        with client.websocket_connect("/ws/assistant") as ws:
            assert ws.receive_json() == {"type": "ready", "configured": True}
            ws.send_json(user_message(single_stage_vehicle, "你好"))
            receive_until(ws, "assistant_text")
        assert client.get("/api/assistant/status").json()["reachable"] is True

        # ③ 对话断网 → reachable False
        mock_upstream(
            monkeypatch,
            [],
            error=UpstreamFailure(code=CODE_UNREACHABLE, message="断网", suggestion="检查网络"),
        )
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(single_stage_vehicle, "在吗"))
            receive_until(ws, "degraded")
        assert client.get("/api/assistant/status").json()["reachable"] is False

        # 补充：上游 HTTP 错误属于"网络通"（应答过）→ reachable True
        mock_upstream(
            monkeypatch,
            [],
            error=UpstreamFailure(
                code=CODE_UPSTREAM_ERROR, message="上游 500", suggestion="稍后重试"
            ),
        )
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(single_stage_vehicle, "在吗"))
            receive_until(ws, "degraded")
        assert client.get("/api/assistant/status").json()["reachable"] is True


# ---------------------------------------------------------------------------
# WS 对话流（mock 上游，绝不真连外网）
# ---------------------------------------------------------------------------


class TestConversation:
    def test_ready_then_degraded_when_unconfigured(
        self, client: TestClient, single_stage_vehicle: Vehicle
    ) -> None:
        """§10.2 降级①：未配置 → ready(configured=false) + 用户消息得明确 degraded 事件。"""
        with client.websocket_connect("/ws/assistant") as ws:
            assert ws.receive_json() == {"type": "ready", "configured": False}
            ws.send_json(user_message(single_stage_vehicle, "造一枚两级火箭"))
            event = ws.receive_json()
            assert event["type"] == "degraded"
            assert event["code"] == "ASSISTANT_NOT_CONFIGURED"
            assert event["suggestion"]

    def test_streaming_text_reply(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """流式增量先到、最终 assistant_text 覆盖整段；上下文含系统提示词。"""
        configure(client)
        seen = mock_upstream(
            monkeypatch,
            ['{"action": ', '"reply", "text": ', '"可以，先改级长"}'],
        )
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(single_stage_vehicle, "帮我看看当前方案"))
            events = receive_until(ws, "assistant_text")
        assert [event["type"] for event in events[:-1]] == [
            "assistant_delta",
            "assistant_delta",
            "assistant_delta",
        ]
        whole = "".join(event["text"] for event in events[:-1])
        # assistant_text 是**解析后的 text 字段**（用户不该看到原始 JSON 指令串）
        assert events[-1]["text"] == "可以，先改级长"
        assert whole == '{"action": "reply", "text": "可以，先改级长"}'
        # 上下文形状：system（含输出契约与参数摘要）+ user
        assert len(seen) == 1
        roles = [message["role"] for message in seen[0]]
        assert roles[0] == "system" and roles[-1] == "user"
        assert "输出契约" in seen[0][0]["content"]
        assert single_stage_vehicle.name in seen[0][0]["content"]

    def test_set_params_downstreams_structured_diff(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """set_params：解释文本 + param_diff 结构化块（path/old/new + 候选 vehicle）。"""
        configure(client)
        mock_upstream(
            monkeypatch,
            [
                json.dumps(
                    {
                        "action": "set_params",
                        "diff": {"payload_mass_kg": 25000.0},
                        "explanation": "把载荷加到 25 t",
                    }
                )
            ],
        )
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(single_stage_vehicle, "载荷改 25 吨"))
            events = receive_until(ws, "param_diff")

        diff_event = events[-1]
        assert events[-2]["type"] == "assistant_text"  # 解释先行
        assert diff_event["diff_id"]
        assert diff_event["changes"] == [
            {"path": "payload_mass_kg", "old": single_stage_vehicle.payload_mass_kg, "new": 25000.0}
        ]
        assert diff_event["vehicle"]["payload_mass_kg"] == 25000.0
        # 候选必须是完整 Vehicle（前端可直接整包写入 store）
        Vehicle.model_validate(diff_event["vehicle"])

    def test_diagnose_runs_local_rules_zero_llm(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """diagnose 动作 → 后端跑本地规则集（与 /api/params/diagnose 同形），零 LLM。"""
        configure(client)
        mock_upstream(
            monkeypatch,
            [json.dumps({"action": "diagnose", "explanation": "我来跑一遍平台诊断"})],
        )
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(single_stage_vehicle, "帮我诊断当前方案"))
            events = receive_until(ws, "diagnosis")

        diagnosis = events[-1]
        assert events[-2]["type"] == "assistant_text"
        assert len(diagnosis["rules"]) == 8  # 与 §6.5 规则条数一致（test_api_params 同口径）
        assert all(isinstance(rule["code"], str) and rule["source"] for rule in diagnosis["rules"])
        assert "本地规则集" in diagnosis["note"]

    def test_unknown_action_reports_invalid(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        configure(client)
        mock_upstream(monkeypatch, [json.dumps({"action": "delete_database"})])
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(single_stage_vehicle, "删库跑路"))
            events = receive_until(ws, "instruction_invalid")
        assert "未知的 action" in events[-1]["message"]

    def test_bad_client_message_gets_error_not_crash(
        self, client: TestClient, single_stage_vehicle: Vehicle
    ) -> None:
        """坏消息（坏 JSON / 缺字段 / 未知类型）→ error 事件，连接保持可用。"""
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_text("{not json")
            assert ws.receive_json()["type"] == "error"

            ws.send_json({"type": "user_message", "text": ""})
            assert ws.receive_json()["type"] == "error"

            ws.send_json({"type": "time_travel"})
            assert ws.receive_json()["type"] == "error"

            # 连接仍然可用：未配置路径的 degraded 照常下发
            ws.send_json(user_message(single_stage_vehicle, "还在吗"))
            assert ws.receive_json()["type"] == "degraded"


# ---------------------------------------------------------------------------
# set_params：硬违打回（Schema 层 + 约束引擎层）
# ---------------------------------------------------------------------------


class TestHardViolation:
    def _send_diff(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        vehicle: Vehicle,
        diff: dict[str, Any],
        expect: str = "instruction_rejected",
    ) -> list[dict[str, Any]]:
        configure(client)
        mock_upstream(
            monkeypatch,
            [json.dumps({"action": "set_params", "diff": diff})],
        )
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(vehicle, "按 AI 的提议改"))
            return receive_until(ws, expect)

    def test_constraint_engine_violation_is_rejected(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """约束引擎硬违（fill_fraction > 1）→ instruction_rejected + 六字段裁定。"""
        events = self._send_diff(
            client, monkeypatch, single_stage_vehicle, {"stages[0].fill_fraction": 1.5}
        )
        assert [event["type"] for event in events] == ["assistant_delta", "instruction_rejected"]
        rejected = events[-1]
        assert "HARD_FILL_OVERFILL" in [item["code"] for item in rejected["diagnostics"]]
        assert rejected["suggestion"]

    def test_schema_violation_is_rejected(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """pydantic 层违规（负质量）→ instruction_rejected + 字段级裁定。"""
        events = self._send_diff(
            client, monkeypatch, single_stage_vehicle, {"payload_mass_kg": -5.0}
        )
        rejected = events[-1]
        assert any(item["field_path"] == "payload_mass_kg" for item in rejected["diagnostics"])

    def test_unreachable_path_is_invalid(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        events = self._send_diff(
            client,
            monkeypatch,
            single_stage_vehicle,
            {"stages[99].length_m": 10.0},
            expect="instruction_invalid",
        )
        assert events[-1]["type"] == "instruction_invalid"


# ---------------------------------------------------------------------------
# 确认制：无 apply_diff 不产 diff_applied
# ---------------------------------------------------------------------------


class TestConfirmationGate:
    def _produce_diff(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, vehicle: Vehicle
    ) -> str:
        configure(client)
        mock_upstream(
            monkeypatch,
            [
                json.dumps(
                    {
                        "action": "set_params",
                        "diff": {"payload_mass_kg": 25000.0},
                        "explanation": "把载荷加到 25 t",
                    }
                )
            ],
        )
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(vehicle, "载荷改 25 吨"))
            events = receive_until(ws, "param_diff")
            return str(events[-1]["diff_id"])

    def test_wrong_id_and_reapply_are_refused(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """确认制的机检锚点：**只有** apply_diff 带未销账的正确 id 才产 diff_applied。"""
        diff_id = self._produce_diff(client, monkeypatch, single_stage_vehicle)

        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            # ① 错误 id → 拒绝（无任何应用事件）
            ws.send_json({"type": "apply_diff", "diff_id": "bogus"})
            error = ws.receive_json()
            assert error["type"] == "error" and error["code"] == "DIFF_NOT_FOUND"

            # ② 正确 id → 应用一次
            ws.send_json({"type": "apply_diff", "diff_id": diff_id})
            applied = ws.receive_json()
            assert applied["type"] == "diff_applied"
            assert applied["vehicle"]["payload_mass_kg"] == 25000.0

            # ③ 已应用的 diff 不可再次应用（销账）
            ws.send_json({"type": "apply_diff", "diff_id": diff_id})
            again = ws.receive_json()
            assert again["type"] == "error" and again["code"] == "DIFF_NOT_FOUND"

    def test_reject_then_apply_rejected(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """用户拒绝即销账：之后同一 id 不可被应用。"""
        diff_id = self._produce_diff(client, monkeypatch, single_stage_vehicle)

        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json({"type": "reject_diff", "diff_id": diff_id})
            assert ws.receive_json() == {"type": "diff_rejected", "diff_id": diff_id}

            ws.send_json({"type": "apply_diff", "diff_id": diff_id})
            error = ws.receive_json()
            assert error["type"] == "error" and error["code"] == "DIFF_NOT_FOUND"


# ---------------------------------------------------------------------------
# 断网快速失败 + 通道隔离
# ---------------------------------------------------------------------------


class TestDegradation:
    def test_network_failure_fails_fast(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """断网 → degraded 事件**及时**到达（不悬挂 WS；R-35）。"""
        mock_upstream(
            monkeypatch,
            [],
            error=UpstreamFailure(
                code=CODE_UNREACHABLE, message="无法连接 AI 上游服务", suggestion="检查网络"
            ),
        )
        configure(client)
        started = time.perf_counter()
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(single_stage_vehicle, "你好"))
            event = ws.receive_json()
        elapsed = time.perf_counter() - started
        assert event["type"] == "degraded"
        assert event["code"] == "ASSISTANT_UPSTREAM_UNREACHABLE"
        assert elapsed < 5.0, f"断网降级耗时 {elapsed:.2f}s——超过了快速失败的口径"

    def test_upstream_error_is_separate_degradation(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """上游错误（③）与断网（②）是**两个**明确的降级码。"""
        mock_upstream(
            monkeypatch,
            [],
            error=UpstreamFailure(
                code=CODE_UPSTREAM_ERROR, message="HTTP 500", suggestion="稍后重试"
            ),
        )
        configure(client)
        with client.websocket_connect("/ws/assistant") as ws:
            ws.receive_json()
            ws.send_json(user_message(single_stage_vehicle, "你好"))
            event = ws.receive_json()
        assert event["type"] == "degraded"
        assert event["code"] == "ASSISTANT_UPSTREAM_ERROR"

    def test_assistant_channel_does_not_affect_other_endpoints(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        single_stage_vehicle: Vehicle,
    ) -> None:
        """AI 通道反复失败，其余端点照常（§10.2：不得因 AI 不可用阻断任何流程）。"""
        mock_upstream(
            monkeypatch,
            [],
            error=UpstreamFailure(code=CODE_UNREACHABLE, message="断网", suggestion="检查网络"),
        )
        configure(client)
        for _ in range(3):
            with client.websocket_connect("/ws/assistant") as ws:
                ws.receive_json()
                ws.send_json(user_message(single_stage_vehicle, "你好"))
                ws.receive_json()

        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/api/params/template").status_code == 200
        diagnose = client.post("/api/params/diagnose", json=vehicle_payload(single_stage_vehicle))
        assert diagnose.status_code == 200


# ---------------------------------------------------------------------------
# 纯函数单元：路径解析 / diff 施加 / JSON 指令提取
# ---------------------------------------------------------------------------


class TestFieldPath:
    def test_parse_accepts_the_shared_vocabulary(self) -> None:
        assert parse_field_path("payload_mass_kg") == ("payload_mass_kg",)
        assert parse_field_path("stages[0].length_m") == ("stages", 0, "length_m")
        assert parse_field_path("stages[1].engine.thrust_vacuum_n") == (
            "stages",
            1,
            "engine",
            "thrust_vacuum_n",
        )
        assert parse_field_path("boosters[0].count") == ("boosters", 0, "count")

    def test_parse_rejects_hostile_shapes(self) -> None:
        for bad in ("", "stages.0.length", "a b", 'x"; drop', "stages[-1].x", "stages[0]]"):
            with pytest.raises(DiffApplyError):
                parse_field_path(bad)


class TestApplyFieldDiff:
    def test_applies_and_returns_changes(self, single_stage_vehicle: Vehicle) -> None:
        candidate, changes = apply_field_diff(
            single_stage_vehicle, {"payload_mass_kg": 25000.0, "stages[0].length_m": 42.0}
        )
        assert candidate.payload_mass_kg == 25000.0
        assert candidate.stages[0].length_m == 42.0
        assert [(change.path, change.old, change.new) for change in changes] == [
            ("payload_mass_kg", 22800.0, 25000.0),
            ("stages[0].length_m", 41.2, 42.0),
        ]
        # 基线不被改动（候选是独立对象）
        assert single_stage_vehicle.payload_mass_kg == 22800.0

    def test_empty_and_oversized_diffs_rejected(self, single_stage_vehicle: Vehicle) -> None:
        with pytest.raises(DiffApplyError, match="diff 为空"):
            apply_field_diff(single_stage_vehicle, {})
        with pytest.raises(DiffApplyError, match="一次最多"):
            apply_field_diff(single_stage_vehicle, {f"stages[0].x{i}": i for i in range(21)})

    def test_hard_violation_raises_rejected_with_diagnostics(
        self, single_stage_vehicle: Vehicle
    ) -> None:
        with pytest.raises(DiffRejected) as excinfo:
            apply_field_diff(single_stage_vehicle, {"stages[0].fill_fraction": 1.5})
        assert any(item.code == "HARD_FILL_OVERFILL" for item in excinfo.value.diagnostics)


class TestExtractInstruction:
    def test_pure_json(self) -> None:
        assert extract_instruction('{"action": "diagnose"}') == {"action": "diagnose"}

    def test_json_with_surrounding_text(self) -> None:
        instruction = extract_instruction(
            '好的，如下：{"action": "reply", "text": "含 } 花括号"} 请查收'
        )
        assert instruction == {"action": "reply", "text": "含 } 花括号"}

    def test_plain_text_returns_none(self) -> None:
        assert extract_instruction("这就是一段普通回答。") is None
        assert extract_instruction("") is None
        assert extract_instruction("{ 不是完整 JSON") is None
