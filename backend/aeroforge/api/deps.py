"""API 层的进程内单例：产物仓库与几何作业执行器（规格 §9.1 / §9.2 / §9.3）。

为何用**惰性**单例而不是"只在 lifespan 里构造"：
``fastapi.testclient.TestClient`` 若不以上下文管理器方式使用，lifespan 不会执行；
惰性构造使端点在有/无 lifespan 两种用法下行为一致，测试无需为"是否进了 with"分叉。
lifespan 仍负责绑定事件循环与停机收线（见 :mod:`aeroforge.api.main`）。
"""

from __future__ import annotations

from functools import lru_cache

from aeroforge.cache.store import ArtifactStore
from aeroforge.worker.jobs import GeometryJobRunner


@lru_cache(maxsize=1)
def get_store() -> ArtifactStore:
    """产物仓库单例（内容寻址，规格 §9.2）。"""
    return ArtifactStore()


@lru_cache(maxsize=1)
def get_runner() -> GeometryJobRunner:
    """几何作业执行器单例（单 worker 线程，规格 §16.3）。"""
    return GeometryJobRunner(get_store())


def reset_singletons() -> None:
    """停掉作业线程并丢弃单例。

    由 lifespan 在**停机**时调用（应用生命周期结束 → 执行器随之结束），
    也供测试在切换产物目录后手动调用。下次 ``get_runner()`` 会重新构造，
    因此"启动 → 停机 → 再启动"（测试里很常见）不会拿到已停止的线程。
    """
    if get_runner.cache_info().currsize:
        get_runner().shutdown()
    get_runner.cache_clear()
    get_store.cache_clear()
