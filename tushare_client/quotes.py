"""Tushare ``rt_k`` 实时日线 → 与 Tickflow 形态接近的行情 dict/list。"""

from __future__ import annotations

from typing import Any

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


def rt_k_record_to_tickflow_quote(rec: dict[str, Any]) -> dict[str, Any]:
    ts_code = str(rec.get("ts_code") or rec.get("TS_CODE") or "").strip()
    nm = rec.get("name") if rec.get("name") is not None else rec.get("NAME")
    close_v = _safe_float(rec.get("close") if rec.get("close") is not None else rec.get("CLOSE"))
    pre_close = _safe_float(
        rec.get("pre_close") if rec.get("pre_close") is not None else rec.get("PRE_CLOSE")
    )
    open_v = _safe_float(rec.get("open") if rec.get("open") is not None else rec.get("OPEN"))
    hi = _safe_float(rec.get("high") if rec.get("high") is not None else rec.get("HIGH"))
    lo_v = _safe_float(rec.get("low") if rec.get("low") is not None else rec.get("LOW"))
    vol = _safe_float(rec.get("vol") if rec.get("vol") is not None else rec.get("VOL"))
    amt = _safe_float(rec.get("amount") if rec.get("amount") is not None else rec.get("AMOUNT"))

    change_pct_ratio = 0.0
    change_amount = 0.0
    if pre_close > 0:
        change_amount = round(close_v - pre_close, 6)
        change_pct_ratio = (close_v - pre_close) / pre_close

    return {
        "symbol": ts_code,
        "last_price": close_v,
        "prev_close": pre_close,
        "open": open_v,
        "high": hi,
        "low": lo_v,
        "volume": vol,
        "amount": amt,
        "name": str(nm or ""),
        "ext": {
            "name": str(nm or ""),
            "change_pct": change_pct_ratio,
            "change_amount": change_amount,
            "amplitude": 0,
            "turnover_rate": 0,
        },
    }


def fetch_quotes(
    api_key: str | None,
    symbols: list[str],
    *,
    as_dataframe: bool = False,
    **kwargs: Any,
) -> Any:
    """
    批量获取标的实时日线（开盘以来快照），接口 ``rt_k``。

    ``rt_k`` 需单独权限，见 https://tushare.pro/document/2?doc_id=372

    :param api_key: Tushare token；``None`` 时用 ``TUSHARE_TOKEN``
    :param symbols: 股票代码列表（六位或带后缀）
    :param as_dataframe: 为 True 时返回 pandas.DataFrame（原表列 + 可读性一致）
                      若为 False（默认），返回 ``list[dict]``「类 Tickflow」结构
    :param kwargs: 透传给 ``pro.rt_k``（若服务端支持）

    raises:
        ImportError: 未安装 ``pandas`` 或 ``tushare``
        ValueError: 缺少 token；或 ``fetch_quote_realtime(as_dataframe=True)``
    """
    if not symbols:
        if as_dataframe:
            try:
                import pandas as pd
            except ImportError as exc:
                raise ImportError("请安装 pandas: pip install pandas") from exc
            return pd.DataFrame()
        return []

    try:
        import pandas as pd
    except ImportError as exc:
        raise ImportError("请安装 pandas: pip install pandas") from exc

    normalized = [normalize_tickflow_symbol(s) for s in symbols]
    ts_code_arg = ",".join(normalized)

    _, pro = prepare_sdk(api_key)

    raw = pro.rt_k(ts_code=ts_code_arg, **kwargs)

    if raw is None:
        return [] if not as_dataframe else pd.DataFrame()
    df = raw if isinstance(raw, pd.DataFrame) else pd.DataFrame(raw)

    if as_dataframe:
        return df

    quotes: list[dict[str, Any]] = []
    for rec in df.to_dict("records"):
        quotes.append(rt_k_record_to_tickflow_quote(rec))
    return quotes


def fetch_quote_realtime(
    api_key: str | None,
    code: str,
    *,
    as_dataframe: bool = False,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """获取单标的 ``rt_k`` 第一行；无数据返回 ``None``。"""
    result = fetch_quotes(api_key, [code], as_dataframe=as_dataframe, **kwargs)
    if as_dataframe:
        raise ValueError("fetch_quote_realtime 不支持 as_dataframe=True")

    if not result:
        return None

    row = result[0]
    return row if isinstance(row, dict) else None
