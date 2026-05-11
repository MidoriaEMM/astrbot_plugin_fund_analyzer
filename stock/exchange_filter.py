"""
A 股代码与交易所/板块：北交所、创业板、科创板前缀判断（东财快照无单独市场列时用代码归纳）。
"""

from __future__ import annotations

import re


def normalize_screening_code(raw: str) -> str:
    c = str(raw).strip()
    digits = "".join(ch for ch in c if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:].zfill(6)
    if c:
        return c.zfill(6)
    return ""


def is_chinext_code(code: str) -> bool:
    """
    深市创业板：历史号段 300/301，注册制及扩展后为统一 30xxxx（含 302 等），
    与沪主板(60/68…)、深主板(000/001/002…)互不重叠。
    """
    c = (code or "").zfill(6)
    return len(c) == 6 and c.startswith("30")


def is_star_market_code(code: str) -> bool:
    """科创板常见号段：688、689。"""
    c = (code or "").zfill(6)
    return c.startswith("688") or c.startswith("689")


def is_beijing_exchange_code(code: str) -> bool:
    """北交所常见号段：43/83/87/88 及 920 等（92xxxx）。"""
    c = (code or "").zfill(6)
    return any(c.startswith(p) for p in ("43", "83", "87", "88", "92"))


def is_st_stock_name(name: str | None) -> bool:
    """
    是否 A 股风险警示简称：*ST / ＊ST / S*ST / 以 ST 开头的简写名。
    用于行情快照无独立 ST 标记时的名称识别。
    """
    if name is None:
        return False
    n = str(name).strip()
    if not n:
        return False
    if re.search(r"(?i)S\*ST", n):
        return True
    if re.search(r"(?:\*|＊)\s*ST", n):
        return True
    if re.match(r"(?i)ST[\u4e00-\u9fffA-Za-z0-9]", n):
        return True
    return False
def should_exclude_a_share(
    code: str,
    *,
    exclude_bse: bool,
    exclude_chinext: bool,
    exclude_star: bool = False,
) -> bool:
    """是否按用户选项剔除该行（须在规范化后的 6 位代码上调用，也可传入含数字的原始串）。"""
    c = normalize_screening_code(code) if code else ""
    if not c:
        return False
    if exclude_chinext and is_chinext_code(c):
        return True
    if exclude_bse and is_beijing_exchange_code(c):
        return True
    if exclude_star and is_star_market_code(c):
        return True
    return False


def a_share_price_limit_pct(code: str, name: str) -> float:
    """
    近似涨跌幅上限（%），用于判断是否贴近涨停：ST 5%、北交所 30%、科创/创业 20%、其余 10%。
    """
    n = name or ""
    if re.search(r"ST", n, re.I):
        return 5.0
    c = normalize_screening_code(code) if code else ""
    if not c:
        return 10.0
    if is_beijing_exchange_code(c):
        return 30.0
    if c.startswith("688") or c.startswith("689"):
        return 20.0
    if c.startswith("30"):
        return 20.0
    return 10.0


def is_effectively_limit_up(
    code: str,
    name: str,
    change_rate_pct: float,
    *,
    eps_pct: float = 0.06,
) -> bool:
    """
    是否视为当日涨停（近似）：涨跌幅 >= 该股涨跌停上限(%) - eps_pct。
    eps_pct 用于吸收行情展示四舍五入与浮点误差。
    """
    lim = a_share_price_limit_pct(code, name)
    return float(change_rate_pct) >= lim - eps_pct
