"""辩论结论文本后附：规则化参考买入/止损价位（演示用，非投资建议）。"""

from __future__ import annotations

from typing import Any


def _f(x: Any) -> float | None:
    try:
        if x is None:
            return None
        v = float(x)
        if v != v or v == 0.0:
            return None
        return v
    except (TypeError, ValueError):
        return None


def format_trade_levels_lines(
    *,
    direction: str,
    latest_price: float,
    alignment: dict[str, Any] | None,
    k_atr: float = 2.0,
) -> list[str]:
    """
    返回 1～2 行纯文本；无可用价位时返回单行说明。

    alignment 键与 DailyAlignmentMetrics.as_alignment_dict 一致：
    ref_close, atr14
    """
    ref = _f((alignment or {}).get("ref_close"))
    atr = _f((alignment or {}).get("atr14"))
    px = _f(latest_price) or ref
    if ref is None and px is not None:
        ref = px
    if px is None:
        return ["价位(演示): 行情不足，请自行设止损。"]

    lines: list[str] = []
    d = direction or "中性"
    ref_s = f"{ref:.3f}" if ref is not None else "-"
    px_s = f"{px:.3f}"

    if atr is not None and ref is not None and atr > 0:
        stop_demo = ref - k_atr * atr
        stop_s = f"{stop_demo:.3f}"
    else:
        stop_s = None

    if d == "看涨":
        lines.append(f"参考买入(演示): 现价 {px_s} 附近或略回踩昨收 {ref_s}。")
        if stop_s is not None:
            lines.append(f"止损(演示): {stop_s}（昨收-{k_atr:g}×ATR14）。")
        elif ref is not None:
            lines.append(f"止损(演示): 跌破昨收约 {ref * 0.95:.3f}（约-5%）或自行设定。")
        else:
            lines.append("止损(演示): 请自行设定。")
    elif d == "看跌":
        lines.append("参考(演示): 偏空时观望或减仓为主。")
        if stop_s is not None:
            lines.append(f"若持有多头，可参考止损 {stop_s}（昨收-{k_atr:g}×ATR14）。")
        elif ref is not None:
            lines.append(f"若持有多头，跌破昨收约 {ref * 0.95:.3f}（约-5%）可作纪律参考。")
        else:
            lines.append("若持有多头，请自行设止损。")
    else:
        lines.append("参考(演示): 中性观望，不追价。")
        if stop_s is not None:
            lines.append(f"若试错仓，止损(演示): {stop_s}。")
        elif ref is not None:
            lines.append(f"若试错仓，跌破昨收约 {ref * 0.95:.3f} 附近考虑止损。")
        else:
            lines.append("若试错仓，请自行设止损。")

    return lines
