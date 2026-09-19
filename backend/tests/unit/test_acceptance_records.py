"""验收记录留白门禁：每条「（已达成）」记录必须显式声明自己**没**覆盖什么（规格 §13.8）。

由来（教训 H 类：留白不显式）
--------------------------------
M1 验收确实把「双通道朝向对齐未做目视核对」写进了记录——但它藏在表格之后的一串叙述里，
读者扫一眼"八项门禁全绿"就会以为覆盖完整。更早的 M0 / M0.5 记录则把未做项混在行文中
（一处写"未实测"、另一处写"明确不在本探针范围"），**格式各不相同，机器无从检查**。

本文件把格式固定为一段以 ``**本记录明确未覆盖的项**`` 开头的声明：

- 确实没有留白就写「无」——但**不许省略该段**；
- 不许写成"本记录明确未覆盖的一项""尚未覆盖的项"等变体（口径唯一才可机检）。

同时把"扫描不到记录"变成**失败**而非空集通过：若记录标记格式变了，本门禁必须响，
否则它会退化成一个永远通过的空守卫（同 ``test_agents_contract`` 的解析自检口径）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_MD = REPO_ROOT / "AeroForge-Spec.md"

#: 留白段的唯一合法开头（后接「：」或补充说明均可，但必须以这串字符起头）。
SENTINEL = "**本记录明确未覆盖的项**"

#: 记录标题的共同标记：M0 / M0.5 / M1 记录都以此结尾。
_SETTLED = "（已达成）"

#: 已知的验收记录数（M0、M0.5、M1 交付链复验、M1 验收、双通道朝向核对、M2 交付链复验）。
#: 少于它即说明扫描瞎了。
#: ⚠ 新增一条「（已达成）」记录时**必须同步此数**——它正是"扫描器还活着"的判据。
KNOWN_RECORDS = 6

_HEADING = re.compile(r"^#{1,6}\s")
_SENTINEL_VARIANT = re.compile(r"^\*\*本记录[^*\n]*(?:未覆盖|未实测)[^*\n]*\*\*")
_FENCE = re.compile(r"^```")


def _section16() -> list[str]:
    """§16 的正文行——所有验收记录都落在这个区间内。"""
    lines = SPEC_MD.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("## 16."))
    stop = next(
        index for index in range(start + 1, len(lines)) if lines[index].startswith("## 17.")
    )
    return lines[start:stop]


def _unfenced(lines: list[str]) -> list[str]:
    """剔除围栏代码块内容——示例文本不参与格式判定（§13.8 的模板就在围栏里）。"""
    inside = False
    kept: list[str] = []
    for line in lines:
        if _FENCE.match(line.strip()):
            inside = not inside
            continue
        if not inside:
            kept.append(line)
    return kept


def _is_title(line: str) -> bool:
    stripped = line.strip()
    if _SETTLED not in stripped:
        return False
    return stripped.startswith("#") or stripped.startswith("**")


def _records() -> dict[str, list[str]]:
    """记录标题 → 该记录的正文行（到下一个标题或下一条记录标题为止）。"""
    lines = _section16()
    starts = [index for index, line in enumerate(lines) if _is_title(line)]
    records: dict[str, list[str]] = {}
    for position, start in enumerate(starts):
        stop = starts[position + 1] if position + 1 < len(starts) else len(lines)
        for cursor in range(start + 1, stop):
            if _HEADING.match(lines[cursor]):
                stop = cursor
                break
        records[lines[start].strip()] = lines[start:stop]
    return records


def test_scanner_finds_every_known_record() -> None:
    """扫描得到 + 格式未变 = 本门禁的前提。前提不成立时必须失败，而不是空集通过。"""
    found = _records()
    assert len(found) >= KNOWN_RECORDS, (
        f"只在规格 §16 内识别到 {len(found)} 条「{_SETTLED}」记录，少于已知的 {KNOWN_RECORDS} 条。"
        f"要么记录被删，要么标题标记格式变了（本门禁据此扫描，格式一变即瞎）：\n"
        + "\n".join(f"  - {title}" for title in found)
    )


def test_every_record_declares_what_it_did_not_cover() -> None:
    """每条记录都必须有一段以 ``SENTINEL`` 开头的留白声明；无留白就写「无」。"""
    offenders = {
        title: body
        for title, body in _records().items()
        if not any(line.lstrip().startswith(SENTINEL) for line in _unfenced(body))
    }
    assert not offenders, (
        "以下验收记录缺少「未覆盖项」声明（规格 §13.8：不允许用'全绿'掩盖留白；"
        f"确无留白也须写「无」，格式为单独一段以 {SENTINEL}： 开头）：\n"
        + "\n".join(f"  - {title}" for title in offenders)
    )


def test_sentinel_has_no_variants() -> None:
    """留白段的措辞必须唯一——出现变体即报错，避免口径漂移后机检失效。"""
    variants = [
        (title, line.strip())
        for title, body in _records().items()
        for line in _unfenced(body)
        if _SENTINEL_VARIANT.match(line.strip()) and not line.strip().startswith(SENTINEL)
    ]
    assert not variants, f"留白段的措辞出现了变体，请统一为 {SENTINEL}：\n" + "\n".join(
        f"  - 《{title}》→ {line}" for title, line in variants
    )
