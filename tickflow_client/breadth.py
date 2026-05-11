"""A 股全市场涨跌广度：由 ``fetch_cn_equity_a_spot_dataframe`` 输出的 DataFrame 聚合。"""

from __future__ import annotations

from typing import Any, Optional


def _find_pct_col(df: Any) -> Optional[str]:
    for c in ("涨跌幅", "changepercent", "change_pct", "pct_chg"):
        if c in df.columns:
            return c
    return None


def summarize_cn_equity_a_breadth(df: Any) -> Optional[dict[str, Any]]:
    """
    统计涨跌家数与占比（涨跌幅列为百分比数值，与 ``spot_universe`` 一致）。

    Returns:
        ``total``、``valid``（非空涨跌幅行数）、``up_count``、``down_count``、``flat_count``、
        ``advance_ratio``（涨/valid）、``decline_ratio``（跌/valid）；
        输入非法时返回 ``None``。
    """
    if df is None:
        return None
    try:
        import pandas as pd
    except ImportError:
        return None

    if not isinstance(df, pd.DataFrame) or len(df) == 0:
        return None

    col = _find_pct_col(df)
    if not col:
        return None

    s = pd.to_numeric(df[col], errors="coerce")
    valid_s = s.dropna()
    n = int(len(valid_s))
    if n == 0:
        return {
            "total": int(len(df)),
            "valid": 0,
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "advance_ratio": 0.0,
            "decline_ratio": 0.0,
        }

    up = int((valid_s > 0).sum())
    down = int((valid_s < 0).sum())
    flat = int((valid_s == 0).sum())

    return {
        "total": int(len(df)),
        "valid": n,
        "up_count": up,
        "down_count": down,
        "flat_count": flat,
        "advance_ratio": round(up / n, 6),
        "decline_ratio": round(down / n, 6),
    }


def fetch_cn_equity_a_breadth(api_key: str | None) -> Optional[dict[str, Any]]:
    """
    拉取 ``CN_Equity_A`` 快照并聚合涨跌广度。

    返回在 ``summarize_cn_equity_a_breadth`` 基础上含 ``source``: ``\"tickflow\"``；
    失败时 ``None``。
    """
    try:
        from .spot_universe import fetch_cn_equity_a_spot_dataframe

        df = fetch_cn_equity_a_spot_dataframe(api_key)
        s = summarize_cn_equity_a_breadth(df)
        if not s:
            return None
        out = dict(s)
        out["source"] = "tickflow"
        return out
    except Exception:
        return None
