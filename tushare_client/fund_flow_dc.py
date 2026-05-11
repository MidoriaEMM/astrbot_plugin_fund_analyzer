"""东方财富口径个股资金流：Tushare ``moneyflow_dc`` → 与 ``EastMoneyAPI.get_fund_flow`` list 条目对齐。

接口文档：https://tushare.pro/document/2?doc_id=349（盘后日更新，字段金额为万元）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ._auth import prepare_sdk
from .symbols import normalize_tickflow_symbol


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


def _trade_date_to_display(raw: Any) -> str:
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


def fetch_daily_moneyflow_as_em_fund_flow_list(
    api_key: str | None,
    code: str,
    days: int,
) -> Optional[list[dict[str, Any]]]:
    """
    拉取 ``moneyflow_dc``，映射为东财 ``_get_fund_flow_eastmoney`` 条目形状：

    - ``date``：``YYYY-MM-DD``
    - ``main_net_inflow`` / ``super_large_inflow`` / ``large_inflow`` / ``medium_inflow`` /
      ``small_inflow``：**元**（Tushare 万元 × 10000）

    ``days``：按交易日截取最近 ``days`` 条（日历窗口略放大以覆盖休市）。
    """
    try:
        import pandas as pd
    except ImportError:
        return None

    ts_code = normalize_tickflow_symbol(str(code).strip())
    need = max(int(days), 1)
    end_dt = datetime.now()
    span = max(need * 2 + 45, 60)
    start_dt = end_dt - timedelta(days=span)

    try:
        _, pro = prepare_sdk(api_key)
        raw = pro.moneyflow_dc(
            ts_code=ts_code,
            start_date=start_dt.strftime("%Y%m%d"),
            end_date=end_dt.strftime("%Y%m%d"),
        )
    except Exception:
        return None

    if raw is None:
        return None
    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
    if len(df) == 0:
        return None

    lc_map = _first_lower_map(df)
    td_col = _lc_col(lc_map, "trade_date", "tradeDate")
    if not td_col:
        return None

    ma = _lc_col(lc_map, "net_amount")
    elg = _lc_col(lc_map, "buy_elg_amount")
    lg = _lc_col(lc_map, "buy_lg_amount")
    md = _lc_col(lc_map, "buy_md_amount")
    sm = _lc_col(lc_map, "buy_sm_amount")

    cols = [c for c in (td_col, ma, elg, lg, md, sm) if c]
    if not cols:
        return None

    dd = df[cols].dropna(subset=[td_col], how="all")
    dd = dd.sort_values(td_col, ascending=True)

    rows: list[dict[str, Any]] = []
    for _, row in dd.iterrows():
        w_yuan = 10000.0
        rows.append(
            {
                "date": _trade_date_to_display(row[td_col]),
                "main_net_inflow": _safe_float(row[ma]) * w_yuan if ma else 0.0,
                "small_inflow": _safe_float(row[sm]) * w_yuan if sm else 0.0,
                "medium_inflow": _safe_float(row[md]) * w_yuan if md else 0.0,
                "large_inflow": _safe_float(row[lg]) * w_yuan if lg else 0.0,
                "super_large_inflow": _safe_float(row[elg]) * w_yuan if elg else 0.0,
            }
        )

    if not rows:
        return None

    tail = rows[-need:] if len(rows) > need else rows
    return tail
