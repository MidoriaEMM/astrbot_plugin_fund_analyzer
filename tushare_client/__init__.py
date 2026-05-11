"""
Tushare Pro 行情轻量封装（与 ``tickflow_client`` 对齐导出），便于独立拷贝到其他工程。

使用前：``pip install tushare``；部分接口（如 ``rt_k``）需另行开通权限，见
https://tushare.pro/document/1?doc_id=290

Token：各 ``fetch_*`` 首参名称与 Tickflow 相同为 ``api_key``（值为 Tushare token），或为
``None`` 并使用环境变量 ``TUSHARE_TOKEN``；也可以通过
``import tushare as ts; ts.set_token('...')`` 预先写入（本模块仍会再调 ``set_token``）。

注意：`adjust_eastmoney_to_tickflow` 在本包中返回值供 `ts.pro_bar` 的 `adj`
参数使用（``qfq`` / ``hfq`` / ``None``），并非 Tickflow 的
``forward_additive`` 等字面量。
"""

from .breadth import fetch_cn_equity_a_breadth, summarize_cn_equity_a_breadth
from .index_daily_ts import SSE_INDEX_TS_CODE, fetch_sse_index_daily_as_eastmoney_history
from .klines import adjust_eastmoney_to_tickflow, fetch_daily_klines_as_eastmoney_history
from .mapping import quote_to_stock_fields
from .moneyflow_mkt_dc import fetch_moneyflow_mkt_dc_rows
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
    "SSE_INDEX_TS_CODE",
    "fetch_sse_index_daily_as_eastmoney_history",
    "fetch_moneyflow_mkt_dc_rows",
    "summarize_cn_equity_a_breadth",
    "fetch_cn_equity_a_breadth",
]
