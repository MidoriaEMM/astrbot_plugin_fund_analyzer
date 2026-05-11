"""
Tickflow 行情轻量封装（API Key 模式），与业务代码解耦，便于复制到其他项目。
"""

from .breadth import fetch_cn_equity_a_breadth, summarize_cn_equity_a_breadth
from .index_klines import SSE_INDEX_SYMBOL, fetch_sse_index_daily_as_eastmoney_history
from .klines import adjust_eastmoney_to_tickflow, fetch_daily_klines_as_eastmoney_history
from .mapping import quote_to_stock_fields
from .quotes import fetch_quote_realtime, fetch_quotes
from .spot_universe import fetch_cn_equity_a_spot_dataframe
from .symbols import normalize_tickflow_symbol

__all__ = [
    "normalize_tickflow_symbol",
    "fetch_quotes",
    "fetch_quote_realtime",
    "quote_to_stock_fields",
    "fetch_cn_equity_a_spot_dataframe",
    "adjust_eastmoney_to_tickflow",
    "fetch_daily_klines_as_eastmoney_history",
    "SSE_INDEX_SYMBOL",
    "fetch_sse_index_daily_as_eastmoney_history",
    "summarize_cn_equity_a_breadth",
    "fetch_cn_equity_a_breadth",
]
