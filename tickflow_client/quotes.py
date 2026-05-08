"""Tickflow REST 实时行情快照。"""

from __future__ import annotations

import os
from typing import Any

from .symbols import normalize_tickflow_symbol


def fetch_quotes(
    api_key: str | None,
    symbols: list[str],
    *,
    as_dataframe: bool = False,
    **kwargs: Any,
) -> Any:
    """
    批量获取标的实时行情。

    :param api_key: 若为 None，使用环境变量 ``TICKFLOW_API_KEY``
    :param symbols: 股票代码列表（6 位或带后缀）
    :param as_dataframe: 是否返回 pandas DataFrame（需安装 pandas）
    """
    key = api_key if api_key is not None else os.environ.get("TICKFLOW_API_KEY")
    if not key:
        raise ValueError("需要传入 api_key 或设置环境变量 TICKFLOW_API_KEY")

    from tickflow import TickFlow

    normalized = [normalize_tickflow_symbol(s) for s in symbols]
    tf = TickFlow(api_key=key)
    return tf.quotes.get(
        symbols=normalized, as_dataframe=as_dataframe, **kwargs
    )


def fetch_quote_realtime(
    api_key: str | None,
    code: str,
    *,
    as_dataframe: bool = False,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """获取单标的实时行情，返回第一条 dict；无数据返回 None。"""
    result = fetch_quotes(api_key, [code], as_dataframe=as_dataframe, **kwargs)
    if as_dataframe:
        raise ValueError("fetch_quote_realtime 不支持 as_dataframe=True")

    if not result:
        return None

    if isinstance(result, dict):
        return result

    if isinstance(result, list) and len(result) > 0:
        row = result[0]
        return row if isinstance(row, dict) else None

    return None
