"""打板选股评分引擎：涨停池 enrich + 分位数评分 + 模式过滤。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .daban_sector import (
    build_concept_groups,
    extract_concept_key,
    sector_limit_count_for,
)
from .limit_up_fetch import (
    fetch_limit_list_d,
    fetch_limit_list_ths,
    fetch_limit_step,
    fetch_moneyflow_dc_trade_date,
    fetch_top_list,
)


class DabanPickMode(str, Enum):
    MIXED = "mixed"
    SHOUBAN = "shouban"
    RELAY = "relay"
    DRAGON = "dragon"


_MODE_LABELS = {
    DabanPickMode.MIXED: "综合",
    DabanPickMode.SHOUBAN: "首板",
    DabanPickMode.RELAY: "接力",
    DabanPickMode.DRAGON: "龙头",
}


def mode_label(mode: DabanPickMode | str) -> str:
    if isinstance(mode, DabanPickMode):
        return _MODE_LABELS.get(mode, "综合")
    try:
        return _MODE_LABELS[DabanPickMode(str(mode))]
    except ValueError:
        return str(mode)


def parse_daban_pick_mode(text: str) -> DabanPickMode | None:
    t = (text or "").strip()
    mapping = {
        "综合": DabanPickMode.MIXED,
        "首板": DabanPickMode.SHOUBAN,
        "接力": DabanPickMode.RELAY,
        "龙头": DabanPickMode.DRAGON,
        "mixed": DabanPickMode.MIXED,
        "shouban": DabanPickMode.SHOUBAN,
        "relay": DabanPickMode.RELAY,
        "dragon": DabanPickMode.DRAGON,
    }
    return mapping.get(t)


# 因子权重：封板 / 资金 / 连板 / 板块 / 龙虎榜 / THS
_WEIGHTS: dict[DabanPickMode, tuple[float, float, float, float, float, float]] = {
    DabanPickMode.MIXED: (0.25, 0.25, 0.15, 0.20, 0.10, 0.05),
    DabanPickMode.SHOUBAN: (0.40, 0.20, 0.05, 0.25, 0.05, 0.05),
    DabanPickMode.RELAY: (0.20, 0.25, 0.35, 0.10, 0.05, 0.05),
    DabanPickMode.DRAGON: (0.15, 0.15, 0.20, 0.35, 0.10, 0.05),
}

SCORE_MIN = 0.35
TIER_A_MIN = 0.65
TIER_B_MIN = 0.50
WEAK_REGIME_TOP_CAP = 8
STRONG_LIMIT_COUNT = 80
MID_LIMIT_COUNT = 40


@dataclass
class DabanPickConfig:
    top_n: int = 15
    mode: DabanPickMode = DabanPickMode.MIXED
    exclude_st: bool = True
    exclude_bj: bool = False
    want_ths: bool = True
    want_step: bool = True
    want_top_list: bool = True
    score_min: float = SCORE_MIN
    tier_a_min: float = TIER_A_MIN
    tier_b_min: float = TIER_B_MIN
    default_nums_when_missing: int = 1
    shouban_max_open_times: int = 2
    dragon_min_sector_count: int = 2


def _lc_map(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _pick(row: dict[str, Any], *names: str, default: Any = None) -> Any:
    m = _lc_map(row)
    for n in names:
        v = m.get(n.lower())
        if v is not None and v != "":
            return v
    return default


def _to_int(x: Any, default: int = 0) -> int:
    try:
        if x is None or x == "":
            return default
        return int(float(x))
    except (TypeError, ValueError):
        return default


def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        v = float(x)
        if v != v:
            return default
        return v
    except (TypeError, ValueError):
        return default


def _is_st_name(name: str) -> bool:
    return bool(re.search(r"\bST\b", name, flags=re.I)) or "ST" in name.upper()


def _dc_tier_sum_wan(fl_row: dict[str, Any]) -> float:
    return sum(
        _to_float(_pick(fl_row, k), 0.0)
        for k in (
            "buy_elg_amount",
            "buy_lg_amount",
            "buy_md_amount",
            "buy_sm_amount",
        )
    )


def _first_by_ts_code(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        code = str(_pick(r, "ts_code") or "").strip().upper()
        if code and code not in by:
            by[code] = r
    return by


def _agg_top_list_net_by_code(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, float] = {}
    for r in rows:
        code = str(_pick(r, "ts_code") or "").strip().upper()
        if not code:
            continue
        by[code] = by.get(code, 0.0) + _to_float(_pick(r, "net_amount"), 0.0)
    return {c: {"lhb_net_amount": v} for c, v in by.items()}


def _first_time_minutes(raw: Any) -> float | None:
    """首封时间越早分数越高；无法解析返回 None。"""
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    parts = s.replace(":", " ").split()
    try:
        if len(parts) >= 2:
            h, m = int(parts[0]), int(parts[1])
            sec = int(parts[2]) if len(parts) > 2 else 0
            return h * 60 + m + sec / 60.0
        if len(s) >= 4 and s.isdigit():
            h, m = int(s[:2]), int(s[2:4])
            return h * 60 + m
    except (TypeError, ValueError):
        pass
    return None


def percentile_ranks(values: list[float | None]) -> list[float]:
    """0~1 分位；None 视为最低。"""
    n = len(values)
    if n == 0:
        return []
    filled = [v if v is not None else float("-inf") for v in values]
    order = sorted(range(n), key=lambda i: filled[i])
    out = [0.0] * n
    for rank, idx in enumerate(order):
        out[idx] = rank / (n - 1) if n > 1 else 0.5
    return out


def inverse_percentile_ranks(values: list[float | None]) -> list[float]:
    """越小越好（如开板次数）。"""
    n = len(values)
    if n == 0:
        return []
    filled = [v if v is not None else float("inf") for v in values]
    order = sorted(range(n), key=lambda i: filled[i])
    out = [0.0] * n
    for rank, idx in enumerate(order):
        out[idx] = 1.0 - (rank / (n - 1) if n > 1 else 0.5)
    return out


def _market_regime_from_limit_count(n_limit: int) -> str:
    if n_limit >= STRONG_LIMIT_COUNT:
        return "强"
    if n_limit >= MID_LIMIT_COUNT:
        return "中"
    return "弱"


def passes_mode_filter(row: dict[str, Any], mode: DabanPickMode, cfg: DabanPickConfig) -> bool:
    nums = _to_int(row.get("nums"), cfg.default_nums_when_missing)
    open_times = _to_int(row.get("open_times"), 0)
    sec = _to_int(row.get("sector_limit_count"), 0)
    if mode == DabanPickMode.SHOUBAN:
        if nums > 1:
            return False
        if open_times > cfg.shouban_max_open_times:
            return False
    elif mode == DabanPickMode.RELAY:
        if nums < 2:
            return False
    elif mode == DabanPickMode.DRAGON:
        if sec < cfg.dragon_min_sector_count:
            return False
    return True


def score_candidates(
    rows: list[dict[str, Any]],
    mode: DabanPickMode,
    *,
    has_ths: bool = True,
) -> list[dict[str, Any]]:
    """对已通过模式过滤的 rows 写入 rank_* 与 score、tier。"""
    if not rows:
        return []

    n = len(rows)
    fd_vals = [_to_float(r.get("fd_amount"), 0.0) for r in rows]
    open_vals = [_to_float(r.get("open_times"), 0.0) for r in rows]
    ft_vals = [_first_time_minutes(r.get("first_time")) for r in rows]
    net_vals = [_to_float(r.get("net_amount_wan"), 0.0) for r in rows]
    rate_vals = [_to_float(r.get("net_amount_rate"), 0.0) for r in rows]
    elg_vals = [_to_float(r.get("buy_elg_wan"), 0.0) for r in rows]
    nums_vals = [_to_float(r.get("nums"), 1.0) for r in rows]
    sec_vals = [_to_float(r.get("sector_limit_count"), 0.0) for r in rows]
    lhb_vals = [_to_float(r.get("lhb_net_amount"), 0.0) for r in rows]
    ths_vals = [_to_float(r.get("ths_limit_up_suc_rate"), 50.0) for r in rows]

    r_fd = percentile_ranks(fd_vals)
    r_open = inverse_percentile_ranks(open_vals)
    r_ft = inverse_percentile_ranks(ft_vals)
    r_net = percentile_ranks(net_vals)
    r_rate = percentile_ranks(rate_vals)
    r_elg = percentile_ranks(elg_vals)
    r_nums = percentile_ranks(nums_vals)
    r_sec = percentile_ranks(sec_vals) if has_ths else [0.5] * n
    r_lhb = percentile_ranks(lhb_vals)
    r_ths = percentile_ranks(ths_vals) if has_ths else [0.5] * n

    w_seal, w_flow, w_nums, w_sec, w_lhb, w_ths = _WEIGHTS[mode]

    scored: list[dict[str, Any]] = []
    for i, r in enumerate(rows):
        seal = (r_fd[i] + r_open[i] + r_ft[i]) / 3.0
        flow = (r_net[i] + r_rate[i] + r_elg[i]) / 3.0
        lhb = r_lhb[i] if r.get("lhb_net_amount") is not None else 0.5
        total = (
            w_seal * seal
            + w_flow * flow
            + w_nums * r_nums[i]
            + w_sec * r_sec[i]
            + w_lhb * lhb
            + w_ths * r_ths[i]
        )
        out = dict(r)
        out["score"] = round(total, 4)
        out["_seal"] = seal
        out["_flow"] = flow
        scored.append(out)
    return scored


def apply_dragon_dedup(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每概念保留综合分最高 1 只。"""
    by_concept: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda x: -float(x.get("score") or 0)):
        ck = str(r.get("concept_key") or "未分类")
        if ck not in by_concept:
            by_concept[ck] = r
    return sorted(by_concept.values(), key=lambda x: -float(x.get("score") or 0))


