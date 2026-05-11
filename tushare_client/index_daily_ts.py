"""上证指数日 K：Tushare ``index_daily``（或 ``pro_bar`` 指数）→ 东财形 ``list[dict]``。

与 ``evaluate_market_regime`` 输入对齐（``close`` / ``high`` / ``low`` / ``volume`` 等）。

- 文档：https://tushare.pro/document/2?doc_id=95
- 需相应积分/权限；``index_daily`` 与 ``pro_bar`` 权限可能不同，失败时可自动尝试另一路径。

``adjust``：指数点位通常无股票式复权；保留参数以与 ``fetch_daily_klines_as_eastmoney_history`` 签名一致，
若走 ``pro_bar(..., asset='I')`` 时会参与 ``adj`` 映射，``index_daily`` 路径则忽略该参数。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ._auth import prepare_sdk
from .klines import adjust_eastmoney_to_tickflow

SSE_INDEX_TS_CODE = "000001.SH"


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        if val is None or val == "":
            return default
        x = float(val)
        if x != x:
            return default
        return x
    except (TypeError, ValueError):
        return default


def _first_lower_map(df: Any) -> dict[str, Any]:
    return {str(c).lower(): c for c in df.columns}


def _col(lc: dict[str, str], names: tuple[str, ...]) -> Optional[str]:
    for n in names:
        orig = lc.get(n.lower())
        if orig is not None:
            return orig
    return None


def _rows_from_daily_df(
    raw: Any,
    *,
    d_need: int,
) -> Optional[list[dict[str, Any]]]:
    """从已排序的日 K DataFrame 生成东财形条目（与 ``tushare_client.klines`` 一致）。"""
    try:
        import pandas as pd
    except ImportError:
        return None

    if raw is None or not isinstance(raw, pd.DataFrame) or len(raw) == 0:
        return None

    lc_map = _first_lower_map(raw)
    dc = _col(lc_map, ("trade_date", "datetime", "tradeDate"))
    o = _col(lc_map, ("open",))
    h = _col(lc_map, ("high",))
    lw = _col(lc_map, ("low",))
    cl = _col(lc_map, ("close",))
    vo = _col(lc_map, ("vol", "volume"))
    am = _col(lc_map, ("amount",))
    pct = _col(lc_map, ("pct_chg", "pctchg"))
    tr = _col(lc_map, ("tor", "turnover_rate", "turnover"))

    if not all((dc, o, h, lw, cl)):
        return None

    cols = [c for c in (dc, o, h, lw, cl, vo, am, pct, tr) if c]
    dd = raw[cols].copy()
    dd = dd.sort_values(dc, ascending=True)

    prev_close: float | None = None
    rows: list[dict[str, Any]] = []
    for _, row in dd.iterrows():
        date_s = str(row[dc])[:10].replace("/", "-")
        if len(date_s) == 8 and date_s.isdigit():
            date_s = f"{date_s[:4]}-{date_s[4:6]}-{date_s[6:8]}"

        clo = _safe_float(row[cl])

        pct_chg = _safe_float(row[pct]) if pct else float("nan")
        if pct_chg == pct_chg:
            change_rate = round(float(pct_chg), 4)
        elif prev_close is not None and prev_close > 0:
            change_rate = round((clo - prev_close) / prev_close * 100, 4)
        else:
            change_rate = 0.0

        vol_hand = _safe_float(row[vo]) if vo else 0.0
        amt_kilo = _safe_float(row[am]) if am else 0.0

        turnover_rate = 0.0
        if tr and tr in row.index:
            t_raw = row[tr]
            turnover_rate = _safe_float(t_raw)
            if 0 < abs(turnover_rate) < 1:
                turnover_rate *= 100.0

        rows.append(
            {
                "date": date_s,
                "open": _safe_float(row[o]),
                "close": clo,
                "high": _safe_float(row[h]),
                "low": _safe_float(row[lw]),
                "volume": vol_hand * 100.0,
                "amount": amt_kilo * 1000.0,
                "change_rate": change_rate,
                "turnover_rate": turnover_rate,
            }
        )
        prev_close = clo

    if not rows:
        return None

    tail = rows[-d_need:] if len(rows) > d_need else rows
    min_required = min(d_need, 5)
    if len(tail) < min_required:
        return None

    return tail


def fetch_sse_index_daily_as_eastmoney_history(
    api_key: str | None,
    days: int,
    adjust: str = "qfq",
) -> list[dict[str, Any]] | None:
    """
    拉取上证指数 ``000001.SH`` 最近 ``days`` 根日 K，结构同东财 ``get_kline_history_by_secid``。

    优先 ``index_daily``；无数据或异常时回退 ``pro_bar``（``asset='I'``）。
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("请安装 pandas") from exc

    d_need = max(int(days), 1)
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=d_need * 3 + 90)

    ts_mod, pro = prepare_sdk(api_key)
    start_s = start_dt.strftime("%Y%m%d")
    end_s = end_dt.strftime("%Y%m%d")

    raw: Any = None
    try:
        raw = pro.index_daily(
            ts_code=SSE_INDEX_TS_CODE,
            start_date=start_s,
            end_date=end_s,
        )
    except Exception:
        raw = None

    out = _rows_from_daily_df(raw, d_need=d_need) if raw is not None else None
    if out is not None:
        return out

    adj = adjust_eastmoney_to_tickflow(adjust)
    pb_kw: dict[str, Any] = {
        "ts_code": SSE_INDEX_TS_CODE,
        "asset": "I",
        "freq": "D",
        "start_date": start_s,
        "end_date": end_s,
    }
    if adj is not None:
        pb_kw["adj"] = adj

    try:
        raw_i = ts_mod.pro_bar(**pb_kw)
    except Exception:
        raw_i = None

    if raw_i is None or not isinstance(raw_i, pd.DataFrame) or len(raw_i) == 0:
        pb_fallback = dict(pb_kw)
        pb_fallback.pop("adj", None)
        try:
            raw_i = ts_mod.pro_bar(**pb_fallback)
        except Exception:
            raw_i = None

    return _rows_from_daily_df(raw_i, d_need=d_need)
