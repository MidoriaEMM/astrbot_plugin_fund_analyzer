"""上证指数日 K：TickFlow ``klines.get('000001.SH', period='1d')`` → 东财形 ``list[dict]``。

与 ``evaluate_market_regime``、``tushare_client.fetch_sse_index_daily_as_eastmoney_history`` 输出对齐。

- K 线文档：https://docs.tickflow.org/zh-hans/api-reference/k线数据/查询-k线数据.md
"""

from __future__ import annotations

from typing import Any, Optional

from .klines import fetch_daily_klines_as_eastmoney_history

SSE_INDEX_SYMBOL = "000001.SH"


def fetch_sse_index_daily_as_eastmoney_history(
    api_key: str | None,
    days: int,
    adjust: str = "qfq",
) -> Optional[list[dict[str, Any]]]:
    """拉取上证指数 ``000001.SH`` 最近 ``days`` 根日 K（复用通用日 K 转换）。"""
    return fetch_daily_klines_as_eastmoney_history(api_key, SSE_INDEX_SYMBOL, days, adjust)
