"""全市场 A 股快照（rt_k + wildcard 分批）→ 与 Tickflow 前筛对齐的 DataFrame。"""

from __future__ import annotations

from typing import Any, Optional


def _first_col(df: Any, candidates: tuple[str, ...]) -> Optional[str]:
    cols = getattr(df, "columns", [])
    lc = {str(c).lower(): c for c in cols}
    for c in candidates:
        if c in df.columns:
            return c
        x = lc.get(c.lower())
        if x is not None:
            return x
    return None


def _code_from_ts_code(val: Any) -> str:
    s = str(val or "").strip()
    if "." in s:
        s = s.split(".")[0].strip()
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:].zfill(6)
    return s.zfill(6) if s else ""


# 分拆请求减轻单次超限；``9*.BJ`` 与 ``920*.BJ`` 等有重叠时按 ts_code 去重
_RT_K_PATTERNS: tuple[str, ...] = (
    "6*.SH",
    "5*.SH",
    "0*.SZ",
    "1*.SZ",
    "2*.SZ",
    "3*.SZ",
    "920*.BJ",
    "43*.BJ",
    "83*.BJ",
    "87*.BJ",
    "9*.BJ",
)


def fetch_cn_equity_a_spot_dataframe(api_key: str | None = None) -> Any:
    """
    分批 ``rt_k`` 通配拉取后与 ``tickflow_client.fetch_cn_equity_a_spot_dataframe``
    列对齐：**代码 / 名称 / 涨跌幅(%) / 成交额 / 成交量**。

    ``rt_k`` 权限与限额见 https://tushare.pro/document/2?doc_id=372
    """
    import pandas as pd

    from ._auth import prepare_sdk

    _, pro = prepare_sdk(api_key)

    frames: list[Any] = []
    for patt in _RT_K_PATTERNS:
        chunk = pro.rt_k(ts_code=patt)
        if chunk is not None and len(chunk) > 0:
            frames.append(chunk if isinstance(chunk, pd.DataFrame) else pd.DataFrame(chunk))

    if not frames:
        raise ValueError("Tushare rt_k 全市场快照返回空表（检查 rt_k 权限与 token）")

    raw = pd.concat(frames, ignore_index=True)
    sym_src = _first_col(raw, ("ts_code", "TS_CODE"))
    if not sym_src:
        raise ValueError(
            "Tushare rt_k 缺少 ts_code 列，列预览: " + repr(list(raw.columns)[:25])
        )

    dup_col = raw[sym_src]
    raw = raw.loc[~dup_col.duplicated(keep="first")].reset_index(drop=True)

    name_src = _first_col(raw, ("name", "NAME"))
    pre_src = _first_col(raw, ("pre_close", "PRE_CLOSE"))
    close_src = _first_col(raw, ("close", "CLOSE"))
    amt_src = _first_col(raw, ("amount", "AMOUNT"))
    vol_src = _first_col(raw, ("vol", "VOL"))

    n = len(raw)
    codes = raw[sym_src].map(_code_from_ts_code).reset_index(drop=True)

    if name_src:
        names = raw[name_src].fillna("").map(lambda x: str(x).strip()).reset_index(drop=True)
    else:
        names = pd.Series([""] * n, dtype=object)

    if close_src and pre_src:
        c = pd.to_numeric(raw[close_src], errors="coerce").fillna(0.0)
        p = pd.to_numeric(raw[pre_src], errors="coerce").fillna(0.0)
        chg = (
            ((c - p).div(p.mask(p == 0)) * 100.0)
            .fillna(0.0)
            .replace([float("inf"), float("-inf")], 0.0)
            .reset_index(drop=True)
        )
    else:
        chg = pd.Series([0.0] * n, dtype=float)

    if amt_src:
        amt = pd.to_numeric(raw[amt_src], errors="coerce").fillna(0.0).reset_index(drop=True)
    else:
        amt = pd.Series([0.0] * n, dtype=float)

    out_dict: dict[str, Any] = {
        "代码": codes,
        "名称": names,
        "涨跌幅": chg,
        "成交额": amt,
    }

    if vol_src:
        out_dict["成交量"] = (
            pd.to_numeric(raw[vol_src], errors="coerce").fillna(0.0).reset_index(drop=True)
        )

    out = pd.DataFrame(out_dict)
    out = out[out["代码"].astype(str).str.len() > 0]
    if len(out) == 0:
        raise ValueError("Tushare rt_k 快照映射后无有效代码行")

    return out.reset_index(drop=True)
