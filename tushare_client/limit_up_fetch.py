"""Tushare 打板/涨跌停相关接口拉取，返回 list[dict]（与 AstrBot 解耦）。

文档：limit_list_d / limit_step / limit_list_ths / top_list / moneyflow_dc 日切片。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Optional

from ._auth import prepare_sdk


def _dataframe_to_records(df: Any) -> list[dict[str, Any]]:
    import numpy as np
    import pandas as pd

    if df is None:
        return []
    if not isinstance(df, pd.DataFrame):
        df = pd.DataFrame(df)
    if len(df) == 0:
        return []
    out = df.replace({np.nan: None})
    raw = out.to_dict(orient="records")
    rows: list[dict[str, Any]] = []
    for item in raw:
        row: dict[str, Any] = {}
        for k, v in item.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                row[str(k)] = None
            elif hasattr(v, "item"):  # numpy scalar
                try:
                    row[str(k)] = v.item()
                except Exception:
                    row[str(k)] = v
            else:
                row[str(k)] = v
        rows.append(row)
    return rows


def _normalize_trade_date(s: str) -> str:
    t = (s or "").strip().replace("-", "")
    if len(t) >= 8 and t[:8].isdigit():
        return t[:8]
    raise ValueError(f"无效交易日期: {s!r}，请使用 YYYYMMDD")


def fetch_last_sse_trade_date(api_key: str | None, *, end_yyyymmdd: str | None = None) -> str | None:
    """上交所交易日历取 <= end 的最后一个开市日（YYYYMMDD）。"""
    try:
        import pandas as pd
    except ImportError:
        return None
    _, pro = prepare_sdk(api_key)
    if end_yyyymmdd:
        end_d = datetime.strptime(_normalize_trade_date(end_yyyymmdd), "%Y%m%d").date()
    else:
        end_d = datetime.now().date()
    start_d = end_d - timedelta(days=60)
    try:
        raw = pro.trade_cal(
            exchange="SSE",
            start_date=start_d.strftime("%Y%m%d"),
            end_date=end_d.strftime("%Y%m%d"),
            is_open="1",
        )
    except Exception:
        return None
    if raw is None:
        return None
    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
    if len(df) == 0:
        return None
    lc = {str(c).lower(): c for c in df.columns}
    cal_col = lc.get("cal_date")
    if not cal_col:
        return None
    dates = df[cal_col].astype(str).str.replace("-", "").str[:8]
    valid = dates[dates.str.match(r"^\d{8}$")]
    if len(valid) == 0:
        return None
    return str(valid.iloc[-1])


def fetch_limit_list_d(
    api_key: str | None,
    trade_date: str,
    *,
    limit_type: str = "U",
) -> list[dict[str, Any]]:
    """涨跌停列表（新），默认涨停 U。"""
    try:
        import pandas as pd
    except ImportError:
        return []

    td = _normalize_trade_date(trade_date)
    _, pro = prepare_sdk(api_key)
    page = 8000
    offset = 0
    all_rows: list[dict[str, Any]] = []
    while True:
        try:
            raw = pro.limit_list_d(
                trade_date=td,
                limit_type=limit_type,
                limit=page,
                offset=offset,
            )
        except TypeError:
            try:
                raw = pro.limit_list_d(trade_date=td, limit_type=limit_type)
            except Exception:
                break
            df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
            return _dataframe_to_records(df)
        except Exception:
            break
        if raw is None:
            break
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        chunk = _dataframe_to_records(df)
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(df) < page:
            break
        offset += len(df)
    return all_rows


def fetch_limit_step(api_key: str | None, trade_date: str) -> list[dict[str, Any]]:
    """连板天梯。"""
    try:
        import pandas as pd
    except ImportError:
        return []

    td = _normalize_trade_date(trade_date)
    _, pro = prepare_sdk(api_key)
    page = 5000
    offset = 0
    all_rows: list[dict[str, Any]] = []
    while True:
        try:
            raw = pro.limit_step(trade_date=td, limit=page, offset=offset)
        except TypeError:
            try:
                raw = pro.limit_step(trade_date=td)
            except Exception:
                break
            df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
            return _dataframe_to_records(df)
        except Exception:
            break
        if raw is None:
            break
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        chunk = _dataframe_to_records(df)
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(df) < page:
            break
        offset += len(df)
    return all_rows


def fetch_limit_list_ths(
    api_key: str | None,
    trade_date: str,
    *,
    limit_type: str = "涨停池",
) -> list[dict[str, Any]]:
    """同花顺涨跌停榜单（涨停池等）。"""
    try:
        import pandas as pd
    except ImportError:
        return []

    td = _normalize_trade_date(trade_date)
    _, pro = prepare_sdk(api_key)
    page = 4000
    offset = 0
    all_rows: list[dict[str, Any]] = []
    while True:
        try:
            raw = pro.limit_list_ths(
                trade_date=td,
                limit_type=limit_type,
                limit=page,
                offset=offset,
            )
        except TypeError:
            # 旧版 sdk 无 limit/offset
            try:
                raw = pro.limit_list_ths(trade_date=td, limit_type=limit_type)
            except Exception:
                break
            df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
            return _dataframe_to_records(df)
        except Exception:
            break
        if raw is None:
            break
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        chunk = _dataframe_to_records(df)
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(df) < page:
            break
        offset += len(df)
    return all_rows


def fetch_top_list(api_key: str | None, trade_date: str) -> list[dict[str, Any]]:
    """龙虎榜每日明细（同一股票可多条 reason）。"""
    try:
        import pandas as pd
    except ImportError:
        return []

    td = _normalize_trade_date(trade_date)
    _, pro = prepare_sdk(api_key)
    page = 8000
    offset = 0
    all_rows: list[dict[str, Any]] = []
    while True:
        try:
            raw = pro.top_list(trade_date=td, limit=page, offset=offset)
        except TypeError:
            try:
                raw = pro.top_list(trade_date=td)
            except Exception:
                break
            df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
            return _dataframe_to_records(df)
        except Exception:
            break
        if raw is None:
            break
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        chunk = _dataframe_to_records(df)
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(df) < page:
            break
        offset += len(df)
    return all_rows


def fetch_moneyflow_dc_trade_date(
    api_key: str | None,
    trade_date: str,
) -> list[dict[str, Any]]:
    """东财个股资金流向：单日全市场（分页）。"""
    try:
        import pandas as pd
    except ImportError:
        return []

    td = _normalize_trade_date(trade_date)
    _, pro = prepare_sdk(api_key)
    page = 6000
    offset = 0
    all_rows: list[dict[str, Any]] = []
    while True:
        try:
            raw = pro.moneyflow_dc(trade_date=td, limit=page, offset=offset)
        except TypeError:
            try:
                raw = pro.moneyflow_dc(trade_date=td)
            except Exception:
                break
            df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
            return _dataframe_to_records(df)
        except Exception:
            break
        if raw is None:
            break
        df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
        chunk = _dataframe_to_records(df)
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(df) < page:
            break
        offset += len(df)
    return all_rows


def fetch_moneyflow_dc_one(
    api_key: str | None,
    trade_date: str,
    ts_code: str,
) -> dict[str, Any] | None:
    """东财个股资金流向：单日单股。返回一行 dict 或 None。"""
    try:
        import pandas as pd
    except ImportError:
        return None

    from .symbols import normalize_tickflow_symbol

    td = _normalize_trade_date(trade_date)
    sym = normalize_tickflow_symbol(ts_code)
    _, pro = prepare_sdk(api_key)
    try:
        raw = pro.moneyflow_dc(trade_date=td, ts_code=sym)
    except Exception:
        return None
    if raw is None:
        return None
    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
    rows = _dataframe_to_records(df)
    if not rows:
        return None
    return rows[0]


def fetch_limit_list_d_one(
    api_key: str | None,
    trade_date: str,
    ts_code: str,
    *,
    limit_type: str = "U",
) -> dict[str, Any] | None:
    """涨跌停列表（新）：单日单股。返回一行 dict 或 None。"""
    try:
        import pandas as pd
    except ImportError:
        return None

    from .symbols import normalize_tickflow_symbol

    td = _normalize_trade_date(trade_date)
    sym = normalize_tickflow_symbol(ts_code)
    _, pro = prepare_sdk(api_key)
    try:
        raw = pro.limit_list_d(trade_date=td, ts_code=sym, limit_type=limit_type)
    except Exception:
        return None
    if raw is None:
        return None
    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)
    rows = _dataframe_to_records(df)
    if not rows:
        return None
    return rows[0]
