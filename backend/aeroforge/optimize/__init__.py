"""优化层（规格 §14：多目标优化 · 权衡研究 · 批量扫描 · 逆向设计，M6）。

子模块分工
-----------
- :mod:`aeroforge.optimize.objectives` —— 目标注册与**候选评估本体**（复用内容寻址
  缓存、行级 provenance，§14 约束 1）；
- :mod:`aeroforge.optimize.design_space` —— 变量空间（length 缩放 / 发动机数 /
  助推器数量）与候选合法性校验（分区铺满复核复用 plan_stage）；
- :mod:`aeroforge.optimize.nsga2` —— 自研轻量 NSGA-II（§3.1 选型表：**无 pymoo
  依赖**；Pareto 非支配排序 + 锦标赛 + SBX 交叉 + 多项式变异，Deb 2001 经典参数，
  纯 Python 确定性 seed）；
- :mod:`aeroforge.optimize.trade_study` —— 权衡研究（≤ 20 命名方案对比表）；
- :mod:`aeroforge.optimize.sweep` —— 批量扫描（全因子 ≤ 10⁴，超限报错）；
- :mod:`aeroforge.optimize.inverse` —— 逆向设计（给定运力求最小构型，约束优化）。

执行形态：候选评估是**毫秒级纯数值链**（实测 F9 单候选 ~0.2 ms），四类运行
（NSGA-II 40×20 ≈ 840 次评估 < 0.5 s；10⁴ 全因子 ≈ 2 s）全部低于 §9.1 的作业化
阈值一个量级以上——但按任务口径四端点**仍走异步作业**（进度按代数上报、可取消、
结果经 ``JobRecord.metrics`` 下发），计算在作业执行器的协调线程内顺序进行：
评估链无 GIL 释放点（纯 Python），进程池无收益反增 pickle 与缓存并发成本，
**刻意不引入**（与 MC 的 10⁴ 样本秒级场景不同量级，实测数字随 M6 报告呈交）。
"""

from __future__ import annotations


class OptimizeCancelled(Exception):
    """优化作业被取消的内部信号（代间 / 批间边界生效，无半成品）。"""


__all__ = ["OptimizeCancelled"]
