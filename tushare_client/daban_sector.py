"""打板选股：从同花顺涨停描述/标签提取概念并聚合板块强度。"""

from __future__ import annotations

import re
from typing import Any

_NOISE = re.compile(
    r"(涨停|连板|首板|二板|三板|四板|五板|六板|七板|八板|N连板|"
    r"一字|T字|炸板|回封|封板|成功|失败|概念|板块|题材|"
    r"昨日|今日|强势|龙头|跟风|补涨|其他|未知|"
    r"ST|退市|新股|次新股|"
    r")\s*",
    flags=re.I,
)


def _clean_segment(seg: str) -> str:
    s = _NOISE.sub("", seg).strip(" +、,，;；|/\\")
    s = re.sub(r"\s+", "", s)
    if len(s) < 2:
        return ""
    if s.isdigit():
        return ""
    return s[:32]


def extract_concept_key(
    *,
    ths_lu_desc: str | None = None,
    ths_tag: str | None = None,
    name: str | None = None,
) -> str:
    """
    从 THS 字段提取主概念键；多段取第一段有效片段。
    无 THS 时用名称兜底（弱）。
    """
    parts: list[str] = []
    for raw in (ths_lu_desc, ths_tag):
        if not raw:
            continue
        text = str(raw).strip()
        for sep in ("+", "；", ";", "，", ",", "|", "/", "\\"):
            text = text.replace(sep, "+")
        for seg in text.split("+"):
            seg = seg.strip()
            if seg:
                parts.append(seg)
    for seg in parts:
        key = _clean_segment(seg)
        if key:
            return key
    if name:
        nm = str(name).strip()
        if nm and not nm.upper().startswith("*ST"):
            return nm[:16]
    return "未分类"


def build_concept_groups(
    rows: list[dict[str, Any]],
    *,
    code_key: str = "ts_code",
    concept_key: str = "concept_key",
) -> dict[str, list[str]]:
    """concept_key -> [ts_code, ...]"""
    groups: dict[str, list[str]] = {}
    for r in rows:
        code = str(r.get(code_key) or "").strip().upper()
        ck = str(r.get(concept_key) or "未分类")
        if code:
            groups.setdefault(ck, []).append(code)
    return groups


def sector_limit_count_for(
    concept_key: str, groups: dict[str, list[str]]
) -> int:
    return len(groups.get(concept_key, []))
