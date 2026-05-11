"""Tushare token / SDK 初始化（对内使用）。"""

from __future__ import annotations

import os
from typing import Any

_sdk_cache: tuple[str | None, Any, Any] | None = None


def effective_token(api_key: str | None) -> str:
    """
    与 Tickflow 客户端一致的首参占位名 ``api_key``，此处填入 **Tushare token**；
    ``None`` 时使用环境变量 ``TUSHARE_TOKEN``。
    """
    key = api_key if api_key is not None else os.environ.get("TUSHARE_TOKEN")
    key = (key or "").strip()
    if not key:
        raise ValueError(
            "需要传入 token 参数，或设置环境变量 TUSHARE_TOKEN（可参考 "
            "https://tushare.pro/document/2 初始化说明）"
        )
    return key


def prepare_sdk(api_key: str | None) -> tuple[Any, Any]:
    """
    ``import tushare`` → ``set_token`` → 返回 ``(ts_module, pro)``。
    同进程、同 token 串下复用 ``pro_api``。
    """
    global _sdk_cache

    tok = effective_token(api_key)
    if _sdk_cache is not None and _sdk_cache[0] == tok:
        return _sdk_cache[1], _sdk_cache[2]

    import tushare as ts

    ts.set_token(tok)
    pro = ts.pro_api()
    _sdk_cache = (tok, ts, pro)
    return ts, pro
