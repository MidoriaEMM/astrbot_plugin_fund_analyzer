"""
从 AstrMessageEvent 取纯文本及「板块/搜索」类指令的尾部解析。
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_BOARD_DISPLAY_LIMIT = 40
MIN_BOARD_DISPLAY_LIMIT = 1
MAX_BOARD_DISPLAY_LIMIT = 200

# 板块内量化排序：分析上限与 TOP 输出（与「量化精选股票」语义类似，双数字在尾部）
DEFAULT_BOARD_QUANT_MAX_SCAN = 80
DEFAULT_BOARD_QUANT_TOP = 10
MIN_BOARD_QUANT_MAX_SCAN = 1
MAX_BOARD_QUANT_MAX_SCAN = 200
MIN_BOARD_QUANT_TOP = 1
MAX_BOARD_QUANT_TOP = 50

# 量化精选股票 / 股票一日游：剔除北交所、创业板、科创板的关键词（可与数字任意混排）
EXCLUDE_BEIJING_KEYWORDS = frozenset({"去北交所", "去北交"})
EXCLUDE_CHINEXT_KEYWORDS = frozenset({"去创业板", "去创"})
EXCLUDE_STAR_KEYWORDS = frozenset({"去科技", "去科创板", "去科创"})
# 仅「量化精选股票」使用 exclude_limit_up；一日游中写入仅剥离 token
EXCLUDE_LIMIT_UP_KEYWORDS = frozenset({"去涨停", "剔涨停", "剔除涨停"})

DEFAULT_QUANT_STOCK_MAX_SCAN = 150
DEFAULT_QUANT_STOCK_TOP = 10
# 「量化精选股票多空」单指令内最多智能分析只数（防误触超长耗时）
MAX_QUANT_STOCK_DEBATE_CAP = 15

DEFAULT_DAY_TRIP_TOP = 15
MIN_DAY_TRIP_TOP = 1
MAX_DAY_TRIP_TOP = 50

# 「短线选股」默认参数
DEFAULT_SHORT_TERM_MAX_SCAN = 200
DEFAULT_SHORT_TERM_TOP = 10
MIN_SHORT_TERM_MAX_SCAN = 1
MAX_SHORT_TERM_MAX_SCAN = 500
MIN_SHORT_TERM_TOP = 1
MAX_SHORT_TERM_TOP = 50
DEFAULT_SHORT_TERM_MIN_AMOUNT_YI = 1.0
MIN_SHORT_TERM_MIN_AMOUNT_YI = 0.0
MAX_SHORT_TERM_MIN_AMOUNT_YI = 1000.0

# 「短线选股」资金流开关关键词（同义词族）
SHORT_TERM_WITH_FUND_FLOW_KEYWORDS = frozenset({
    "加资金流", "含资金流", "带资金流", "加主力", "含主力", "带主力", "加流向",
})


def get_event_plain_text(event: Any) -> str:
    """尽量兼容不同 AstrBot 版本的事件 API。"""
    gt = getattr(event, "get_plain_text", None)
    if callable(gt):
        try:
            t = gt()
            if t is not None:
                return str(t).strip()
        except TypeError:
            try:
                t = gt(False)
                if t is not None:
                    return str(t).strip()
            except Exception:
                pass
        except Exception:
            pass
    for attr in ("message_str", "plain_text", "text"):
        v = getattr(event, attr, None)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def strip_command_prefix(text: str, command: str) -> str:
    """去掉可选 /、! 前缀及命令名，返回剩余参数段。"""
    s = (text or "").strip()
    for p in ("/", "！", "!"):
        if s.startswith(p):
            s = s[len(p) :].strip()
    if s.startswith(command):
        return s[len(command) :].strip()
    return s


def parse_keyword_and_limit(
    tail: str,
    *,
    default_limit: int = DEFAULT_BOARD_DISPLAY_LIMIT,
) -> tuple[str, int]:
    """
    解析「关键词/名称 … [条数]」：若尾部为纯数字且前面非空，则该数字为 limit；
    若整段为纯数字且无名称语义，返回 ("", limit) 由调用方判无效。
    """
    tail = (tail or "").strip()
    if not tail:
        return "", default_limit
    m = re.match(r"^(.+?)\s+(\d+)\s*$", tail)
    if m:
        name = m.group(1).strip()
        try:
            limit = int(m.group(2))
        except ValueError:
            limit = default_limit
        limit = max(MIN_BOARD_DISPLAY_LIMIT, min(MAX_BOARD_DISPLAY_LIMIT, limit))
        return name, limit
    if tail.isdigit():
        try:
            lim = int(tail)
        except ValueError:
            lim = default_limit
        lim = max(MIN_BOARD_DISPLAY_LIMIT, min(MAX_BOARD_DISPLAY_LIMIT, lim))
        return "", lim
    return tail, max(
        MIN_BOARD_DISPLAY_LIMIT, min(MAX_BOARD_DISPLAY_LIMIT, default_limit)
    )


def parse_name_maxscan_top(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_BOARD_QUANT_MAX_SCAN,
    default_top: int = DEFAULT_BOARD_QUANT_TOP,
) -> tuple[str, int, int]:
    """
    解析「名称 … [分析上限] [输出条数]」：末尾可有 1 或 2 个纯数字 token；
    两个数字时依次为 max_scan、top_n；一个数字时为 max_scan，top_n 用 default_top。
    """
    tail = (tail or "").strip()
    if not tail:
        return "", default_max_scan, default_top
    parts = tail.split()
    if len(parts) >= 2 and parts[-1].isdigit() and parts[-2].isdigit():
        try:
            max_scan = int(parts[-2])
            top_n = int(parts[-1])
        except ValueError:
            max_scan, top_n = default_max_scan, default_top
        name = " ".join(parts[:-2]).strip()
    elif len(parts) >= 1 and parts[-1].isdigit():
        try:
            max_scan = int(parts[-1])
        except ValueError:
            max_scan = default_max_scan
        name = " ".join(parts[:-1]).strip()
        top_n = default_top
    else:
        name = tail
        max_scan = default_max_scan
        top_n = default_top
    max_scan = max(
        MIN_BOARD_QUANT_MAX_SCAN, min(MAX_BOARD_QUANT_MAX_SCAN, max_scan)
    )
    top_n = max(MIN_BOARD_QUANT_TOP, min(MAX_BOARD_QUANT_TOP, top_n))
    return name, max_scan, top_n


def split_exchange_exclude_keyword_tokens(
    tail: str,
) -> tuple[list[str], bool, bool, bool, bool]:
    """分出剔除关键词与非关键词 token（含可选剔除近似涨停、科创板）。"""
    parts = (tail or "").split()
    exclude_bse = False
    exclude_chinext = False
    exclude_limit_up = False
    exclude_star = False
    rest: list[str] = []
    for p in parts:
        if p in EXCLUDE_BEIJING_KEYWORDS:
            exclude_bse = True
        elif p in EXCLUDE_CHINEXT_KEYWORDS:
            exclude_chinext = True
        elif p in EXCLUDE_STAR_KEYWORDS:
            exclude_star = True
        elif p in EXCLUDE_LIMIT_UP_KEYWORDS:
            exclude_limit_up = True
        else:
            rest.append(p)
    return rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star


def parse_quant_stock_screen_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_QUANT_STOCK_MAX_SCAN,
    default_top: int = DEFAULT_QUANT_STOCK_TOP,
) -> tuple[int, int, bool, bool, bool, bool]:
    """
    解析「量化精选股票」尾部：可选 去北交所/去北交、去创业板/去创、去科技/去科创板/去科创、
    去涨停/剔涨停/剔除涨停：前筛剔除涨跌幅>9%（不按板块区分幅度），及 1～2 个正整数。
    无数字时为 default_max_scan / default_top；一个数字视为 max_scan；两个依次为 max_scan、top_n。
    """
    rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star = (
        split_exchange_exclude_keyword_tokens(tail)
    )
    nums: list[int] = []
    for x in rest:
        if x.isdigit():
            try:
                v = int(x)
                if v > 0:
                    nums.append(v)
            except ValueError:
                pass
    if len(nums) == 0:
        max_scan, top_n = default_max_scan, default_top
    elif len(nums) == 1:
        max_scan, top_n = nums[0], default_top
    else:
        max_scan, top_n = nums[0], nums[1]
    return (
        max_scan,
        top_n,
        exclude_bse,
        exclude_chinext,
        exclude_limit_up,
        exclude_star,
    )


def parse_quant_stock_screen_debate_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_QUANT_STOCK_MAX_SCAN,
    default_top: int = DEFAULT_QUANT_STOCK_TOP,
    max_debate_cap: int = MAX_QUANT_STOCK_DEBATE_CAP,
) -> tuple[int, int, int, bool, bool, bool, bool]:
    """
    解析「量化精选股票多空」尾部：与「量化精选股票」相同的剔除关键词；
    正整数可 1～3 个：依次为 max_scan、top_n、智能分析只数上限；
    第三个上限会与 top_n 及 max_debate_cap 取最小值；未写第三个时分析只数为 min(top_n, max_debate_cap)。
    """
    rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star = (
        split_exchange_exclude_keyword_tokens(tail)
    )
    nums: list[int] = []
    for x in rest:
        if x.isdigit():
            try:
                v = int(x)
                if v > 0:
                    nums.append(v)
            except ValueError:
                pass
    if len(nums) == 0:
        max_scan, top_n = default_max_scan, default_top
    elif len(nums) == 1:
        max_scan, top_n = nums[0], default_top
    elif len(nums) == 2:
        max_scan, top_n = nums[0], nums[1]
    else:
        max_scan, top_n = nums[0], nums[1]
        debate_cap = min(nums[2], top_n, max_debate_cap)
        return (
            max_scan,
            top_n,
            debate_cap,
            exclude_bse,
            exclude_chinext,
            exclude_limit_up,
            exclude_star,
        )
    debate_cap = min(top_n, max_debate_cap)
    return (
        max_scan,
        top_n,
        debate_cap,
        exclude_bse,
        exclude_chinext,
        exclude_limit_up,
        exclude_star,
    )


_SHORT_TERM_AMOUNT_RE = re.compile(r"^(?:额)?(\d+(?:\.\d+)?)亿(?:额)?$")


def _parse_short_term_amount_token(tok: str) -> float | None:
    """识别成交额阈值 token，如 '额1亿' / '1.5亿' / '2亿额'，返回亿元数；不匹配返回 None。"""
    m = _SHORT_TERM_AMOUNT_RE.match(tok or "")
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    return max(MIN_SHORT_TERM_MIN_AMOUNT_YI, min(MAX_SHORT_TERM_MIN_AMOUNT_YI, v))


def parse_short_term_screen_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_SHORT_TERM_MAX_SCAN,
    default_top: int = DEFAULT_SHORT_TERM_TOP,
    default_min_amount_yi: float = DEFAULT_SHORT_TERM_MIN_AMOUNT_YI,
) -> tuple[int, int, float, bool, bool, bool, bool, bool]:
    """
    解析「短线选股」尾部参数。

    支持任意顺序的：
    - 关键词剔除：去北交所/去创业板/去科创板/去涨停（与「量化精选股票」一致）
    - 成交额阈值：'额1亿' / '1.5亿' / '2亿额'（默认 1 亿，0 表示不过滤）
    - 资金流开关：'加资金流' / '含资金流' / '加主力' 等同义词
    - 1～2 个正整数：依次为 max_scan、top_n（默认 200/10）

    Returns:
        (max_scan, top_n, min_amount_yi, exclude_bse, exclude_chinext,
         exclude_limit_up, exclude_star, with_fund_flow)
    """
    rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star = (
        split_exchange_exclude_keyword_tokens(tail)
    )

    min_amount_yi = float(default_min_amount_yi)
    with_fund_flow = False
    nums: list[int] = []
    leftover: list[str] = []
    for tok in rest:
        if tok in SHORT_TERM_WITH_FUND_FLOW_KEYWORDS:
            with_fund_flow = True
            continue
        amt = _parse_short_term_amount_token(tok)
        if amt is not None:
            min_amount_yi = amt
            continue
        if tok.isdigit():
            try:
                v = int(tok)
                if v > 0:
                    nums.append(v)
                    continue
            except ValueError:
                pass
        leftover.append(tok)
    _ = leftover  # 当前忽略其他 token

    if len(nums) == 0:
        max_scan, top_n = default_max_scan, default_top
    elif len(nums) == 1:
        max_scan, top_n = nums[0], default_top
    else:
        max_scan, top_n = nums[0], nums[1]

    max_scan = max(MIN_SHORT_TERM_MAX_SCAN, min(MAX_SHORT_TERM_MAX_SCAN, max_scan))
    top_n = max(MIN_SHORT_TERM_TOP, min(MAX_SHORT_TERM_TOP, top_n))
    min_amount_yi = max(
        MIN_SHORT_TERM_MIN_AMOUNT_YI,
        min(MAX_SHORT_TERM_MIN_AMOUNT_YI, float(min_amount_yi)),
    )
    return (
        max_scan,
        top_n,
        min_amount_yi,
        exclude_bse,
        exclude_chinext,
        exclude_limit_up,
        exclude_star,
        with_fund_flow,
    )


def parse_day_trip_tail(
    tail: str,
    *,
    default_top: int = DEFAULT_DAY_TRIP_TOP,
) -> tuple[int, bool, bool, bool]:
    """解析「股票一日游」尾部：同上关键词（去涨停类 token 仅剥离不影响逻辑）；至多解读第一个正整数为展示条数（1～50）。"""
    rest, exclude_bse, exclude_chinext, _, exclude_star = (
        split_exchange_exclude_keyword_tokens(tail)
    )
    nums: list[int] = []
    for x in rest:
        if x.isdigit():
            try:
                v = int(x)
                if v > 0:
                    nums.append(v)
                    break
            except ValueError:
                pass
    top_n = nums[0] if nums else default_top
    top_n = max(MIN_DAY_TRIP_TOP, min(MAX_DAY_TRIP_TOP, top_n))
    return top_n, exclude_bse, exclude_chinext, exclude_star
