"""全市场 A 股快照（标的池 CN_Equity_A）转为与 AKShare 前筛兼容的 DataFrame。"""

from __future__ import annotations

import os
from typing import Any, Optional


def _first_col(df: Any, candidates: tuple[str, ...]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _code_from_symbol(val: Any) -> str:
    s = str(val or "").strip()
    if "." in s:
        s = s.split(".")[0].strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:].zfill(6)
    return s.zfill(6) if s else ""


def fetch_cn_equity_a_spot_dataframe(api_key: str | None = None) -> Any:
    """
    拉取 CN_Equity_A 实时行情并返回 pandas DataFrame。

    列：代码、名称、涨跌幅（百分比数值）、成交额、成交量（若有）。

    :param api_key: 若为 None，使用 ``TICKFLOW_API_KEY``
    :raises ImportError: 未安装 pandas 或 tickflow
    :raises ValueError: 缺少 API Key、或返回空表
    """
    import pandas as pd

    key = api_key if api_key is not None else os.environ.get("TICKFLOW_API_KEY")
    if not key:
        raise ValueError("需要 api_key 或环境变量 TICKFLOW_API_KEY")

    from tickflow import TickFlow

    tf = TickFlow(api_key=key)
    raw = tf.quotes.get(universes=["CN_Equity_A"], as_dataframe=True)

    if raw is None:
        raise ValueError("Tickflow CN_Equity_A 返回 None")
    if not isinstance(raw, pd.DataFrame):
        raise ValueError(f"Tickflow quotes 期望 DataFrame，实际 {type(raw).__name__}")
    if len(raw) == 0:
        raise ValueError("Tickflow CN_Equity_A 返回空表")

    sym_col = _first_col(raw, ("symbol", "Symbol"))
    if not sym_col:
        raise ValueError(
            "Tickflow 快照缺少 symbol 列，列预览: " + repr(list(raw.columns)[:25])
        )

    name_col = _first_col(
        raw,
        ("ext.name", "name", "ext_name", "Name"),
    )
    pct_col = _first_col(
        raw,
        ("ext.change_pct", "change_pct", "changePercent", "ext_change_pct"),
    )
    amt_col = _first_col(raw, ("amount", "Amount", "turnover", "成交额"))
    vol_col = _first_col(raw, ("volume", "Volume", "vol"))

    n = len(raw)
    codes = raw[sym_col].map(_code_from_symbol).reset_index(drop=True)

    if name_col:
        names = raw[name_col].fillna("").map(lambda x: str(x).strip()).reset_index(drop=True)
    else:
        names = pd.Series([""] * n, dtype=object)

    if pct_col:
        chg = (pd.to_numeric(raw[pct_col], errors="coerce").fillna(0.0) * 100.0).reset_index(
            drop=True
        )
    else:
        chg = pd.Series([0.0] * n, dtype=float)

    if amt_col:
        amt = pd.to_numeric(raw[amt_col], errors="coerce").fillna(0.0).reset_index(drop=True)
    else:
        amt = pd.Series([0.0] * n, dtype=float)

    out_dict: dict[str, Any] = {
        "代码": codes,
        "名称": names,
        "涨跌幅": chg,
        "成交额": amt,
    }

    if vol_col:
        out_dict["成交量"] = (
            pd.to_numeric(raw[vol_col], errors="coerce").fillna(0.0).reset_index(drop=True)
        )

    out = pd.DataFrame(out_dict)
    out = out[out["代码"].astype(str).str.len() > 0]
    if len(out) == 0:
        raise ValueError("Tickflow CN_Equity_A 映射后无有效代码行")

    return out.reset_index(drop=True)
