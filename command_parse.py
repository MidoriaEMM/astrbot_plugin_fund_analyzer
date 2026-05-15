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

# 量化精选股票等：剔除/恢复北交所、创业板、科创板的关键词（可与数字任意混排；「去*」优先于「含*」）
EXCLUDE_BEIJING_KEYWORDS = frozenset({"去北交所", "去北交"})
INCLUDE_BEIJING_KEYWORDS = frozenset({"含北交所", "含北交"})
EXCLUDE_CHINEXT_KEYWORDS = frozenset(
    {"去创业板", "去创", "去创业"}
)
INCLUDE_CHINEXT_KEYWORDS = frozenset({"含创业板", "含创", "含创业"})
EXCLUDE_STAR_KEYWORDS = frozenset({"去科技", "去科创板", "去科创"})
INCLUDE_STAR_KEYWORDS = frozenset({"含科技", "含科创板", "含科创"})
# 仅「量化精选股票」使用 exclude_limit_up
EXCLUDE_LIMIT_UP_KEYWORDS = frozenset({"去涨停", "剔涨停", "剔除涨停"})
# 「量化精选股票多空 / 仓位计划」：写上则涨停标的仍跑多智能体辩论；默认不写则涨停跳过 LLM
LIMIT_UP_DEBATE_KEYWORDS = frozenset({"涨停分析"})
# 显式保留 ST；未写时默认剔除 *ST/ST（见 _pairs_for_short_term 等）
INCLUDE_ST_KEYWORDS = frozenset({"含ST", "带ST", "不去ST", "保留ST"})

DEFAULT_QUANT_STOCK_MAX_SCAN = 150
DEFAULT_QUANT_STOCK_TOP = 10
# 「量化精选股票多空」单指令内最多智能分析只数（防误触超长耗时）
MAX_QUANT_STOCK_DEBATE_CAP = 15

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

# 「短线选股 / 短线批量分析」上证盘面水温（与威科夫同源启发式）
SHORT_TERM_WITH_MARKET_KEYWORDS = frozenset({
    "加大盘", "大盘", "上证", "大盘水温",
})
# 「短线选股 / 短线批量分析」威科夫类触发检测（仅形态标签与小加分）
SHORT_TERM_WITH_TRIGGER_KEYWORDS = frozenset({
    "加触发", "威科夫触发",
})

# 「短线批量分析」展示条数默认/范围（与 short_term.SHORT_TERM_BATCH_MAX_CODES 互不冲突）
DEFAULT_SHORT_TERM_BATCH_TOP = 20
MIN_SHORT_TERM_BATCH_TOP = 1
MAX_SHORT_TERM_BATCH_TOP = 50

# 「股票智能分析」可选：报告图、详细阶段进度（与代码/场外标记任意混排）
STOCK_SMART_ANALYSIS_IMAGE_KEYWORDS = frozenset({"发图", "出图", "要图", "图片"})
STOCK_SMART_ANALYSIS_VERBOSE_PROGRESS_KEYWORDS = frozenset(
    {"详进度", "详细进度", "显示进度"}
)
# 「量化精选股票」：默认纯文本；写「发图」等与上表一致才渲染报告图
REPORT_IMAGE_KEYWORDS = STOCK_SMART_ANALYSIS_IMAGE_KEYWORDS


