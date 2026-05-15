"""打板选股 v2 文本输出。"""

from __future__ import annotations

from typing import Any

from .daban_pick_engine import DabanPickMode, mode_label


def _format_pct_chg(pct: Any) -> str:
    try:
        return f"{float(pct):+.2f}%" if pct is not None else "-"
    except (TypeError, ValueError):
        return "-"


def _fmt_fd(fd: Any) -> str:
    try:
        v = float(fd or 0)
        if v >= 1e8:
            return f"{v / 1e8:.2f}亿"
        if v >= 1e4:
            return f"{v / 1e4:.0f}万"
        return f"{v:.0f}"
    except (TypeError, ValueError):
        return "-"


def format_daban_pick_v2(
    rows: list[dict[str, Any]],
    *,
    trade_date: str,
    mode: DabanPickMode,
    stats: dict[str, Any] | None = None,
    codes_tier: str = "A",
) -> str:
    """格式化打板选股结果。"""
    st = stats or {}
    n_limit = int(st.get("n_limit", 0))
    n_after_mode = int(st.get("n_after_mode", 0))
    n_scored = int(st.get("n_scored", 0))
    regime = str(st.get("market_regime", "-"))
    mode_s = mode_label(mode)

    if not rows:
        return (
            f"打板选股 {trade_date} [{mode_s}] 无结果\n"
            f"涨停池 {n_limit} 只 → 模式过滤 {n_after_mode} 只 → 评分 {n_scored} 只\n"
            f"盘面: {regime}"
        )

    lines = [
        f"打板选股 {trade_date} [{mode_s}] 盘面={regime}",
        f"涨停池{n_limit}→模式过滤{n_after_mode}→评分{n_scored}→输出{len(rows)}只",
        "排序: 综合分↓ | 分档 A=优先 B=观察",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for i, r in enumerate(rows, 1):
        pct_s = _format_pct_chg(r.get("pct_chg"))
        tier = r.get("tier", "B")
        score = r.get("score")
        score_s = f"{float(score):.2f}" if score is not None else "-"
        concept = r.get("concept_key") or "-"
        if len(str(concept)) > 10:
            concept = str(concept)[:9] + "…"
        sec_n = r.get("sector_limit_count", 0)
        lines.append(
            f"{i:2}. [{tier}] {r.get('ts_code')} {r.get('name')} "
            f"分={score_s} {pct_s} "
            f"连板={r.get('nums')} 开板={r.get('open_times')} "
            f"封单={_fmt_fd(r.get('fd_amount'))} "
            f"主力={r.get('net_amount_wan')}万 "
            f"{concept}({sec_n})"
        )

    if codes_tier.upper() == "A":
        code_rows = [r for r in rows if str(r.get("tier", "")).upper() == "A"]
    else:
        code_rows = rows
    codes = " ".join(str(r.get("ts_code") or "") for r in code_rows)
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"代码({codes_tier}档): {codes or '(无)'}")
    return "\n".join(lines)
