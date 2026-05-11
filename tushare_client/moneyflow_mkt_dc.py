"""东方财富口径大盘资金流：Tushare ``moneyflow_mkt_dc`` → ``list[dict]``。

文档：https://tushare.pro/document/2?doc_id=345
盘后更新；需相应积分/权限。失败或空表时返回 ``None``。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ._auth import prepare_sdk


def _trade_date_to_iso(raw: Any) -> str:
    s = str(raw or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return str(s).replace("/", "-")[:10]


def _first_lower_map(df: Any) -> dict[str, str]:
    return {str(c).lower(): c for c in df.columns}


def _lc_col(lc_map: dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        orig = lc_map.get(n.lower())
        if orig is not None:
            return orig
    return None


def fetch_moneyflow_mkt_dc_rows(
    api_key: str | None,
    *,
    trade_date: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int | None = None,
) -> Optional[list[dict[str, Any]]]:
    """
    拉取大盘资金流向（DC），按交易日升序。

    :param trade_date: ``YYYYMMDD``，单日
    :param start_date / end_date: ``YYYYMMDD`` 区间（与 ``trade_date`` 互斥时可组合使用）
    :param limit: 仅保留最近 ``limit`` 条（截取升序列表尾部）
    """
    try:
        import pandas as pd
    except ImportError:
        return None

    _, pro = prepare_sdk(api_key)

    kw: dict[str, Any] = {}
    if trade_date:
        kw["trade_date"] = str(trade_date).replace("-", "")[:8]
    if start_date:
        kw["start_date"] = str(start_date).replace("-", "")[:8]
    if end_date:
        kw["end_date"] = str(end_date).replace("-", "")[:8]

    if not kw:
        end_dt = datetime.now()
        kw["end_date"] = end_dt.strftime("%Y%m%d")
        kw["start_date"] = (end_dt - timedelta(days=120)).strftime("%Y%m%d")

    try:
        raw = pro.moneyflow_mkt_dc(**kw)
    except Exception:
        return None

    if raw is None:
        return None
    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
    if len(df) == 0:
        return None

    lc = _first_lower_map(df)
    td = _lc_col(lc, "trade_date", "tradedate")
    if not td:
        return None

    numeric_cols = [
        "close_sh",
        "pct_change_sh",
        "close_sz",
        "pct_change_sz",
        "net_amount",
        "net_amount_rate",
        "buy_elg_amount",
        "buy_elg_amount_rate",
        "buy_lg_amount",
        "buy_lg_amount_rate",
        "buy_md_amount",
        "buy_md_amount_rate",
        "buy_sm_amount",
        "buy_sm_amount_rate",
    ]

    dd = df.sort_values(td, ascending=True)
    rows: list[dict[str, Any]] = []
    for _, row in dd.iterrows():
        item: dict[str, Any] = {"trade_date": _trade_date_to_iso(row[td])}
        for name in numeric_cols:
            col = _lc_col(lc, name)
            if not col:
                continue
            v = row[col]
            try:
                if v is None or (isinstance(v, float) and v != v):
                    item[name] = None
                else:
                    item[name] = float(v)
            except (TypeError, ValueError):
                item[name] = None
        rows.append(item)

    if not rows:
        return None

    if limit is not None and limit > 0 and len(rows) > limit:
        rows = rows[-limit:]

    return rows