def parse_stock_smart_analysis_tail(tail: str) -> tuple[str, bool, bool]:
    """
    解析「股票智能分析」尾部：分出 发图 / 详进度 关键词，其余拼接为代码段（可含场外、.OF 等）。
    Returns:
        (code_tail, want_image_report, want_verbose_progress)
    """
    parts = (tail or "").split()
    want_image = False
    want_verbose_progress = False
    rest: list[str] = []
    for p in parts:
        if p in REPORT_IMAGE_KEYWORDS:
            want_image = True
        elif p in STOCK_SMART_ANALYSIS_VERBOSE_PROGRESS_KEYWORDS:
            want_verbose_progress = True
        else:
            rest.append(p)
    code_tail = " ".join(rest).strip()
    return code_tail, want_image, want_verbose_progress


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
    *,
    default_exclude_bse: bool = False,
    default_exclude_chinext: bool = False,
    default_exclude_star: bool = False,
) -> tuple[list[str], bool, bool, bool, bool, bool]:
    """分出剔除关键词与非关键词 token；最后一项为 include_st（用户要保留 ST）。"""
    parts = (tail or "").split()
    exclude_bse = default_exclude_bse
    exclude_chinext = default_exclude_chinext
    exclude_star = default_exclude_star
    # 先「含*」再「去*」，保证显式剔除覆盖显式包含
    for p in parts:
        if p in INCLUDE_BEIJING_KEYWORDS:
            exclude_bse = False
        elif p in INCLUDE_CHINEXT_KEYWORDS:
            exclude_chinext = False
        elif p in INCLUDE_STAR_KEYWORDS:
            exclude_star = False
    for p in parts:
        if p in EXCLUDE_BEIJING_KEYWORDS:
            exclude_bse = True
        elif p in EXCLUDE_CHINEXT_KEYWORDS:
            exclude_chinext = True
        elif p in EXCLUDE_STAR_KEYWORDS:
            exclude_star = True
    exclude_limit_up = False
    include_st = False
    rest: list[str] = []
    for p in parts:
        if p in EXCLUDE_BEIJING_KEYWORDS or p in INCLUDE_BEIJING_KEYWORDS:
            continue
        if p in EXCLUDE_CHINEXT_KEYWORDS or p in INCLUDE_CHINEXT_KEYWORDS:
            continue
        if p in EXCLUDE_STAR_KEYWORDS or p in INCLUDE_STAR_KEYWORDS:
            continue
        if p in EXCLUDE_LIMIT_UP_KEYWORDS:
            exclude_limit_up = True
            continue
        if p in INCLUDE_ST_KEYWORDS:
            include_st = True
            continue
        rest.append(p)
    return rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star, include_st


def parse_quant_stock_screen_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_QUANT_STOCK_MAX_SCAN,
    default_top: int = DEFAULT_QUANT_STOCK_TOP,
) -> tuple[int, int, bool, bool, bool, bool, bool, bool]:
    """
    解析「量化精选股票」尾部：默认剔除北交所、创业板、科创板；可用 含北交/含创业板/含科技 等恢复；
    仍可用 去北交、去创业板、去科技 等显式剔除（与同条中「含*」并存时「去*」优先）。
    可选 去涨停/剔涨停/剔除涨停；默认剔除 *ST/ST，写「含ST」「带ST」「不去ST」「保留ST」则保留。
    尾部可写 发图/出图/要图/图片，表示输出 HTML 报告图（默认仅文本）。
    前筛剔除涨跌幅>9%（不按板块区分幅度），及 1～2 个正整数。
    无数字时为 default_max_scan / default_top；一个数字视为 max_scan；两个依次为 max_scan、top_n。
    """
    rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star, include_st = (
        split_exchange_exclude_keyword_tokens(
            tail,
            default_exclude_bse=True,
            default_exclude_chinext=True,
            default_exclude_star=True,
        )
    )
    exclude_st = not include_st
    want_image = False
    rest_wo_img: list[str] = []
    for x in rest:
        if x in REPORT_IMAGE_KEYWORDS:
            want_image = True
        else:
            rest_wo_img.append(x)
    rest = rest_wo_img
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
        exclude_st,
        want_image,
    )


