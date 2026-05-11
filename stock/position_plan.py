"""
基于辩论 score 与 ATR 止损距离的整手仓位演示（不构成投资建议）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PositionPlanRow:
    code: str
    name: str
    score: float
    shares: int
    price: float
    notional: float
    stop_dist: float
    atr14: float
    avg_amount_5d_yi: float | None
    completed_at: str | None
    last_bar_date: str


@dataclass
class PositionPlanResult:
    rows: list[PositionPlanRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    total_notional: float = 0.0


def _floor_hundred_shares(notional: float, price: float) -> int:
    if price <= 0:
        return 0
    raw = notional / price
    return int(raw // 100) * 100


def build_position_plan(
    principal: float,
    risk_fraction: float,
    k_atr: float,
    *,
    min_score_01: float,
    min_avg_amount_yi: float,
    single_cap_fraction: float,
    max_positions: int | None,
    snapshots: list[dict[str, Any]],
) -> PositionPlanResult:
    """
    仅做多：direction==看涨、score≥阈值、ATR 有效；可选近 5 日均成交额下限（亿元）。
    风险预算按 score 加权分配；止损距离 = k_atr * ATR14（价格单位）；股数为 100 整数倍。
    先做单票名义本金封顶，再若总和超本金则按比例缩减份额（固定策略）。
    """
    result = PositionPlanResult()
    if principal <= 0:
        result.warnings.append("本金无效，跳过配比")
        return result

    candidates: list[dict[str, Any]] = []
    for snap in snapshots:
        code = str(snap.get("code", "")).strip()
        name = str(snap.get("name", "")).strip()
        direction = snap.get("direction", "")
        score = float(snap.get("score") or 0.0)
        price = float(snap.get("price") or 0.0)
        atr_raw = snap.get("atr14")
        atr14 = float(atr_raw) if atr_raw is not None else None
        avg_amt = snap.get("avg_amount_5d_yi")

        if direction != "看涨":
            result.warnings.append(f"{code} {name}：非看涨，不参与做多配比")
            continue
        if score < min_score_01:
            result.warnings.append(
                f"{code} {name}：score={score:.3f} < {min_score_01:.3f}，跳过"
            )
            continue
        if atr14 is None or atr14 <= 0:
            result.warnings.append(f"{code} {name}：ATR14 无效，跳过")
            continue
        if price <= 0:
            result.warnings.append(f"{code} {name}：现价无效，跳过")
            continue
        if min_avg_amount_yi > 0:
            if avg_amt is None:
                result.warnings.append(
                    f"{code} {name}：缺少成交额对齐数据，额均过滤跳过"
                )
                continue
            if float(avg_amt) < min_avg_amount_yi:
                result.warnings.append(
                    f"{code} {name}：额均={float(avg_amt):.4f}亿 < {min_avg_amount_yi}亿，跳过"
                )
                continue

        stop_dist = k_atr * atr14
        if stop_dist <= 0:
            result.warnings.append(f"{code} {name}：止损距离无效，跳过")
            continue

        candidates.append(
            {
                "code": code,
                "name": name,
                "score": score,
                "price": price,
                "atr14": atr14,
                "stop_dist": stop_dist,
                "avg_amount_5d_yi": float(avg_amt) if avg_amt is not None else None,
                "completed_at": snap.get("completed_at"),
                "last_bar_date": str(snap.get("last_bar_date") or ""),
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    if max_positions is not None:
        candidates = candidates[: max_positions]

    sum_scores = sum(c["score"] for c in candidates)
    if sum_scores <= 0:
        result.warnings.append("无满足条件的看涨标的或分数权重和为 0")
        return result

    risk_budget = principal * risk_fraction
    max_single_nv = principal * single_cap_fraction

    rows_raw: list[dict[str, Any]] = []
    for c in candidates:
        w = c["score"] / sum_scores
        risk_i = risk_budget * w
        stop_dist = c["stop_dist"]
        shares = int((risk_i / stop_dist) // 100) * 100

        price = c["price"]
        while shares > 0 and shares * price > max_single_nv:
            shares -= 100

        rows_raw.append({**c, "shares": shares})

    # 按比例缩减直到总市值不超过本金
    def total_nv(rs: list[dict[str, Any]]) -> float:
        return sum(r["shares"] * r["price"] for r in rs)

    nv = total_nv(rows_raw)
    for _ in range(12):
        if nv <= principal or nv <= 0:
            break
        factor = principal / nv
        new_raw: list[dict[str, Any]] = []
        for r in rows_raw:
            tgt_nv = r["shares"] * r["price"] * factor
            new_sh = _floor_hundred_shares(tgt_nv, r["price"])
            new_raw.append({**r, "shares": new_sh})
        rows_raw = new_raw
        nv = total_nv(rows_raw)

    if nv > principal:
        result.warnings.append(
            f"缩减后总市值仍约 {nv:.2f} 元（本金 {principal:.2f}），可能因整手取整导致略超"
        )

    out_rows: list[PositionPlanRow] = []
    for r in rows_raw:
        if r["shares"] <= 0:
            continue
        nv_i = r["shares"] * r["price"]
        out_rows.append(
            PositionPlanRow(
                code=r["code"],
                name=r["name"],
                score=float(r["score"]),
                shares=int(r["shares"]),
                price=float(r["price"]),
                notional=round(nv_i, 2),
                stop_dist=float(r["stop_dist"]),
                atr14=float(r["atr14"]),
                avg_amount_5d_yi=r.get("avg_amount_5d_yi"),
                completed_at=r.get("completed_at"),
                last_bar_date=str(r.get("last_bar_date") or ""),
            )
        )

    result.rows = out_rows
    result.total_notional = round(sum(x.notional for x in out_rows), 2)
    return result


def format_position_plan_table(plan: PositionPlanResult) -> str:
    if not plan.rows:
        return "（无配比输出）"
    lines: list[str] = ["【仓位概要】"]
    for r in plan.rows:
        if r.avg_amount_5d_yi is not None:
            avg_txt = f"{r.avg_amount_5d_yi:.4f}"
        else:
            avg_txt = "-"
        ct = r.completed_at or "-"
        lines.append(
            f"{r.code} {r.name} score={r.score:.3f} | {r.shares}股×{r.price:.4f}"
            f"≈{r.notional:.2f}元 | stop={r.stop_dist:.4f} ATR={r.atr14:.4f} | "
            f"额均{avg_txt}亿 | bar={r.last_bar_date or '-'} | {ct}"
        )
    lines.append(f"合计预估名义本金：{plan.total_notional:.2f}")
    return "\n".join(lines)
