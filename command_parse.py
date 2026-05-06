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

# 量化精选股票 / 股票一日游：剔除北交所、创业板的关键词（可与数字任意混排）
EXCLUDE_BEIJING_KEYWORDS = frozenset({"去北交所", "去北交"})
EXCLUDE_CHINEXT_KEYWORDS = frozenset({"去创业板", "去创"})
# 仅「量化精选股票」使用 exclude_limit_up；一日游中写入仅剥离 token
EXCLUDE_LIMIT_UP_KEYWORDS = frozenset({"去涨停", "剔涨停", "剔除涨停"})

DEFAULT_QUANT_STOCK_MAX_SCAN = 150
DEFAULT_QUANT_STOCK_TOP = 10

DEFAULT_DAY_TRIP_TOP = 15
MIN_DAY_TRIP_TOP = 1
MAX_DAY_TRIP_TOP = 50


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
) -> tuple[list[str], bool, bool, bool]:
    """分出剔除关键词与非关键词 token（含可选剔除近似涨停）。"""
    parts = (tail or "").split()
    exclude_bse = False
    exclude_chinext = False
    exclude_limit_up = False
    rest: list[str] = []
    for p in parts:
        if p in EXCLUDE_BEIJING_KEYWORDS:
            exclude_bse = True
        elif p in EXCLUDE_CHINEXT_KEYWORDS:
            exclude_chinext = True
        elif p in EXCLUDE_LIMIT_UP_KEYWORDS:
            exclude_limit_up = True
        else:
            rest.append(p)
    return rest, exclude_bse, exclude_chinext, exclude_limit_up


def parse_quant_stock_screen_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_QUANT_STOCK_MAX_SCAN,
    default_top: int = DEFAULT_QUANT_STOCK_TOP,
) -> tuple[int, int, bool, bool, bool]:
    """
    解析「量化精选股票」尾部：可选 去北交所/去北交、去创业板/去创、去涨停/剔涨停/剔除涨停，及 1～2 个正整数。
    无数字时为 default_max_scan / default_top；一个数字视为 max_scan；两个依次为 max_scan、top_n。
    """
    rest, exclude_bse, exclude_chinext, exclude_limit_up = (
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
    return max_scan, top_n, exclude_bse, exclude_chinext, exclude_limit_up


def parse_day_trip_tail(
    tail: str,
    *,
    default_top: int = DEFAULT_DAY_TRIP_TOP,
) -> tuple[int, bool, bool]:
    """解析「股票一日游」尾部：同上关键词（去涨停类 token 仅剥离不影响逻辑）；至多解读第一个正整数为展示条数（1～50）。"""
    rest, exclude_bse, exclude_chinext, _ = split_exchange_exclude_keyword_tokens(
        tail
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
    return top_n, exclude_bse, exclude_chinext