def parse_quant_stock_screen_debate_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_QUANT_STOCK_MAX_SCAN,
    default_top: int = DEFAULT_QUANT_STOCK_TOP,
    max_debate_cap: int = MAX_QUANT_STOCK_DEBATE_CAP,
) -> tuple[int, int, int, bool, bool, bool, bool, bool, bool]:
    """
    解析「量化精选股票多空」尾部：与「量化精选股票」相同的板块/涨停/ST 关键词与默认（默认剔北交所、创业板、科创板）。
    可选写「涨停分析」：涨停标的亦执行多智能体辩论；默认不写则涨停跳过 LLM。
    正整数可 1～3 个：依次为 max_scan、top_n、智能分析只数上限；
    第三个上限会与 top_n 及 max_debate_cap 取最小值；未写第三个时分析只数为 min(top_n, max_debate_cap)。
    """
    raw_parts = (tail or "").split()
    debate_on_limit_up = any(p in LIMIT_UP_DEBATE_KEYWORDS for p in raw_parts)
    filtered = [p for p in raw_parts if p not in LIMIT_UP_DEBATE_KEYWORDS]
    tail_wo_debate_kw = " ".join(filtered)

    rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star, include_st = (
        split_exchange_exclude_keyword_tokens(
            tail_wo_debate_kw,
            default_exclude_bse=True,
            default_exclude_chinext=True,
            default_exclude_star=True,
        )
    )
    exclude_st = not include_st
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
            exclude_st,
            debate_on_limit_up,
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
        exclude_st,
        debate_on_limit_up,
    )


# 「量化精选仓位计划」默认风控参数（本金须在尾部显式指定）
DEFAULT_POSITION_RISK_FRACTION = 0.01
DEFAULT_POSITION_K_ATR = 2.0
DEFAULT_POSITION_MIN_SCORE = 0.7
DEFAULT_POSITION_SINGLE_CAP_FRACTION = 0.2


def _parse_position_principal_token(tok: str) -> float | None:
    """本金：本金100万 / 本金50w / 本金1000000（元）。不匹配返回 None。"""
    if not tok.startswith("本金"):
        return None
    body = tok[len("本金") :]
    m = re.match(r"^(\d+(?:\.\d+)?)(万|[wW])$", body)
    if m:
        return float(m.group(1)) * 10000.0
    m2 = re.match(r"^(\d+(?:\.\d+)?)$", body)
    if m2:
        return float(m2.group(1))
    return None


def _parse_position_risk_token(tok: str) -> float | None:
    """风险：风险1% 或 风险0.01（≤1 视为小数仓位）；不匹配返回 None。"""
    if not tok.startswith("风险"):
        return None
    body = tok[len("风险") :]
    m = re.match(r"^(\d+(?:\.\d+)?)%$", body)
    if m:
        return max(1e-6, min(0.5, float(m.group(1)) / 100.0))
    m2 = re.match(r"^(\d+(?:\.\d+)?)$", body)
    if m2:
        v = float(m2.group(1))
        if v <= 1.0:
            return max(1e-6, min(0.5, v))
        return max(1e-6, min(0.5, v / 100.0))
    return None


def _parse_stop_atr_token(tok: str) -> float | None:
    m = re.match(r"^止损(\d+(?:\.\d+)?)ATR$", tok or "")
    if not m:
        return None
    return max(0.25, min(10.0, float(m.group(1))))


def _parse_min_score_token(tok: str) -> float | None:
    m = re.match(r"^分(\d+(?:\.\d+)?)$", tok or "")
    if not m:
        return None
    return max(0.0, min(1.0, float(m.group(1))))


def _parse_single_cap_token(tok: str) -> float | None:
    m = re.match(r"^单票(\d+(?:\.\d+)?)%$", tok or "")
    if not m:
        return None
    return max(0.01, min(1.0, float(m.group(1)) / 100.0))


def _parse_max_positions_token(tok: str) -> int | None:
    m = re.match(r"^最多(\d+)只$", tok or "")
    if not m:
        m = re.match(r"^最多(\d+)$", tok or "")
    if not m:
        return None
    return max(1, min(50, int(m.group(1))))


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


