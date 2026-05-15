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
from .daban_pick_engine import (
    DabanPickConfig,
    DabanPickMode,
    apply_dragon_dedup,
    assign_tiers,
    enrich_limit_pool,
    fetch_daban_pick,
    mode_label,
    parse_daban_pick_mode,
    percentile_ranks,
    passes_mode_filter,
    run_daban_pick_pipeline,
    score_candidates,
)
from .daban_pick_format import format_daban_pick_v2
from .daban_sector import build_concept_groups, extract_concept_key
from .index_daily_ts import SSE_INDEX_TS_CODE, fetch_sse_index_daily_as_eastmoney_history
from .klines import (
    adjust_eastmoney_to_tickflow,
    fetch_daily_klines_as_eastmoney_history,
    fetch_daily_klines_date_range,
)
from .limit_up_fetch import (
    fetch_last_sse_trade_date,
    fetch_limit_list_d,
    fetch_limit_list_ths,
    fetch_limit_step,
    fetch_moneyflow_dc_trade_date,
    fetch_top_list,
)
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
    "fetch_daily_klines_date_range",
    "SSE_INDEX_TS_CODE",
    "fetch_sse_index_daily_as_eastmoney_history",
    "fetch_moneyflow_mkt_dc_rows",
    "summarize_cn_equity_a_breadth",
    "fetch_cn_equity_a_breadth",
    "fetch_last_sse_trade_date",
    "fetch_limit_list_d",
    "fetch_limit_list_ths",
    "fetch_limit_step",
    "fetch_moneyflow_dc_trade_date",
    "fetch_top_list",
    "DabanPickMode",
    "DabanPickConfig",
    "fetch_daban_pick",
    "format_daban_pick_v2",
    "mode_label",
    "parse_daban_pick_mode",
    "enrich_limit_pool",
    "run_daban_pick_pipeline",
    "score_candidates",
    "passes_mode_filter",
    "percentile_ranks",
    "apply_dragon_dedup",
    "assign_tiers",
    "extract_concept_key",
    "build_concept_groups",
]
