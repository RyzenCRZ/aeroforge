"""AeroForge 桌面启动器（ADR-015，验证门禁见规格 §16.2）。

职责链：探测运行时 → 选空闲端口 → 同进程起后端 → 等就绪 → 开 pywebview 原生窗口。

设计要点：
- **不使用子进程**起后端。同进程内跑 uvicorn 线程，窗口关闭即 `should_exit`，
  进程自然归零（§16.2「退出」契约），不存在孤儿进程占端口的问题。
- **端口不写死**：先 bind 到一个空闲端口再把该 socket 交给 uvicorn，
  避免"先探测端口→再绑定"之间的竞态。

用法：
    uv run python desktop/launcher.py            # 正常启动
    uv run python desktop/launcher.py --probe    # 自检：打印启动耗时后自动关窗退出
    uv run python desktop/launcher.py --preflight # 环境预检（与 tools/preflight.py 同一实现）

关于 ``aeroforge.selfcheck`` 的导入：它**必须**在本模块被导入（规格 §16.2「预检保留」）。
两个作用同一处落点：① 履行 R-30——预检要随交付产物冻结，冻结的前提是它出现在
PyInstaller 的导入图内；② 该模块对 ``build123d`` / ``cea`` 的真实 ``import`` 使
``hook-OCP`` / ``hook-cea`` 得以触发，OCP 与 libcea 原生库才会被收集进包。
⚠ 删掉这一行不会报错，但产物会静默缺失 OCCT / CEA。
"""

from __future__ import annotations

import argparse
import multiprocessing
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# 允许以脚本方式直接运行（python desktop/launcher.py）：把仓库根加入 sys.path，
# 使 `desktop.resource_path` 与 `aeroforge` 均可导入。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import uvicorn  # noqa: E402
import webview  # noqa: E402

from aeroforge import selfcheck  # noqa: E402 - 见模块 docstring：导入即是为打包收集原生库
from aeroforge.api.main import app as fastapi_app  # noqa: E402
from aeroforge.api.static import mount_frontend  # noqa: E402
from desktop.resource_path import ascii_root, frontend_dist, resource_root  # noqa: E402

#: WebView2 运行时在注册表中的固定标识（Microsoft 官方 EdgeUpdate 客户端 GUID）。
_WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

#: 前端探针在 window 上暴露的结果对象（见 frontend/src/r3f/ProbeScene.tsx）。
_PROBE_EXPR = "window.__aeroforgeProbe ?? null"

_WINDOW_TITLE = "AeroForge — 参数化航天器设计与评估平台"


def webview2_version() -> str | None:
    """探测 Microsoft Edge WebView2 运行时版本，缺失返回 ``None``。

    R-33：缺失时必须给出明确指引，禁止静默白屏。WebView2 由 32 位安装器写入
    注册表的 WOW6432Node 视图，而本进程可能是 64 位，故两个视图都要查。
    """
    import winreg

    locations: list[tuple[int, str]] = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"),
    ]
    for hive, path in locations:
        try:
            with winreg.OpenKey(hive, f"{path}\\{_WEBVIEW2_CLIENT_ID}") as key:
                value, _ = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if isinstance(value, str) and value:
            return value
    return None