def _parse_position_avg_amount_yi(tok: str) -> float | None:
    """额均2亿；或与短线一致的 额1亿 / 2亿额。"""
    m = re.match(r"^额均(\d+(?:\.\d+)?)亿$", tok or "")
    if m:
        return max(0.0, min(MAX_SHORT_TERM_MIN_AMOUNT_YI, float(m.group(1))))
    return _parse_short_term_amount_token(tok)


def parse_quant_stock_screen_position_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_QUANT_STOCK_MAX_SCAN,
    default_top: int = DEFAULT_QUANT_STOCK_TOP,
    max_debate_cap: int = MAX_QUANT_STOCK_DEBATE_CAP,
) -> tuple[
    int,
    int,
    int,
    bool,
    bool,
    bool,
    bool,
    bool,
    bool,
    float | None,
    float,
    float,
    float,
    float,
    float,
    int | None,
]:
    """
    解析「量化精选仓位计划」尾部：在「量化精选股票多空」筛参基础上（默认剔除北交所、创业板、科创板），
    增加本金 / 风险 / 止损ATR / 最低分 / 额均 / 单票上限 / 最多只数（均可选除本金外有默认值）。
    「涨停分析」等辩论开关与多空指令相同。
    本金须出现一次：本金100万、本金50w、本金1000000。
    """
    parts = (tail or "").split()
    rest: list[str] = []
    principal: float | None = None
    risk_fraction = DEFAULT_POSITION_RISK_FRACTION
    k_atr = DEFAULT_POSITION_K_ATR
    min_score_01 = DEFAULT_POSITION_MIN_SCORE
    min_avg_amount_yi = 0.0
    single_cap_fraction = DEFAULT_POSITION_SINGLE_CAP_FRACTION
    max_positions: int | None = None

    for p in parts:
        pv = _parse_position_principal_token(p)
        if pv is not None:
            principal = pv
            continue
        rv = _parse_position_risk_token(p)
        if rv is not None:
            risk_fraction = rv
            continue
        kv = _parse_stop_atr_token(p)
        if kv is not None:
            k_atr = kv
            continue
        sv = _parse_min_score_token(p)
        if sv is not None:
            min_score_01 = sv
            continue
        av = _parse_position_avg_amount_yi(p)
        if av is not None:
            min_avg_amount_yi = av
            continue
        cv = _parse_single_cap_token(p)
        if cv is not None:
            single_cap_fraction = cv
            continue
        mv = _parse_max_positions_token(p)
        if mv is not None:
            max_positions = mv
            continue
        rest.append(p)

    debate_rest = " ".join(rest)
    (
        max_scan,
        top_n,
        debate_cap,
        exclude_bse,
        exclude_chinext,
        exclude_limit_up,
        exclude_star,
        exclude_st,
        debate_on_limit_up,
    ) = parse_quant_stock_screen_debate_tail(
        debate_rest,
        default_max_scan=default_max_scan,
        default_top=default_top,
        max_debate_cap=max_debate_cap,
    )
    return (
        max_scan,
        top_n,
        debate_cap,
        exclude_bse,
        exclude_chinext,
        exclude_limit_up,
        exclude_star,
        exclude_st,
        debate_on_limit_up,
        principal,
        risk_fraction,
        k_atr,
        min_score_01,
        min_avg_amount_yi,
        single_cap_fraction,
        max_positions,
    )


