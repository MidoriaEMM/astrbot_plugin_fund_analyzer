"""
与日 K 同源的对齐指标（ATR、近 5 日均成交额），不含额外 HTTP。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _safe_float(val: Any) -> float:
    try:
        if val is None:
            return 0.0
        return float(val)
    except (ValueError, TypeError):
        return 0.0


@dataclass
class DailyAlignmentMetrics:
    """与辩论 pipeline 使用的 history_data 最后一根 bar 对齐。"""

    last_bar_date: str
    atr14: float | None
    avg_amount_5d_yi: float | None
    ref_close: float

    def as_alignment_dict(self) -> dict[str, Any]:
        return {
            "last_bar_date": self.last_bar_date,
            "atr14": self.atr14,
            "avg_amount_5d_yi": self.avg_amount_5d_yi,
            "ref_close": self.ref_close,
        }


def compute_daily_alignment_metrics(
    history_data: list[dict],
    quant_analyzer: Any,
    *,
    atr_period: int = 14,
    avg_amount_days: int = 5,
) -> DailyAlignmentMetrics | None:
    """
    从历史 K 线计算 ATR 与近 N 日日均成交额（亿元）。
    history 不足或非列表时返回 None。
    """
    if not history_data or not isinstance(history_data, list):
        return None

    last = history_data[-1]
    raw_date = last.get("date", "")
    last_bar_date = str(raw_date) if raw_date is not None else ""

    closes = [_safe_float(d.get("close", 0)) for d in history_data]
    highs = [
        _safe_float(d.get("high", c)) for d, c in zip(history_data, closes)
    ]
    lows = [_safe_float(d.get("low", c)) for d, c in zip(history_data, closes)]

    ref_close = closes[-1] if closes else 0.0

    atr14: float | None = None
    if quant_analyzer is not None and len(closes) >= atr_period + 1:
        atr14 = quant_analyzer.calculate_atr(highs, lows, closes, atr_period)

    amounts = [_safe_float(d.get("amount", 0)) for d in history_data]
    tail_n = amounts[-avg_amount_days:] if amounts else []
    avg_amt = sum(tail_n) / len(tail_n) if tail_n else None
    avg_amount_5d_yi = (
        round(avg_amt / 1e8, 6) if avg_amt is not None else None
    )

    return DailyAlignmentMetrics(
        last_bar_date=last_bar_date,
        atr14=atr14,
        avg_amount_5d_yi=avg_amount_5d_yi,
        ref_close=ref_close,
    )
