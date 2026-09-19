"""AeroForge — 航天飞行器参数化建模与性能评估平台。

架构与口径的唯一真理源为仓库根目录的 ``AeroForge-Spec.md``；
任何架构级变更必须先修订该文档再写代码（Doc-First，见规格 §15 与附录 D）。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aeroforge")
except PackageNotFoundError:  # 未以发行版安装（例如直接从源码树导入）
    __version__ = "0.0.0.dev0"

#: 几何契约版本：参与内容寻址缓存键（规格 §9.2 / §16.3）。
#:
#: 与 ``__version__``（软件版本）不同，本值只在**几何语义发生变化**时递增——
#: 例如修改段模型的参数含义、改变回转轴约定、调整导出单位。
#: 递增会使全部缓存失效，这是**期望行为**：旧产物对新语义不再有效。
#:
#: ⚠ 必须与 ``AeroForge-Spec.md`` 的「文档版本」一致；由
#: ``backend/tests/unit/test_spec_version.py`` 强制校对，防止文档与代码漂移。
SPEC_VERSION = "0.5.1"

__all__ = ["SPEC_VERSION", "__version__"]
