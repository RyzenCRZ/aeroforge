"""数据层：仓储 · ETL · ORM 模型 · 材料库 · 溯源。

子模块规划：repository · etl · models · materials · lineage。
存储选型为 SQLite（ADR-007）；查询必须走三层标签索引，禁止全表扫描。
"""