def parse_short_term_screen_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_SHORT_TERM_MAX_SCAN,
    default_top: int = DEFAULT_SHORT_TERM_TOP,
    default_min_amount_yi: float = DEFAULT_SHORT_TERM_MIN_AMOUNT_YI,
) -> tuple[int, int, float, bool, bool, bool, bool, bool, bool, bool, bool]:
    """
    解析「短线选股」尾部参数。

    支持任意顺序的：
    - 关键词剔除：与「量化精选股票」相同词表与默认（默认剔除北交所、创业板、科创板；可用 含北交/含创/含科技 等恢复；「去*」优先于「含*」）；另含去涨停；默认剔除 ST，「含ST」等保留
    - 成交额阈值：'额1亿' / '1.5亿' / '2亿额'（默认 1 亿，0 表示不过滤）
    - 资金流开关：'加资金流' / '含资金流' / '加主力' 等同义词
    - 盘面水温：'加大盘' / '大盘' / '上证' / '大盘水温'（拉上证指数并缩放总分）
    - 触发检测：'加触发' / '威科夫触发'（展示触发列并小额加减分）
    - 1～2 个正整数：依次为 max_scan、top_n（默认 200/10）

    Returns:
        (max_scan, top_n, min_amount_yi, exclude_bse, exclude_chinext,
         exclude_limit_up, exclude_star, exclude_st, with_fund_flow,
         with_market_regime, with_wyckoff_trigger)
    """
    rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star, include_st = (
        split_exchange_exclude_keyword_tokens(
            tail,
            default_exclude_bse=True,
            default_exclude_chinext=True,
            default_exclude_star=True,
        )
    )
    exclude_st = not include_st

    min_amount_yi = float(default_min_amount_yi)
    with_fund_flow = False
    with_market_regime = False
    with_wyckoff_trigger = False
    nums: list[int] = []
    leftover: list[str] = []
    for tok in rest:
        if tok in SHORT_TERM_WITH_FUND_FLOW_KEYWORDS:
            with_fund_flow = True
            continue
        if tok in SHORT_TERM_WITH_MARKET_KEYWORDS:
            with_market_regime = True
            continue
        if tok in SHORT_TERM_WITH_TRIGGER_KEYWORDS:
            with_wyckoff_trigger = True
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
        exclude_st,
        with_fund_flow,
        with_market_regime,
        with_wyckoff_trigger,
    )


def parse_wyckoff_screen_tail(
    tail: str,
    *,
    default_max_scan: int = DEFAULT_SHORT_TERM_MAX_SCAN,
    default_top: int = DEFAULT_SHORT_TERM_TOP,
    default_min_amount_yi: float = DEFAULT_SHORT_TERM_MIN_AMOUNT_YI,
) -> tuple[int, int, float, bool, bool, bool, bool, bool]:
    """
    解析「威科夫选股」尾部：与「短线选股」相同（含板块默认剔除与含* 恢复），但不启用资金流关键词（出现则忽略）。
    Returns:
        (max_scan, top_n, min_amount_yi, exclude_bse, exclude_chinext,
         exclude_limit_up, exclude_star, exclude_st)
    """
    rest, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star, include_st = (
        split_exchange_exclude_keyword_tokens(
            tail,
            default_exclude_bse=True,
            default_exclude_chinext=True,
            default_exclude_star=True,
        )
    )
    exclude_st = not include_st

    min_amount_yi = float(default_min_amount_yi)
    nums: list[int] = []
    leftover: list[str] = []
    for tok in rest:
        if tok in SHORT_TERM_WITH_FUND_FLOW_KEYWORDS:
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
    _ = leftover

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
        exclude_st,
    )


def parse_wyckoff_batch_tail(
    tail: str,
    *,
    default_top: int = DEFAULT_SHORT_TERM_BATCH_TOP,
) -> tuple[list[str], int]:
    """解析「威科夫批量分析」：提取代码与展示条数；资金流关键词忽略。"""
    codes, _with_ff, top_show, _wm, _wt = parse_short_term_batch_tail(
        tail, default_top=default_top
    )
    return codes, top_show


