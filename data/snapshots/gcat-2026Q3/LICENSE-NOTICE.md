# LICENSE-NOTICE — GCAT（gcat-2026Q3）

> 本文件是规格 §7.1「许可先于抓取」门禁的落点（§17 R-10）：
> **结论写入时间先于任何 GCAT 数据的下载与固化**（2026-09-19，本目录此时尚无
> `records.parquet` / `snapshot.json` / `field_report.md`——它们随实际抓取产生，
> 不得在本结论之前出现）。

## 核实结论：✅ 通过 —— CC-BY（署名即可使用）

- **核实对象**：GCAT（General Catalog of Artificial Space Objects），Jonathan C. McDowell，
  `https://planet4589.org/space/gcat`——即规格 §7.3 字段映射（`lv_name` / `lv_family` /
  `lv_variant` / `lv_manufacturer` …）与 §7.1 条数（vehicles / engines / stages）所指的目录。
- **许可原文链接**：<https://planet4589.org/space/gcat/>（「Citing GCAT」一节，2026-09-19 在线核实）
- **原文摘录**（逐字，未改写）：
  > GCAT is released under the Creative Commons CC-BY licence. You are free to use the data
  > in GCAT as long as you include a citation to it. A suitable short citation is
  > 'data from GCAT (J. McDowell, planet4589.org/space/gcat)'. A full citation is
  > ```
  > McDowell, Jonathan C., 2020. General Catalog of Artificial Space Objects,
  >  Release 1.8.7 , https://planet4589.org/space/gcat
  > ```
- **版本锚点**：核实当日官网首页标注 **GCAT Release 1.8.7（2026 Aug 25）｜Data Update 2026 Aug 30**。
  快照抓取时必须在 `snapshot.json` 里记录当时实际 release 号与各 TSV 的 sha256；若 release
  演进导致字段变化，以**快照当时**的 release 为准（快照不可变）。
- **原文未标注 CC-BY 的具体版本号**（如 4.0），义务按原文表述执行：**署名（citation）**。
  本项目按上面 full citation 格式署名即可满足。

## 与本项目红线的关系（§1.4）

| 红线 | 判定 |
|---|---|
| 2. 禁 GPL / AGPL / SSPL | ✅ 不触碰。CC-BY 是**数据**许可，非代码 copyleft；允许再分发，唯一条件是署名 |
| 4. 数值必须可溯源 | ✅ 同向。CC-BY 的强制署名义务恰好落实为「每份快照记录来源 URL + 引用文本」；下游所有取自 GCAT 的数字都经快照 id 溯源 |

## 随数据使用持续生效的义务

1. **应用内署名**：M3 交付的「数据面板 / 关于」处必须出现上面 full citation 文本（文档 `docs/` 与
   用户可见界面各一处；M7 安装包的第三方声明清单里同样列出）。
2. **快照溯源**：`snapshot.json` 必含 `{source_url, fetched_at, sha256, record_count}`（§7.1 快照规范），
   `sha256` 参与缓存键（§9.2）。
3. **镜像不采信**：本结论只覆盖 `planet4589.org` 官方源。任何第三方镜像（R-10 提到的"镜像许可不明"）
   **不在本结论覆盖范围内**，抓取一律走官方源。

## 本核实的边界（未覆盖项）

- 仅核实了 **GCAT**。§7.1 表中的 **SPACEMATDB 等材料库**（"需核许可与试验条件"）**尚未核实**，
  在其结论落档前不得抓取——同一条门禁适用于它。
- "CC-BY 具体版本号未标注"这一点**不构成查不清**：原文的义务表述（署名）完整、明确且可直接执行，
  故不触发 §7.1 第 3 条（转自建数据集）。
