"""性能计算子包（M4 计算内核，规格 §8.2–§8.6）。

八块职责，互不替代：

- :mod:`aeroforge.perf.cea_table` —— CEA 预计算表（``data/cea``）的加载与 <1 ms 查询
  （ADR-004）。运行时**只读表、禁止实时调用 CEA**（AGENTS 红线）。
- :mod:`aeroforge.perf.workpoint` —— 最优 O/F 工作点求解：上面级 max Isp、
  一级/助推 max 密度比冲（§8.2 工作点表）。
- :mod:`aeroforge.perf.efficiency` —— 效率因子与 ``Isp_actual``（§8.3）。
- :mod:`aeroforge.perf.mass` —— 质量估算层（§8.4）：几何解析 + GCAT 回归双来源
  与 20% 交叉校验。
- :mod:`aeroforge.perf.solver` —— 多级质量迭代求解器（§8.5）：外层 GLOW 割线 +
  内层自上而下，含 0 级段（OI-36）——纯数值，不碰 OCCT。
- :mod:`aeroforge.perf.losses` —— 弹道损失 L1（§8.6）：重力 / 气动 / 转向 /
  背压四项参数化经验模型 + 自转加成 + 目标轨道 ΔV 需求表（量级锚定）。
- :mod:`aeroforge.perf.capacity` —— 各轨道点值运力（OI-38）：固定火箭的载荷
  二分与 LEO / SSO / GTO / GEO（直送）四目标运力表。
- :mod:`aeroforge.perf.budget` —— ΔV 瀑布组装（OI-23 / §8.8）：理想 ΔV →
  各损失项 → 自转加成 → 总额，闭合容差 1e-6 km/s。

CEA 输出一律是**理想值**（无喷管散流、无边界层、无摩擦），未经 §8.3 效率修正
不得进入质量估算（§8.2 注记）；求解器消费的 Schema 比冲是实际值（已含效率），
故不二次施加 η。
"""

from __future__ import annotations

#: 标准重力加速度 [m/s²]。ADR-014 契约①：CEA 的 ``Isp`` / ``Isp_vacuum`` 字段量纲是
#: **有效排气速度 m/s**，换算为秒必须除以本值——全子包唯一权威常量，禁止就地重写。
G0 = 9.80665

__all__ = ["G0"]