def parse_short_term_batch_tail(
    tail: str,
    *,
    default_top: int = DEFAULT_SHORT_TERM_BATCH_TOP,
) -> tuple[list[str], bool, int, bool, bool]:
    """
    解析「短线批量分析」尾部参数。

    规则：
    - 先用正则提取所有 **六位** 股票代码（按出现顺序去重，由调用方做后续校验/截断）。
    - 资金流关键词与「短线选股」共用 ``SHORT_TERM_WITH_FUND_FLOW_KEYWORDS``。
    - ``SHORT_TERM_WITH_MARKET_KEYWORDS`` / ``SHORT_TERM_WITH_TRIGGER_KEYWORDS`` 与短线选股一致。
    - 剩余 token 中的第一个 1～2 位 / 1~50 范围内的正整数 token 视为「展示条数 ``top_show``」，
      默认 ``DEFAULT_SHORT_TERM_BATCH_TOP``。

    Returns:
        ``(codes, with_fund_flow, top_show, with_market_regime, with_wyckoff_trigger)``
    """
    from .stock.short_term import parse_stock_codes_from_text

    codes = parse_stock_codes_from_text(tail or "")

    with_fund_flow = False
    with_market_regime = False
    with_wyckoff_trigger = False
    top_show = default_top
    if tail:
        for tok in tail.split():
            if tok in SHORT_TERM_WITH_FUND_FLOW_KEYWORDS:
                with_fund_flow = True
                continue
            if tok in SHORT_TERM_WITH_MARKET_KEYWORDS:
                with_market_regime = True
                continue
            if tok in SHORT_TERM_WITH_TRIGGER_KEYWORDS:
                with_wyckoff_trigger = True
                continue
            show_tok = tok[1:] if tok.startswith("前") and len(tok) > 1 else tok
            if show_tok.isdigit() and len(show_tok) != 6:
                try:
                    v = int(show_tok)
                except ValueError:
                    continue
                if MIN_SHORT_TERM_BATCH_TOP <= v <= MAX_SHORT_TERM_BATCH_TOP:
                    top_show = v
                    break

    top_show = max(MIN_SHORT_TERM_BATCH_TOP, min(MAX_SHORT_TERM_BATCH_TOP, top_show))
    return (
        codes,
        with_fund_flow,
        top_show,
        with_market_regime,
        with_wyckoff_trigger,
    )


# 「打板选股」：Tushare 涨停池 + 评分引擎（建议 8000 积分档）
DABAN_NO_THS_KEYWORDS = frozenset({"不含同花顺", "无同花顺"})
DABAN_NO_STEP_KEYWORDS = frozenset({"不含天梯", "无天梯"})
DABAN_NO_LHB_KEYWORDS = frozenset({"不含龙虎榜", "无龙虎榜"})
DABAN_MODE_KEYWORDS = frozenset({"综合", "首板", "接力", "龙头"})


def _parse_daban_board_filters(tail: str) -> tuple[list[str], bool, bool, bool, bool]:
    """
    打板命令默认剔除北交所、创业板、科创板、ST（与量化精选一致）。
    可用 含北交/含创/含科技/含ST 恢复；显式 去* 与同条 含* 并存时以 去* 优先。
    """
    rest, exclude_bse, exclude_chinext, _lim, exclude_star, include_st = (
        split_exchange_exclude_keyword_tokens(
            tail,
            default_exclude_bse=True,
            default_exclude_chinext=True,
            default_exclude_star=True,
        )
    )
    return rest, exclude_bse, exclude_chinext, exclude_star, not include_st
_DABAN_MODE_MAP = {
    "综合": "mixed",
    "首板": "shouban",
    "接力": "relay",
    "龙头": "dragon",
}
DEFAULT_DABAN_TOP_N = 15
MIN_DABAN_TOP_N = 1
MAX_DABAN_TOP_N = 200


