"""桌面启动器就绪轮询的代理绕过回归（M7 断网复跑实测缺陷）。

缺陷史：``wait_for_health`` 原用 ``urllib.request.urlopen`` 轮询 ``127.0.0.1``，
默认遵循 ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量——OI-15 断网复跑（代理黑洞法）
实测：后端其实 0.6 s 就绪，但健康检查被送进黑洞代理，30 s 超时、exit=4。
修复为 ``ProxyHandler({})`` 显式直连（本机回环永不走代理）。

两条测试 = 正向 + **负对照**（§13.8 教训：断言"会失败的东西现在不失败"之前，
先证明"坏掉时测试确实会响"——没有负对照的回归测试可能恒真）。
"""

from __future__ import annotations

import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# desktop 包不在安装集内（wheel 只含 backend/aeroforge），显式补仓库根到 sys.path。
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from desktop.launcher import wait_for_health  # noqa: E402

#: 黑洞代理：端口 9（discard）在本机必然拒绝连接——任何走代理的请求立刻失败。
_BLACKHOLE = "http://127.0.0.1:9"


class _HealthHandler(BaseHTTPRequestHandler):
    """最小 HTTP 服务：任何 GET 都回 200，模拟 /api/health。"""

    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args: object) -> None:  # 静默测试输出
        return


@pytest.fixture()
def _local_http_server() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture()
def _blackhole_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", _BLACKHOLE)
    monkeypatch.setenv("HTTPS_PROXY", _BLACKHOLE)
    monkeypatch.setenv("http_proxy", _BLACKHOLE)
    monkeypatch.setenv("https_proxy", _BLACKHOLE)


def test_wait_for_health_bypasses_proxy_env(
    _local_http_server: int, _blackhole_proxy_env: None
) -> None:
    """黑洞代理环境下就绪轮询仍直达回环地址并成功（修复的行为本体）。"""
    elapsed = wait_for_health(_local_http_server, timeout_s=5.0)
    assert elapsed is not None, "代理环境变量把 127.0.0.1 健康检查送进了代理（缺陷复发）"
    assert elapsed < 5.0


def test_negative_control_plain_urlopen_fails_under_blackhole_proxy(
    _local_http_server: int, _blackhole_proxy_env: None
) -> None:
    """负对照：同一环境下裸 urlopen 必须失败——证明上面的修复测试能响。"""
    url = f"http://127.0.0.1:{_local_http_server}/api/health"
    with pytest.raises((urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        urllib.request.urlopen(url, timeout=2.0).close()
