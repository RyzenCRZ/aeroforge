"""数据子系统（规格 §7）：GCAT 快照固化、ETL、标签与查询的领域层落点。

子模块规划：snapshot（快照固化，已落地）· repository · etl · models · materials · lineage。
存储选型为 SQLite（ADR-007）；查询必须走三层标签索引，禁止全表扫描（§7.2）。
"""
