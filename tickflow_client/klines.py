"""日 K 线：Tickflow klines.get 与东财 push2his 解析结果对齐（list[dict]）。"""

from __future__ import annotations

import os
from typing import Any, Optional

from .symbols import normalize_tickflow_symbol


def adjust_eastmoney_to_tickflow(adjust: str) -> str:
    """
    东财 get_fund_history 的 adjust → Tickflow ``klines.get`` 的 ``adjust``。
    差值前复权与东财/同花顺价格口径更一致。
    """
    a = (adjust or "").strip().lower()
    if a in ("hfq", "2", "backward"):
        return "backward_additive"
    if a in ("", "0", "none", "bfq", "不复权"):
        return "none"
    # 默认 qfq / 1
    return "forward_additive"


def _first_col(df: Any, candidates: tuple[str, ...]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        x = float(val)
        if x != x:  # nan
            return default
        return x
    except (TypeError, ValueError):
        return default


def _turnover_rate_pct_from_quote_ext(quote: dict[str, Any]) -> float:
    """实时行情 ext.turnover_rate：文档为小数比例（0.01=1%），转为百分比数值。"""
    ext = quote.get("ext") or {}
    if not isinstance(ext, dict):
        return 0.0
    r = _safe_float(ext.get("turnover_rate"))
    if r == 0.0:
        return 0.0
    if abs(r) < 1:
        return r * 100.0
    return r


def _maybe_patch_last_bar_turnover_from_quotes(
    api_key: str,
    raw_code: str,
    bars: list[dict[str, Any]],
) -> None:
    """日 K 缺最后一根换手率时，用 quotes 扩展字段补缺（与同接口文档一致）。"""
    if not bars:
        return
    last_tr = _safe_float(bars[-1].get("turnover_rate"))
    if last_tr > 1e-6:
        return
    try:
        from .quotes import fetch_quote_realtime

        q = fetch_quote_realtime(api_key, raw_code, as_dataframe=False)
        if not isinstance(q, dict):
            return
        pct = _turnover_rate_pct_from_quote_ext(q)
        if pct > 1e-6:
            bars[-1]["turnover_rate"] = pct
    except Exception:
        pass


def fetch_daily_klines_as_eastmoney_history(
    api_key: str | None,
    code: str,
    days: int,
    adjust: str = "qfq",
) -> list[dict[str, Any]] | None:
    """
    使用 Tickflow 拉取日 K，转成与 ``EastMoneyAPI._get_exchange_fund_history`` 相同的
    条目结构：date, open, close, high, low, volume, amount, change_rate, turnover_rate。
    日 K 未带换手时，会用同标的 **实时行情** ``ext.turnover_rate`` 补最后一根条的换手
    （见 https://docs.tickflow.org/zh-hans/api-reference/实时行情/查询实时行情 ）。

    :returns: 按日期升序的最近 ``days`` 条；条数不足阈值时返回 ``None`` 以便上层回退东财。
    """
    import pandas as pd

    key = api_key if api_key is not None else os.environ.get("TICKFLOW_API_KEY")
    if not key:
        return None

    symbol = normalize_tickflow_symbol(str(code).strip())
    d = max(int(days), 1)
    # 休市与节假日余量，不超过文档单次上限
    count = min(d + 50, 10000)
    tf_adjust = adjust_eastmoney_to_tickflow(adjust)

    from tickflow import TickFlow

    tf = TickFlow(api_key=key)
    df = tf.klines.get(symbol, period="1d", count=count, adjust=tf_adjust, as_dataframe=True)

    if df is None or not isinstance(df, pd.DataFrame) or len(df) == 0:
        return None

    date_col = _first_col(df, ("trade_date", "tradeDate", "date", "datetime"))
    if not date_col:
        return None

    o = _first_col(df, ("open", "Open"))
    h = _first_col(df, ("high", "High"))
    lw = _first_col(df, ("low", "Low"))
    cl = _first_col(df, ("close", "Close"))
    vo = _first_col(df, ("volume", "Volume", "vol"))
    am = _first_col(df, ("amount", "Amount", "turnover_value"))
    tr = _first_col(
        df,
        ("ext.turnover_rate", "turnover_rate", "turnover", "hs"),
    )

    if not all((o, h, lw, cl)):
        return None

    dd = df[[c for c in (date_col, o, h, lw, cl, vo, am, tr) if c]].copy()
    dd = dd.sort_values(date_col, ascending=True)

    prev_close = None
    rows: list[dict[str, Any]] = []
    for _, row in dd.iterrows():
        date_s = str(row[date_col])[:10].replace("/", "-")
        oc = _safe_float(row[o])
        hi = _safe_float(row[h])
        lo_v = _safe_float(row[lw])
        clo = _safe_float(row[cl])
        vol = _safe_float(row[vo]) if vo else 0.0
        amt = _safe_float(row[am]) if am else 0.0

        turnover_rate = 0.0
        if tr and tr in row.index:
            traw = row[tr]
            turnover_rate = _safe_float(traw)
            if 0 < abs(turnover_rate) < 1:
                turnover_rate *= 100.0

        change_rate = 0.0
        if prev_close is not None and prev_close > 0:
            change_rate = round((clo - prev_close) / prev_close * 100, 4)

        rows.append(
            {
                "date": date_s,
                "open": oc,
                "close": clo,
                "high": hi,
                "low": lo_v,
                "volume": vol,
                "amount": amt,
                "change_rate": change_rate,
                "turnover_rate": turnover_rate,
            }
        )
        prev_close = clo

    if not rows:
        return None

    tail = rows[-d:] if len(rows) > d else rows
    min_required = min(d, 5)
    if len(tail) < min_required:
        return None

    rc = str(code).strip()
    _maybe_patch_last_bar_turnover_from_quotes(key, rc, tail)
    return tail
