"""几何层：母线剖面 → 旋转体 / 放样 → BREP / LOD 网格。

子模块规划（规格 §15）：meridian（2D 轮廓）· solid · loft · mesh · smooth · validate · export。

边界约束：
- 本层不得直接读写数据库。
- 对外只暴露 BREP 与网格产物，禁止泄露 OCCT ``TopoDS_Shape`` 内部结构。
- 以米为单位；导出 glTF 必须显式 ``unit=Unit.M``
  （规格 §3.2：默认 ``Unit.MM`` 会导致整体 1000× 缩放，直接触发 R-25 双通道漂移）。
"""
