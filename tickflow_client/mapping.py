"""将 Tickflow 行情 dict 映射为与本项目 StockInfo 字段对齐的扁平 dict（可独立复用）。"""

from __future__ import annotations

from typing import Any


def _safe_float(val: Any, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _ratio_to_pct(ratio: Any) -> float:
    """Tickflow ext 中年涨跌幅、振幅、换手率等为小数比例时转为百分比数值。"""
    x = _safe_float(ratio, float("nan"))
    if x != x:  # NaN
        return 0.0
    return x * 100.0


def quote_to_stock_fields(quote: dict[str, Any], raw_code_display: str) -> dict[str, Any]:
    """
    把 ``tf.quotes.get`` 返回的单条 dict 转为可 ``StockInfo(**fields)`` 的字段。

    :param quote: Tickflow 行情记录
    :param raw_code_display: 对外展示用的股票代码（如 ``000001``），与 AKShare 路径一致
    """
    ext = quote.get("ext") or {}
    if not isinstance(ext, dict):
        ext = {}

    symbol = str(quote.get("symbol") or "")
    short_code = symbol.split(".")[0] if "." in symbol else symbol
    rd = str(raw_code_display).strip()
    if rd.isdigit():
        code = rd.zfill(6)
    elif "." in rd and rd.split(".")[0].strip().isdigit():
        code = rd.split(".")[0].strip().zfill(6)
    elif short_code.isdigit():
        code = short_code.zfill(6)
    else:
        code = short_code or rd

    change_pct_ratio = ext.get("change_pct")
    change_rate = _ratio_to_pct(change_pct_ratio)
    last_price = _safe_float(quote.get("last_price"))
    prev_close = _safe_float(quote.get("prev_close"))
    if change_rate == 0.0 and prev_close > 0 and last_price > 0:
        change_rate = (last_price - prev_close) / prev_close * 100.0

    change_amount = _safe_float(ext.get("change_amount"))
    if change_amount == 0.0 and last_price > 0 and prev_close > 0:
        change_amount = last_price - prev_close

    return {
        "code": code,
        "name": str(ext.get("name") or quote.get("name") or ""),
        "latest_price": last_price,
        "change_amount": change_amount,
        "change_rate": change_rate,
        "open_price": _safe_float(quote.get("open")),
        "high_price": _safe_float(quote.get("high")),
        "low_price": _safe_float(quote.get("low")),
        "prev_close": prev_close,
        "volume": _safe_float(quote.get("volume")),
        "amount": _safe_float(quote.get("amount")),
        "amplitude": _ratio_to_pct(ext.get("amplitude")),
        "turnover_rate": _ratio_to_pct(ext.get("turnover_rate")),
        "pe_ratio": 0.0,
        "pb_ratio": 0.0,
        "total_market_cap": 0.0,
        "circulating_market_cap": 0.0,
    }
