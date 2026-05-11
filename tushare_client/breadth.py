"""A 股涨跌广度：基于 ``rt_k`` 全市场快照聚合（与 Tickflow 侧口径对齐）。"""

from __future__ import annotations

from typing import Any, Optional


def summarize_cn_equity_a_breadth(df: Any) -> Optional[dict[str, Any]]:
    """
    统计涨跌家数与占比；``df`` 须含列 ``涨跌幅``（百分比数值），与 ``spot_universe`` 输出一致。
    """
    if df is None:
        return None
    try:
        import pandas as pd
    except ImportError:
        return None

    if not isinstance(df, pd.DataFrame) or len(df) == 0:
        return None

    col = "涨跌幅"
    if col not in df.columns:
        for c in ("changepercent", "change_pct", "pct_chg"):
            if c in df.columns:
                col = c
                break
        else:
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
    拉取 ``rt_k`` 全市场快照并聚合涨跌广度。

    返回字段在 ``summarize_cn_equity_a_breadth`` 基础上增加 ``source``: ``\"tushare\"``。
    失败或空表时返回 ``None``。``rt_k`` 权限见 https://tushare.pro/document/2?doc_id=372
    """
    try:
        from .spot_universe import fetch_cn_equity_a_spot_dataframe

        df = fetch_cn_equity_a_spot_dataframe(api_key)
        s = summarize_cn_equity_a_breadth(df)
        if not s:
            return None
        out = dict(s)
        out["source"] = "tushare"
        return out
    except Exception:
        return None
