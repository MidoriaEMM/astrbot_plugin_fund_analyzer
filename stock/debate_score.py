"""
辩论结论 → 仓位用 score∈[0,1] 映射（规则固定，便于复现与日志）。
"""

from __future__ import annotations

from .debate_engine import DebateResult


def debate_result_score_01(result: DebateResult) -> float:
    """
    - 看涨：信心度 / 100，截断到 [0, 1]
    - 看跌 / 中性：0（做多仓位计划中剔除或不分配）
    """
    if result.final_direction == "看涨":
        return max(0.0, min(1.0, float(result.confidence) / 100.0))
    return 0.0