def assign_tiers(
    rows: list[dict[str, Any]],
    *,
    tier_a_min: float = TIER_A_MIN,
    tier_b_min: float = TIER_B_MIN,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        s = float(r.get("score") or 0)
        tier = "A" if s >= tier_a_min else ("B" if s >= tier_b_min else "C")
        row = dict(r)
        row["tier"] = tier
        out.append(row)
    return out


def enrich_limit_pool(
    limit_up_rows: list[dict[str, Any]],
    moneyflow_rows: list[dict[str, Any]],
    *,
    ths_rows: list[dict[str, Any]] | None = None,
    step_rows: list[dict[str, Any]] | None = None,
    top_list_rows: list[dict[str, Any]] | None = None,
    cfg: DabanPickConfig | None = None,
) -> list[dict[str, Any]]:
    """涨停池 → 候选特征行（未评分）。"""
    cfg = cfg or DabanPickConfig()
    flow_by = _first_by_ts_code(moneyflow_rows)
    ths_by = _first_by_ts_code(ths_rows or [])
    nums_by: dict[str, int] = {}
    for r in step_rows or []:
        code = str(_pick(r, "ts_code") or "").strip().upper()
        if code:
            nums_by[code] = max(nums_by.get(code, 0), _to_int(_pick(r, "nums"), 0))
    lhb_by = _agg_top_list_net_by_code(top_list_rows or [])

    base: list[dict[str, Any]] = []
    for lu in limit_up_rows:
        code = str(_pick(lu, "ts_code") or "").strip().upper()
        if not code:
            continue
        name = str(_pick(lu, "name") or "")
        if cfg.exclude_st and name and _is_st_name(name):
            continue
        if cfg.exclude_bj and code.endswith(".BJ"):
            continue

        fl = flow_by.get(code, {})
        net_wan = _to_float(_pick(fl, "net_amount"), 0.0)
        nums = nums_by.get(code)
        if nums is None or nums <= 0:
            nums = cfg.default_nums_when_missing

        th = ths_by.get(code, {})
        concept = extract_concept_key(
            ths_lu_desc=_pick(th, "lu_desc"),
            ths_tag=_pick(th, "tag"),
            name=name,
        )

        row: dict[str, Any] = {
            "ts_code": code,
            "name": name,
            "nums": nums,
            "net_amount_wan": round(net_wan, 2),
            "tier_sum_wan": round(_dc_tier_sum_wan(fl) if fl else 0.0, 2),
            "buy_elg_wan": round(_to_float(_pick(fl, "buy_elg_amount"), 0.0), 2),
            "net_amount_rate": _pick(fl, "net_amount_rate"),
            "open_times": _to_int(_pick(lu, "open_times"), 0),
            "fd_amount": _to_float(_pick(lu, "fd_amount"), 0.0),
            "limit_times": _to_int(_pick(lu, "limit_times"), 0),
            "first_time": _pick(lu, "first_time"),
            "last_time": _pick(lu, "last_time"),
            "pct_chg": _pick(lu, "pct_chg", "pct_change"),
            "concept_key": concept,
            "ths_lu_desc": _pick(th, "lu_desc"),
            "ths_limit_up_suc_rate": _pick(th, "limit_up_suc_rate"),
        }
        lh = lhb_by.get(code)
        if lh:
            row["lhb_net_amount"] = round(_to_float(lh.get("lhb_net_amount"), 0.0), 2)
        base.append(row)

    groups = build_concept_groups(base)
    for row in base:
        ck = str(row.get("concept_key") or "未分类")
        row["sector_limit_count"] = sector_limit_count_for(ck, groups)
    return base


def run_daban_pick_pipeline(
    candidates: list[dict[str, Any]],
    cfg: DabanPickConfig,
    *,
    n_limit: int,
    has_ths: bool = True,
    stats_out: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """评分、过滤、分档、截取 top_n。"""
    regime = _market_regime_from_limit_count(n_limit)
    effective_top = cfg.top_n
    if regime == "弱":
        effective_top = min(effective_top, WEAK_REGIME_TOP_CAP)

    filtered = [r for r in candidates if passes_mode_filter(r, cfg.mode, cfg)]
    n_after_mode = len(filtered)

    scored = score_candidates(filtered, cfg.mode, has_ths=has_ths)
    scored = [r for r in scored if float(r.get("score") or 0) >= cfg.score_min]
    n_scored = len(scored)

    if cfg.mode == DabanPickMode.DRAGON:
        scored = apply_dragon_dedup(scored)

    scored.sort(key=lambda x: -float(x.get("score") or 0))
    tiered = assign_tiers(scored, tier_a_min=cfg.tier_a_min, tier_b_min=cfg.tier_b_min)
    # 输出 A+B，C 档剔除展示
    out = [r for r in tiered if r.get("tier") in ("A", "B")][:effective_top]

    if stats_out is not None:
        stats_out.update(
            {
                "n_limit": n_limit,
                "n_after_mode": n_after_mode,
                "n_scored": n_scored,
                "market_regime": regime,
                "mode": cfg.mode.value,
                "n_a": sum(1 for r in out if r.get("tier") == "A"),
                "n_b": sum(1 for r in out if r.get("tier") == "B"),
            }
        )
    return out


def fetch_daban_pick(
    api_key: str | None,
    trade_date: str,
    config: DabanPickConfig | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    拉数 + 评分选股。

    Returns:
        (rows, stats)
    """
    cfg = config or DabanPickConfig()
    td = trade_date.replace("-", "")[:8]

    lu = fetch_limit_list_d(api_key, td, limit_type="U")
    mf = fetch_moneyflow_dc_trade_date(api_key, td)
    ths = (
        fetch_limit_list_ths(api_key, td, limit_type="涨停池")
        if cfg.want_ths
        else []
    )
    st = fetch_limit_step(api_key, td) if cfg.want_step else []
    lhb = fetch_top_list(api_key, td) if cfg.want_top_list else []

    candidates = enrich_limit_pool(
        lu,
        mf,
        ths_rows=ths or None,
        step_rows=st or None,
        top_list_rows=lhb or None,
        cfg=cfg,
    )
    stats: dict[str, Any] = {}
    rows = run_daban_pick_pipeline(
        candidates,
        cfg,
        n_limit=len(lu),
        has_ths=bool(ths),
        stats_out=stats,
    )
    stats["n_mf"] = len(mf)
    return rows, stats