def parse_daban_pick_tail(
    tail: str,
) -> tuple[str | None, int, str, bool, bool, bool, bool, bool, bool, bool]:
    """
    解析「打板选股」尾部。

    Returns:
        (trade_date, top_n, mode_str, want_ths, want_step, want_top_list,
         exclude_bse, exclude_chinext, exclude_star, exclude_st)
    """
    rest, exclude_bse, exclude_chinext, exclude_star, exclude_st = (
        _parse_daban_board_filters(tail)
    )
    trade_date: str | None = None
    top_n = DEFAULT_DABAN_TOP_N
    mode = "mixed"
    want_ths = True
    want_step = True
    want_top_list = True
    for p in rest:
        if p in DABAN_NO_THS_KEYWORDS:
            want_ths = False
        elif p in DABAN_NO_STEP_KEYWORDS:
            want_step = False
        elif p in DABAN_NO_LHB_KEYWORDS:
            want_top_list = False
        elif p in DABAN_MODE_KEYWORDS:
            mode = _DABAN_MODE_MAP.get(p, mode)
        elif len(p) == 8 and p.isdigit():
            trade_date = p
        elif p.isdigit() and len(p) <= 3:
            try:
                v = int(p)
                if v > 0:
                    top_n = v
            except ValueError:
                pass
    top_n = max(MIN_DABAN_TOP_N, min(MAX_DABAN_TOP_N, top_n))
    return (
        trade_date,
        top_n,
        mode,
        want_ths,
        want_step,
        want_top_list,
        exclude_bse,
        exclude_chinext,
        exclude_star,
        exclude_st,
    )


def parse_daban_money_tail(
    tail: str,
) -> tuple[str | None, int, bool, bool, bool, bool]:
    """兼容旧调用：等同 parse_daban_pick_tail（忽略 mode）。"""
    (
        trade_date,
        top_n,
        _mode,
        want_ths,
        want_step,
        want_top_list,
        exclude_bse,
        _ec,
        _es,
        _exclude_st,
    ) = parse_daban_pick_tail(tail)
    return trade_date, top_n, want_ths, want_step, want_top_list, exclude_bse


def parse_daban_pick_debate_tail(
    tail: str,
    *,
    max_debate_cap: int = MAX_QUANT_STOCK_DEBATE_CAP,
) -> tuple[str | None, int, int, str, bool, bool, bool, bool, bool, bool, bool]:
    """
    解析「打板选股多空」尾部：打板选股参数 + 辩论只数。
    正整数 1～2 个：top_n、辩论只数（未写第二项则 min(top_n, max_debate_cap)）。
    """
    rest, exclude_bse, exclude_chinext, exclude_star, exclude_st = (
        _parse_daban_board_filters(tail)
    )
    trade_date: str | None = None
    mode = "mixed"
    want_ths = True
    want_step = True
    want_top_list = True
    nums: list[int] = []
    for p in rest:
        if p in DABAN_NO_THS_KEYWORDS:
            want_ths = False
        elif p in DABAN_NO_STEP_KEYWORDS:
            want_step = False
        elif p in DABAN_NO_LHB_KEYWORDS:
            want_top_list = False
        elif p in DABAN_MODE_KEYWORDS:
            mode = _DABAN_MODE_MAP.get(p, mode)
        elif len(p) == 8 and p.isdigit():
            trade_date = p
        elif p.isdigit() and len(p) < 8:
            try:
                v = int(p)
                if v > 0:
                    nums.append(v)
            except ValueError:
                pass
    if len(nums) == 0:
        top_n = DEFAULT_DABAN_TOP_N
        debate_cap = min(top_n, max_debate_cap)
    elif len(nums) == 1:
        top_n = nums[0]
        debate_cap = min(top_n, max_debate_cap)
    else:
        top_n = nums[0]
        debate_cap = min(nums[1], nums[0], max_debate_cap)
    top_n = max(MIN_DABAN_TOP_N, min(MAX_DABAN_TOP_N, top_n))
    debate_cap = max(1, min(debate_cap, top_n, max_debate_cap))
    return (
        trade_date,
        top_n,
        debate_cap,
        mode,
        want_ths,
        want_step,
        want_top_list,
        exclude_bse,
        exclude_chinext,
        exclude_star,
        exclude_st,
    )


