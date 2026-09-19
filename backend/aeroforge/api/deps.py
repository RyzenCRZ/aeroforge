"""API 层的装配依赖：进程内单例（产物仓库 / 作业执行器）与每请求资源（目录库）。

为何用**惰性**单例而不是"只在 lifespan 里构造"：
``fastapi.testclient.TestClient`` 若不以上下文管理器方式使用，lifespan 不会执行；
惰性构造使端点在有/无 lifespan 两种用法下行为一致，测试无需为"是否进了 with"分叉。
lifespan 仍负责绑定事件循环与停机收线（见 :mod:`aeroforge.api.main`）。

目录库（§7.7）**不做**进程级单例：FastAPI 的同步端点跑在线程池里，跨线程共享
一条 sqlite 连接会踩 SQLite 的线程归属检查；目录查询是只读短查询，每请求开 /
关一条连接的代价可忽略，换来「连接的创建、使用、释放恒在同一根线程」。
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from aeroforge.cache.store import ArtifactStore
from aeroforge.data.repository import CatalogRepository
from aeroforge.errors import CatalogNotFoundError
from aeroforge.paths import data_root
from aeroforge.worker.jobs import GeometryJobRunner


@lru_cache(maxsize=1)
def get_store() -> ArtifactStore:
    """产物仓库单例（内容寻址，规格 §9.2）。"""
    return ArtifactStore()


@lru_cache(maxsize=1)
def get_runner() -> GeometryJobRunner:
    """几何作业执行器单例（单 worker 线程，规格 §16.3）。"""
    return GeometryJobRunner(get_store())


def get_catalog() -> Iterator[CatalogRepository]:
    """GCAT 目录库只读仓储（每请求一连接；路径与 ``tools/gcat_db.py`` 默认值一致）。

    库未构建时转 :class:`CatalogNotFoundError`（§10.3：可操作建议，而不是
    一句 500 的「未预期内部错误」——构建目录库是正常的前置步骤）。

    ``DB_NAME`` 走延迟导入：:mod:`aeroforge.data.db` 链上带 pandas，而本模块
    随 API 装配整体加载——不为了一个文件名常量让全部端点的冷启动吃这份重量。
    """
    from aeroforge.data.db import DB_NAME

    try:
        repository = CatalogRepository(data_root() / DB_NAME)
    except FileNotFoundError as exc:
        raise CatalogNotFoundError(str(exc)) from exc
    try:
        yield repository
    finally:
        repository.close()


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