def bind_free_port() -> tuple[socket.socket, int]:
    """绑定 ``127.0.0.1`` 上的一个空闲端口并返回已绑定的 socket 与其端口号。

    返回 socket 而非仅端口号：直接把已绑定的 socket 交给 uvicorn，
    消除"探测空闲端口 → uvicorn 再绑定"之间的竞态窗口。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock, int(sock.getsockname()[1])


def start_backend(sock: socket.socket) -> tuple[uvicorn.Server, threading.Thread]:
    """在守护线程中启动 uvicorn，复用调用方已绑定的 socket。"""
    config = uvicorn.Config(fastapi_app, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        name="aeroforge-backend",
        daemon=True,
    )
    thread.start()
    return server, thread


def wait_for_health(port: int, timeout_s: float = 30.0) -> float | None:
    """轮询 ``/api/health`` 直到就绪，返回就绪耗时（秒）；超时返回 ``None``。

    走真实 HTTP 而非只查 ``server.started``：这同时验证了 ASGI 栈与路由装配。

    ⚠ 必须显式绕过代理（M7 断网复跑实测缺陷）：``urllib.urlopen`` 默认遵循
    ``HTTP_PROXY`` / ``HTTPS_PROXY`` 环境变量——企业代理或代理黑洞场景下，
    对 ``127.0.0.1`` 的健康检查会被送进代理而永久失败（实测：黑洞代理下
    30 s 超时、exit=4）。本机回环地址永不经过任何代理。
    """
    url = f"http://127.0.0.1:{port}/api/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started_at = time.perf_counter()
    deadline = started_at + timeout_s
    while time.perf_counter() < deadline:
        try:
            with opener.open(url, timeout=1.0) as response:  # 仅本机回环，直连不走代理
                if response.status == 200:
                    return time.perf_counter() - started_at
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.05)
    return None


def _await_probe(window: webview.Window, timeout_s: float = 30.0) -> dict[str, Any] | None:
    """等前端探针把结果写到 ``window.__aeroforgeProbe``。"""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            value = window.evaluate_js(_PROBE_EXPR)
        except Exception:  # 页面尚未完成加载时求值会抛，重试即可
            value = None
        if isinstance(value, dict) and value.get("webgl2") is not None:
            return value
        time.sleep(0.1)
    return None


def _report(timings: dict[str, float], probe: dict[str, Any] | None, webview2: str) -> bool:
    """打印启动报告，返回探针是否全部通过。"""
    print()
    print("AeroForge 桌面启动报告")
    print(f"  WebView2 运行时      {webview2}")
    print(f"  后端就绪             {timings.get('backend', -1.0):.2f} s")
    if "loaded" in timings:
        print(f"  窗口可见（DOM 就绪） {timings['loaded']:.2f} s")
    else:
        print("  窗口可见（DOM 就绪） 超时")

    if probe is None:
        print("  前端探针             超时：未取得 window.__aeroforgeProbe")
        return False

    if "rendered" in timings:
        print(f"  首帧渲染完成         {timings['rendered']:.2f} s")

    webgl2 = probe.get("webgl2")
    renderer = probe.get("renderer", "未知")
    print(f"  WebGL2               {'可用' if webgl2 else '不可用'}")
    print(f"  渲染器               {renderer}")
    if not webgl2:
        print("  ⚠ WebGL2 不可用 —— 3D 视口方案不可行，须先改规格（R-33）")
    return bool(webgl2)


def main(argv: list[str] | None = None) -> int:
    """入口。返回进程退出码。"""
    parser = argparse.ArgumentParser(description="AeroForge 桌面启动器")
    parser.add_argument(
        "--probe",
        action="store_true",
        help="自检模式：打印启动耗时报告后自动关闭窗口（用于 M0.5 门禁与 CI）",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="环境预检（字体 / OCCT / CEA）后退出；与 tools/preflight.py 同一实现（R-30 / §16.2）",
    )
    args = parser.parse_args(argv)

    # 路径编码门禁必须先于一切（含 --preflight）：CEA 原生层不接受非 ASCII 路径，
    # 否则用户看到的是一句"C 层说 thermo.lib 找不到"的误导性报错（§16.2 坑位 4）。
    if not ascii_root():
        print(
            f"错误：安装路径含非 ASCII 字符，CEA 热力学数据库无法加载。\n"
            f"  当前路径：{resource_root()}\n"
            f"  请将 AeroForge 安装或解压到纯英文路径后重试，例如：\n"
            f"    C:\\Program Files\\AeroForge\n"
            f"    D:\\AeroForge\n"
            f"（原因：NASA cea 的 C 扩展用窄字符 API 打开数据表，详见规格 §16.2 坑位 4）",
            file=sys.stderr,
        )
        return 5

    # 预检先于一切：字体损坏会使 import build123d 崩溃，须在触达任何几何代码之前拦下（R-30）。
    if args.preflight:
        return selfcheck.main()

    # 前置自检：缺 WebView2 就明确报错，不进入白屏（R-33 / §16.2 坑位 3）。
    webview2 = webview2_version()
    if webview2 is None:
        print(
            "错误：未检测到 Microsoft Edge WebView2 运行时，无法创建桌面窗口。\n"
            "请安装 Evergreen 运行时后重试：\n"
            "  https://developer.microsoft.com/microsoft-edge/webview2/\n"
            f"（检测位置：HKLM/HKCU 的 EdgeUpdate\\Clients\\{_WEBVIEW2_CLIENT_ID}）",
            file=sys.stderr,
        )
        return 2

    dist = frontend_dist()
    if not dist.is_dir():
        print(
            f"错误：未找到前端构建产物 {dist}\n请先执行：cd frontend && npm run build",
            file=sys.stderr,
        )
        return 3

    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    # 静态托管必须在后端起服务之前完成：兜底路由的注册顺序决定匹配优先级。
    if not mount_frontend(fastapi_app, dist):
        print(f"错误：前端构建产物不完整（缺 index.html）：{dist}", file=sys.stderr)
        return 3

    sock, port = bind_free_port()
    server, thread = start_backend(sock)

    backend_ready = wait_for_health(port)
    if backend_ready is None:
        print("错误：后端在 30 s 内未就绪。", file=sys.stderr)
        server.should_exit = True
        thread.join(timeout=5)
        return 4
    timings["backend"] = time.perf_counter() - t0

    url = f"http://127.0.0.1:{port}/"
    window = webview.create_window(
        _WINDOW_TITLE,
        url,
        width=1440,
        height=900,
        min_size=(1024, 640),
    )

    def on_start() -> None:
        """GUI 就绪后运行（pywebview 在独立线程调用），负责测量与自检收尾。"""
        # create_window 的返回类型是 Window | None（失败时 None）；实际可达性由
        # webview.start() 承载，此处仅按 mypy strict 口径收窄。
        assert window is not None
        if window.events.loaded.wait(30):
            timings["loaded"] = time.perf_counter() - t0
        probe = _await_probe(window)
        if probe is not None:
            rendered = probe.get("rendered_ms")
            if isinstance(rendered, (int, float)):
                timings["rendered"] = timings.get("loaded", 0.0) + float(rendered) / 1000.0
        passed = _report(timings, probe, webview2)
        print(f"  访问地址             {url}")
        if args.probe:
            print(f"  探针结论             {'通过' if passed else '未通过'}")
            window.destroy()

    webview.start(func=on_start)

    # 窗口已关闭：停后端并等线程收尾，确保进程干净退出（§16.2「退出」契约）。
    server.should_exit = True
    thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    # 冻结态多进程子进程的正确入口（M7 实测缺陷修复）：PyInstaller onedir 下
    # ProcessPoolExecutor（MC ×4）以 `AeroForge.exe --multiprocessing-fork …` 拉起子进程，
    # 缺 freeze_support() 时子进程会落进 argparse 并报 unrecognized arguments 退出，
    # 池整体瘫痪；该调用在非冻结 / 非子进程形态下是零开销 no-op。
    multiprocessing.freeze_support()
    raise SystemExit(main())