def parse_date_range_token(token: str) -> tuple[str, str]:
    """
    解析 ``YYYYMMDD-YYYYMMDD`` 日期区间。

    Returns:
        (start_yyyymmdd, end_yyyymmdd)
    """
    s = (token or "").strip()
    if "-" not in s:
        raise ValueError(
            f"日期区间格式错误: {token!r}，请使用 YYYYMMDD-YYYYMMDD，例如 20240101-20240630"
        )
    a, b = s.split("-", 1)
    start = a.strip().replace("-", "")[:8]
    end = b.strip().replace("-", "")[:8]
    if len(start) != 8 or not start.isdigit():
        raise ValueError(f"开始日期无效: {a!r}")
    if len(end) != 8 or not end.isdigit():
        raise ValueError(f"结束日期无效: {b!r}")
    if start > end:
        raise ValueError(f"开始日期不能晚于结束日期: {start} > {end}")
    return start, end


STOCK_BACKTEST_STRATEGY_OFF_KEYWORDS = frozenset(
    {"仅基准", "买入持有", "不含策略"}
)
STOCK_BACKTEST_STRATEGY_ON_KEYWORDS = frozenset({"含策略", "全策略"})


def parse_stock_backtest_tail(tail: str) -> tuple[str, str, str, str]:
    """
    解析「股票回测」尾部。

    用法: 股票回测 <代码> <YYYYMMDD-YYYYMMDD> [仅基准|含策略]

    Returns:
        (code_raw, start_yyyymmdd, end_yyyymmdd, strategy_mode)
        strategy_mode: auto | off | on（仅基准与含策略同写时含策略优先）
    """
    parts = (tail or "").split()
    if len(parts) < 2:
        raise ValueError(
            "用法: 股票回测 <代码> <YYYYMMDD-YYYYMMDD> [仅基准|含策略]\n"
            "示例: 股票回测 600519 20240101-20240630\n"
            "      股票回测 600519 20240101-20240115 仅基准"
        )
    code = parts[0]
    if len(parts) >= 3 and len(parts[1]) == 8 and parts[1].isdigit() and len(parts[2]) == 8:
        raise ValueError(
            "请使用 YYYYMMDD-YYYYMMDD 格式合并日期区间，"
            "例如: 股票回测 600519 20240101-20240630"
        )
    start, end = parse_date_range_token(parts[1])

    want_off = False
    want_on = False
    for p in parts[2:]:
        if p in STOCK_BACKTEST_STRATEGY_ON_KEYWORDS:
            want_on = True
        elif p in STOCK_BACKTEST_STRATEGY_OFF_KEYWORDS:
            want_off = True

    if want_on:
        strategy_mode = "on"
    elif want_off:
        strategy_mode = "off"
    else:
        strategy_mode = "auto"

    return code, start, end, strategy_mode


def ts_code_to_fund_debate_code(ts_code: str) -> str:
    """Tushare ts_code → 六位证券代码，供 get_lof_realtime / 辩论 pipeline。"""
    import re

    s = str(ts_code or "").strip().upper()
    if "." in s:
        s = s.split(".", 1)[0]
    d = re.sub(r"\D", "", s)
    if not d:
        return ""
    if len(d) > 6:
        d = d[-6:]
    return d.zfill(6)


def parse_daban_check_stock_tail(tail: str) -> tuple[str | None, str]:
    """
    解析「打板查股」尾部。

    用法: 打板查股 <代码或ts_code> [YYYYMMDD]
    Returns:
        (trade_date_or_none, code_raw)
    """
    parts = (tail or "").split()
    if not parts:
        return None, ""
    trade_date: str | None = None
    code = parts[0].strip()
    for p in parts[1:]:
        if len(p) == 8 and p.isdigit():
            trade_date = p
    return trade_date, code
