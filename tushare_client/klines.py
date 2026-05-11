"""日 K：`ts.pro_bar` 与东方财富 ``push2his`` 条目结构对齐（``list[dict]``）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ._auth import prepare_sdk
from .symbols import normalize_tickflow_symbol


def adjust_eastmoney_to_tickflow(adjust: str) -> Optional[str]:
    """
    东财 ``get_fund_history`` / 业务侧的 ``adjust`` 字符串 → ``ts.pro_bar`` 的 ``adj``。

    返回值 **不是** Tickflow ``klines.get`` 的 ``forward_additive`` / ``backward_additive``
    字面量。

    Returns:
        ``"qfq"`` / ``"hfq"`` / ``None``（不复权）
    """
    a = (adjust or "").strip().lower()
    if a in ("hfq", "2", "backward"):
        return "hfq"
    if a in ("", "0", "none", "bfq", "不复权"):
        return None
    return "qfq"


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


def fetch_daily_klines_as_eastmoney_history(
    api_key: str | None,
    code: str,
    days: int,
    adjust: str = "qfq",
) -> list[dict[str, Any]] | None:
    """
    使用 ``ts.pro_bar`` 拉取日 K（含换手率因子 ``tor``），转成与同项目东财
    ``EastMoneyAPI._get_exchange_fund_history`` 相同的条目结构：
    ``date, open, close, high, low, volume, amount, change_rate, turnover_rate``。

    官方口径下 ``vol`` 为「手」，``amount`` 为「千元」；本条目中的 ``volume`` 已换算为「股」，
    ``amount`` 已换算为「元」，与同项目场内 K / Tickflow 习惯一致。
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("请安装 pandas") from exc

    ts_code = normalize_tickflow_symbol(str(code).strip())
    d_need = max(int(days), 1)
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=d_need * 3 + 90)

    ts_mod, _pro = prepare_sdk(api_key)

    adj = adjust_eastmoney_to_tickflow(adjust)
    pb_kw: dict[str, Any] = {
        "ts_code": ts_code,
        "asset": "E",
        "freq": "D",
        "start_date": start_dt.strftime("%Y%m%d"),
        "end_date": end_dt.strftime("%Y%m%d"),
        "factors": ["tor"],
    }
    if adj is not None:
        pb_kw["adj"] = adj

    try:
        raw = ts_mod.pro_bar(**pb_kw)
    except Exception:
        raw = None

    if raw is None or not isinstance(raw, pd.DataFrame) or len(raw) == 0:
        pb_fallback = dict(pb_kw)
        pb_fallback.pop("factors", None)
        try:
            raw_fb = ts_mod.pro_bar(**pb_fallback)
        except Exception:
            raw_fb = None
        if raw_fb is not None and isinstance(raw_fb, pd.DataFrame) and len(raw_fb) > 0:
            raw = raw_fb

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
    pct = _col(lc_map, ("pct_chg",))
    tr = _col(lc_map, ("tor", "turnover_rate", "turnover"))

    if not all((dc, o, h, lw, cl)):
        return None

    dd = raw[[c for c in (dc, o, h, lw, cl, vo, am, pct, tr) if c]].copy()
    dd = dd.sort_values(dc, ascending=True)

    prev_close = None
    rows: list[dict[str, Any]] = []
    for _, row in dd.iterrows():
        date_s = str(row[dc])[:10].replace("/", "-")

        clo = _safe_float(row[cl])

        pct_chg = _safe_float(row[pct]) if pct else float("nan")
        if pct_chg == pct_chg:  # non-NaN
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
