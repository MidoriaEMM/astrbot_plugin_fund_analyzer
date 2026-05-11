"""
威科夫启发式短线评分：大盘水温 + 阶段/触发/量价/均线/赔率/仓位建议。
与「短线选股」共用候选池与日 K 数据源，打分逻辑独立；不构成投资建议。
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from astrbot.api import logger

from ..quant_screening import (
    DEFAULT_SCREENING_CONCURRENCY,
    DISCLAIMER,
    HISTORY_DAYS,
    KLINE_MAX_RETRIES_SCREENING,
    SCREENING_JITTER_ENABLED,
    SCREENING_JITTER_SEC,
)
from .short_term import (
    SHORT_TERM_BATCH_MAX_CODES,
    _pairs_for_short_term,
    parse_stock_codes_from_text,
)

WYCKOFF_MIN_BARS = 52  # MA50 + 缓冲

# 七维满分（与用户权重一致）
W_MARKET, W_PHASE, W_TRIG, W_VP, W_MA, W_RR, W_POS = 15, 20, 20, 20, 10, 10, 5


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


def _sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _mean(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


@dataclass
class MarketRegimeSnapshot:
    """上证指数得出的盘面档位（单次指令内共用）。"""

    score_15: float  # 0~15，已落在满分标尺上
    label: str
    attitude: str
    notes: list[str] = field(default_factory=list)
    index_ok: bool = False
    # A 股涨跌广度（Tushare rt_k / Tickflow CN_Equity_A）；未取到则为 None
    breadth: Optional[dict[str, Any]] = None
    breadth_ok: bool = False


def _evaluate_market_regime_from_index(index_hist: list[dict]) -> MarketRegimeSnapshot:
    if not index_hist or len(index_hist) < 22:
        return MarketRegimeSnapshot(
            score_15=7.5,
            label="未知",
            attitude="上证指数数据不足，大盘维度按中性分计",
            notes=["无法取得足够上证指数 K 线"],
            index_ok=False,
        )

    closes = [_safe_float(x.get("close")) for x in index_hist]
    highs = [_safe_float(x.get("high"), c) for x, c in zip(index_hist, closes)]
    lows = [_safe_float(x.get("low"), c) for x, c in zip(index_hist, closes)]
    vols = [_safe_float(x.get("volume")) for x in index_hist]

    if closes[-1] <= 0 or closes[-6] <= 0:
        return MarketRegimeSnapshot(
            score_15=7.5,
            label="未知",
            attitude="指数价位异常，大盘维度按中性分计",
            notes=[],
            index_ok=False,
        )

    ret5 = (closes[-1] / closes[-6] - 1.0) * 100.0
    ret10 = (closes[-1] / closes[-11] - 1.0) * 100.0 if len(closes) > 11 else ret5

    vm5 = _sma(vols, 5) or 0.0
    vm20 = _sma(vols, 20) or 0.0
    vol_r = vm5 / vm20 if vm20 > 0 else 1.0

    high20 = max(highs[-21:-1]) if len(highs) > 21 else max(highs[:-1])
    dist_hi_pct = (high20 - closes[-1]) / high20 * 100.0 if high20 > 0 else 0.0

    ma10 = _sma(closes, 10)

    notes: list[str] = [
        f"上证近5日 {ret5:+.2f}% · 近10日 {ret10:+.2f}% · 量比5/20 {vol_r:.2f}",
    ]

    # 停手：放量下跌或中期急跌
    if ret5 <= -2.0 and vol_r >= 1.12:
        return MarketRegimeSnapshot(
            score_15=3.0,
            label="停手观望",
            attitude="指数偏弱且量能偏大，短线忌逆水行舟",
            notes=notes + ["规则：近5日显著下跌且量能放大"],
            index_ok=True,
        )
    if ret10 <= -4.0:
        return MarketRegimeSnapshot(
            score_15=2.5,
            label="停手观望",
            attitude="中期走弱，宜空仓或极小仓试错",
            notes=notes + ["规则：近10日跌幅过大"],
            index_ok=True,
        )

    # 高位放量滞涨
    if dist_hi_pct <= 1.8 and abs(ret5) <= 1.3 and vol_r >= 1.18:
        return MarketRegimeSnapshot(
            score_15=5.5,
            label="高位防派发",
            attitude="贴近前高且放量滞涨，降低仓位防派发",
            notes=notes + ["规则：贴近20日内高点、涨跌不大但量能偏大"],
            index_ok=True,
        )

    # 可进攻
    if ret5 >= 2.0:
        return MarketRegimeSnapshot(
            score_15=14.0,
            label="可进攻",
            attitude="短线允许进攻（仍需个股过关）",
            notes=notes + ["规则：近5日涨幅与动能较好"],
            index_ok=True,
        )
    if (
        ret5 >= 0.8
        and vol_r >= 1.0
        and ma10 is not None
        and closes[-1] > ma10
    ):
        return MarketRegimeSnapshot(
            score_15=13.0,
            label="可进攻",
            attitude="短线允许进攻",
            notes=notes + ["规则：重心在10日线上且量能不弱"],
            index_ok=True,
        )

    # 缩量震荡 / 轻仓
    if vol_r <= 0.93 or abs(ret5) <= 1.1:
        return MarketRegimeSnapshot(
            score_15=9.5,
            label="轻仓试探",
            attitude="震荡环境宜小仓试错",
            notes=notes + ["规则：量能偏弱或涨跌平淡"],
            index_ok=True,
        )

    return MarketRegimeSnapshot(
        score_15=9.0,
        label="轻仓试探",
        attitude="一致性一般，控制仓位",
        notes=notes,
        index_ok=True,
    )


_BREADTH_MIN_VALID = 50


def _apply_breadth_to_regime(
    snap: MarketRegimeSnapshot,
    breadth: Optional[dict[str, Any]],
) -> MarketRegimeSnapshot:
    """在指数档位上叠加 A 股涨跌广度启发式修正。"""
    if not breadth:
        notes = list(snap.notes)
        notes.append("涨跌广度：未获取（无 Tushare/Tickflow 全市场快照或失败）")
        return replace(snap, notes=notes, breadth=None, breadth_ok=False)

    valid = int(breadth.get("valid") or 0)
    if valid < _BREADTH_MIN_VALID:
        notes = list(snap.notes)
        notes.append(
            f"涨跌广度：有效样本仅 {valid}，<{_BREADTH_MIN_VALID}，未参与修正"
        )
        return replace(snap, notes=notes, breadth=breadth, breadth_ok=False)

    adv = _safe_float(breadth.get("advance_ratio"), 0.5)
    decl = _safe_float(breadth.get("decline_ratio"), 0.5)
    up_c = int(breadth.get("up_count") or 0)
    down_c = int(breadth.get("down_count") or 0)
    src = str(breadth.get("source") or "?")

    score = float(snap.score_15)
    label = snap.label
    attitude = snap.attitude
    notes = list(snap.notes)
    notes.append(
        f"涨跌广度({src}): 涨{up_c} 跌{down_c} /{valid}，涨占比{adv * 100:.1f}%"
    )

    if (
        snap.label == "可进攻"
        and snap.score_15 >= 12.0
        and adv < 0.35
    ):
        score = max(0.0, score - 2.5)
        notes.append("规则：涨家偏少，指数与个股易分化，略降大盘分")
    elif snap.label == "可进攻" and adv < 0.38:
        score = max(0.0, score - 1.5)
        notes.append("规则：涨家占比一般，偏指数驱动")

    if (
        snap.label == "可进攻"
        and adv < 0.30
        and decl > 0.45
    ):
        score = max(0.0, score - 1.0)
        notes.append("规则：跌家明显更多，短线一致性弱")

    if (
        snap.index_ok
        and snap.label in ("可进攻", "轻仓试探")
        and adv >= 0.55
        and decl <= 0.40
    ):
        score = min(15.0, score + 1.0)
        notes.append("规则：个股普涨配合，略抬大盘分")

    if label == "停手观望" and adv >= 0.48:
        notes.append("提示：广度尚可，但指数规则仍判停手，请仍以指数仓位逻辑为主")

    return replace(
        snap,
        score_15=round(score, 2),
        label=label,
        attitude=attitude,
        notes=notes,
        breadth=breadth,
        breadth_ok=True,
    )


def evaluate_market_regime(
    index_hist: list[dict],
    breadth: Optional[dict[str, Any]] = None,
) -> MarketRegimeSnapshot:
    snap = _evaluate_market_regime_from_index(index_hist)
    return _apply_breadth_to_regime(snap, breadth)


def _price_position_20d(highs: list[float], lows: list[float], closes: list[float]) -> float:
    if len(closes) < 21:
        return 0.5
    hi = max(highs[-21:-1])
    lo = min(lows[-21:-1])
    if hi <= lo:
        return 0.5
    return (closes[-1] - lo) / (hi - lo)


def _atr_ratio(highs: list[float], lows: list[float], closes: list[float]) -> float:
    if len(closes) < 22:
        return 1.0
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    a5 = sum(trs[-5:]) / 5.0
    a20 = sum(trs[-20:]) / 20.0
    return a5 / a20 if a20 > 0 else 1.0


def _detect_phase(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
) -> tuple[str, float, list[str]]:
    reasons: list[str] = []
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma50 = _sma(closes, 50)
    if ma5 is None or ma10 is None or ma20 is None:
        return "数据不足", 6.0, []

    pos = _price_position_20d(highs, lows, closes)
    vol_r = (_sma(volumes, 5) or 0.0) / (_sma(volumes, 20) or 1.0)
    ret5 = (closes[-1] / closes[-6] - 1.0) * 100.0 if len(closes) > 5 and closes[-6] > 0 else 0.0

    # Markdown：弱势下行
    if (
        closes[-1] < ma20
        and ma10 < ma20
        and len(closes) > 15
        and closes[-1] < _mean(closes[-15:])
    ):
        reasons.append("收盘低于 MA20 且短期均线空头")
        return "近似Markdown（弱势）", 5.0, reasons

    # 派发：高位 + 量大 + 上攻放缓
    if pos >= 0.82 and vol_r >= 1.15 and ret5 < 2.5:
        reasons.append("20日区间高位 + 量能偏大 + 涨幅收敛")
        return "近似派发（谨慎）", 8.0, reasons

    # Markup
    if ma5 > ma10 > ma20 and closes[-1] > ma20:
        sc = 17.0
        reasons.append("MA5>MA10>MA20 且站稳 MA20")
        if ma50 is not None and closes[-1] > ma50:
            sc += 1.5
            reasons.append("亦站稳 MA50")
        return "近似Markup（拉升）", min(W_PHASE, sc), reasons

    # 吸筹末期：低位 + 波动压缩
    atr_comp = _atr_ratio(highs, lows, closes)
    if pos <= 0.42 and atr_comp <= 0.78:
        reasons.append("区间偏低 + ATR 压缩（蓄势）")
        return "近似吸筹末期", 14.0, reasons

    # 默认震荡蓄力
    reasons.append("未见清晰单边结构")
    return "震荡/不明", 10.0, reasons


def _upper_shadow_pct(o: float, h: float, c: float, pc: float) -> float:
    if pc <= 0:
        return 0.0
    body_top = max(o, c)
    return (h - body_top) / pc * 100.0


def _detect_triggers(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    change_rates: list[float],
) -> tuple[str, float, list[str]]:
    """返回主触发名、得分(0~W_TRIG)、要点。"""
    notes: list[str] = []
    n = len(closes)
    if n < 25:
        return "未识别", 5.0, ["K 线不足以识别 Spring/SOS"]

    candidates: list[tuple[float, str, str]] = []

    prior_low = min(lows[-22:-3])
    # Spring：假跌破后收回（近两日中任一）
    sprung = False
    for k in (-2, -1):
        if lows[k] < prior_low * 0.997 and closes[k] > prior_low:
            sprung = True
            break
    if sprung:
        candidates.append((18.0, "Spring", "假跌破平台后收回（近两日）"))
        notes.append("Spring：假跌破后收复支撑")

    # SOS：放量突破前高
    window_hi = max(highs[-22:-1])
    vma5_excl = _sma(volumes[:-1], 5) or volumes[-2]
    if closes[-1] > window_hi * 0.998 and volumes[-1] >= vma5_excl * 1.45:
        candidates.append((16.5, "SOS", "放量突破近20日前高"))
        notes.append("SOS：放量突破阻力")

    # LPS：此前有突破后缩量回踩
    brk_idx: Optional[int] = None
    for j in range(n - 14, n - 1):
        if j < 20:
            continue
        wh_before = max(highs[max(0, j - 20) : j])
        if closes[j] > wh_before * 1.001:
            brk_idx = j
            break
    if brk_idx is not None:
        ma10 = _sma(closes, 10)
        vbrk = volumes[brk_idx]
        avg_pull = _mean(volumes[-3:])
        if (
            ma10 is not None
            and closes[-1] >= ma10 * 0.985
            and closes[-1] >= lows[brk_idx] * 0.99
            and avg_pull < max(vbrk, 1.0) * 0.82
        ):
            candidates.append((19.0, "LPS", "突破后缩量回踩 MA10/支撑"))
            notes.append("LPS：突破后缩量回抽")

    # EVR：放量但跌幅有限（承接）
    vm5 = _sma(volumes, 5) or 1.0
    if volumes[-1] >= vm5 * 1.35 and change_rates[-1] >= -0.7:
        candidates.append((14.0, "EVR", "放量承接（跌幅有限）"))
        notes.append("EVR：努力有结果——放量未深跌")

    # 放量突破平台（非 SOS 强度）
    if (
        closes[-1] > window_hi * 0.995
        and volumes[-1] >= vm5 * 1.2
        and not any(c[1] == "SOS" for c in candidates)
    ):
        candidates.append((12.0, "放量突破平台", "盘中突破平台伴随放量"))

    # 缩量回踩
    if (_sma(volumes, 5) or 0) < (_sma(volumes, 20) or 1) * 0.88 and change_rates[-1] <= 0.8:
        ma10 = _sma(closes, 10)
        if ma10 is not None and lows[-1] <= ma10 * 1.02 and closes[-1] >= ma10 * 0.99:
            candidates.append((11.5, "缩量回踩支撑", "缩量回踩 MA10 一带"))

    # 危险：连续放量长上影（派发嫌疑）
    dangerous = False
    pc = closes[-2]
    sh = _upper_shadow_pct(opens[-1], highs[-1], closes[-1], pc or closes[-1])
    if sh >= 2.5 and volumes[-1] >= vm5 * 1.3:
        dangerous = True
        notes.append("放量长上影：警惕派发")

    if not candidates:
        base = 4.0 if dangerous else 6.0
        return ("危险量价" if dangerous else "无明确触发"), base, notes

    candidates.sort(key=lambda x: -x[0])
    name = candidates[0][1]
    sc = candidates[0][0]
    if dangerous and name not in ("Spring", "LPS"):
        sc = max(4.0, sc - 6.0)
        notes.append("叠加放量长上影扣分")
    notes.insert(0, candidates[0][2])
    return name, min(W_TRIG, sc), notes


def detect_primary_trigger_from_history(history: list[dict]) -> tuple[str, list[str]]:
    """
    供「短线选股」加触发开关：与威科夫选股同源规则，仅返回主触发名与要点。
    """
    if not history or len(history) < 25:
        return "未识别", ["K线不足25根"]
    closes = [_safe_float(h.get("close")) for h in history]
    opens = [_safe_float(h.get("open")) for h in history]
    highs = [_safe_float(h.get("high"), c) for h, c in zip(history, closes)]
    lows = [_safe_float(h.get("low"), c) for h, c in zip(history, closes)]
    vols = [_safe_float(h.get("volume")) for h in history]
    chg = [_safe_float(h.get("change_rate")) for h in history]
    if closes[-1] <= 0:
        return "未识别", []
    name, _sc, notes = _detect_triggers(opens, highs, lows, closes, vols, chg)
    return name, notes


def trigger_score_bonus(trigger_name: str) -> float:
    """短线综合分上的小额加减（先大盘乘数后叠加）。"""
    return {
        "LPS": 4.0,
        "Spring": 3.5,
        "SOS": 3.0,
        "EVR": 2.5,
        "放量突破平台": 2.0,
        "缩量回踩支撑": 2.0,
        "无明确触发": 0.0,
        "未识别": 0.0,
        "危险量价": -3.0,
    }.get(trigger_name, 0.0)


def _score_volume_price(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    change_rates: list[float],
) -> tuple[float, list[str], list[str]]:
    rs: list[str] = []
    rk: list[str] = []
    if len(closes) < 10:
        return 10.0, rs, rk

    vm5 = _sma(volumes, 5) or 1.0
    up_vol = 0
    dn_sh = 0
    for i in range(-5, 0):
        if abs(i) > len(closes):
            break
        idx = len(closes) + i
        if idx <= 0:
            continue
        pc = closes[idx - 1]
        if closes[idx] > pc and volumes[idx] > vm5 * 0.9:
            up_vol += 1
        if closes[idx] < pc and volumes[idx] < vm5 * 0.85:
            dn_sh += 1

    score = 10.0
    if up_vol >= 3:
        score += 6.0
        rs.append("多日上涨放量（需求占优）")
    elif up_vol >= 2:
        score += 3.5
        rs.append("部分交易日上涨放量")

    if dn_sh >= 2:
        score += 4.0
        rs.append("下跌缩量（供应减弱）")

    # 努力无结果
    if volumes[-1] >= vm5 * 1.35 and abs(change_rates[-1]) < 0.6:
        score -= 7.0
        rk.append("放量滞涨：量大价不动")

    pc = closes[-2] if len(closes) > 1 else closes[-1]
    sh = _upper_shadow_pct(opens[-1], highs[-1], closes[-1], pc)
    if sh >= 3.0 and volumes[-1] >= vm5 * 1.15:
        score -= 6.0
        rk.append(f"长上影 {sh:.1f}% + 放量")

    # 跌时量大、涨时量小（近5日）
    bad = 0
    for i in range(-5, 0):
        idx = len(closes) + i
        if idx < 1:
            continue
        pc = closes[idx - 1]
        if closes[idx] < pc and volumes[idx] > vm5 * 1.1:
            bad += 1
        if closes[idx] > pc and volumes[idx] < vm5 * 0.75:
            bad += 1
    if bad >= 3:
        score -= 5.0
        rk.append("跌放量/涨缩量节奏不健康")

    score = max(0.0, min(W_VP, score))
    return score, rs, rk


def _score_ma(
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> tuple[float, list[str], list[str]]:
    rs: list[str] = []
    rk: list[str] = []
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    ma50 = _sma(closes, 50)
    c = closes[-1]
    score = 0.0

    if ma5 and ma10 and c > ma5 and c > ma10:
        score += 3.5
        rs.append("站在 MA5/MA10 上方")
    elif ma5 and c < ma5:
        rk.append("跌破 MA5")
        score -= 2.0

    if ma5 and ma10 and ma20 and ma5 > ma10 > ma20:
        score += 4.0
        rs.append("MA5>MA10>MA20 短线多头")

    if ma20 and c > ma20:
        score += 1.5
        rs.append("站上 MA20")

    if ma50:
        ma50_prev = _sma(closes[:-5], 50)
        if ma50_prev and ma50 > ma50_prev * 1.002:
            score += 2.0
            rs.append("MA50 抬头（中期支撑）")
        if c > ma50:
            score += 1.0

    # 远离均线不追
    if ma20 and ma20 > 0:
        dev = abs(c - ma20) / ma20 * 100.0
        if dev >= 8.0:
            score -= 3.0
            rk.append(f"离 MA20 过远({dev:.1f}%)，不宜追高")

    score = max(0.0, min(W_MA, score + 1.0))  # 抬高基准避免全零
    return score, rs, rk


def _estimate_rr(
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> tuple[float, float, float, str]:
    """回报 risk_reward, stop_est, target_est, summary."""
    c = closes[-1]
    swing_low = min(lows[-12:-1])
    ma10 = _sma(closes, 10)
    stop = swing_low * 0.994
    if ma10 is not None:
        stop = min(stop, ma10 * 0.982)
    risk = c - stop
    target = max(highs[-22:-1]) if len(highs) > 22 else max(highs[:-1])
    if target <= c:
        target = c * 1.03
    reward = target - c
    if risk <= 0 or reward <= 0:
        return 0.0, stop, target, "止损或过近，赔率无效"
    rr = reward / risk
    return rr, stop, target, f"近似止损 {stop:.2f} · 目标 {target:.2f} · R:R≈{rr:.2f}:1"


def _score_rr(rr: float) -> tuple[float, list[str]]:
    if rr <= 0:
        return 1.0, ["结构止损无效或风险过大"]
    if rr >= 3.0:
        return 10.0, ["赔率理想（≥3:1）"]
    if rr >= 2.0:
        return 8.5, ["满足最低赔率（≥2:1）"]
    if rr >= 1.5:
        return 5.0, ["赔率一般"]
    return 2.5, ["赔率偏弱（<2:1），不符优选标准"]


def _score_position_dim(regime_label: str, trigger_name: str) -> tuple[float, str]:
    """仓位维度 0~5 分 + 文案。"""
    if regime_label == "停手观望":
        adv = "大盘停手档：空仓或≤10%试错"
        base = 1.5
    elif regime_label == "高位防派发":
        adv = "防派发：≤15%~20%，见好就收"
        base = 2.5
    elif regime_label == "可进攻":
        base = 4.0
        adv = "环境允许进攻，仍需个股确认"
    else:
        base = 3.0
        adv = "轻仓试错为主"

    if trigger_name == "LPS":
        adv += "｜LPS 确认后可 20%~30%"
        base = min(W_POS, base + 1.2)
    elif trigger_name == "Spring":
        adv += "｜Spring 试仓 10%~20%"
        base = min(W_POS, base + 0.8)
    elif trigger_name == "SOS":
        adv += "｜SOS 放量延续可视大盘加到 30%~40%"
        base = min(W_POS, base + 1.0)
    elif trigger_name == "EVR":
        adv += "｜EVR 承接试仓 15%~25%"
        base = min(W_POS, base + 0.6)
    elif trigger_name in ("无明确触发", "未识别"):
        adv += "｜无触发勿满仓"
        base = max(0.5, base - 1.0)
    elif trigger_name == "危险量价":
        adv = "信号矛盾：轻仓或空仓"
        base = 0.8

    if regime_label == "停手观望":
        base = min(base, 2.0)

    return min(W_POS, base), adv


@dataclass
class WyckoffLine:
    code: str
    name: str
    total: float
    score_market: float
    score_phase: float
    score_trigger: float
    score_vp: float
    score_ma: float
    score_rr: float
    score_position: float
    regime_label: str
    phase: str
    trigger: str
    rr_text: str
    rr_value: float
    position_advice: str
    reasons: list[str]
    risks: list[str]


def analyze_stock_wyckoff(
    history: list[dict],
    *,
    code: str,
    name: str,
    regime: MarketRegimeSnapshot,
) -> Optional[WyckoffLine]:
    if len(history) < WYCKOFF_MIN_BARS:
        return None

    closes = [_safe_float(h.get("close")) for h in history]
    opens = [_safe_float(h.get("open")) for h in history]
    highs = [_safe_float(h.get("high"), c) for h, c in zip(history, closes)]
    lows = [_safe_float(h.get("low"), c) for h, c in zip(history, closes)]
    vols = [_safe_float(h.get("volume")) for h in history]
    chg = [_safe_float(h.get("change_rate")) for h in history]

    if closes[-1] <= 0:
        return None

    phase_name, sc_phase, phase_notes = _detect_phase(closes, highs, lows, vols)
    trig_name, sc_trig, trig_notes = _detect_triggers(
        opens, highs, lows, closes, vols, chg
    )
    sc_vp, vp_rs, vp_rk = _score_volume_price(
        opens, highs, lows, closes, vols, chg
    )
    sc_ma, ma_rs, ma_rk = _score_ma(closes, highs, lows)

    rr, stop_p, tgt_p, rr_txt = _estimate_rr(closes, highs, lows)
    sc_rr, rr_notes = _score_rr(rr)
    sc_pos, pos_adv = _score_position_dim(regime.label, trig_name)

    sc_market = regime.score_15

    reasons = phase_notes + trig_notes + vp_rs + ma_rs + rr_notes + [rr_txt]
    risks = vp_rk + ma_rk

    total = (
        sc_market
        + sc_phase
        + sc_trig
        + sc_vp
        + sc_ma
        + sc_rr
        + sc_pos
    )
    total = round(max(0.0, min(100.0, total)), 1)

    return WyckoffLine(
        code=code,
        name=name,
        total=total,
        score_market=round(sc_market, 1),
        score_phase=round(sc_phase, 1),
        score_trigger=round(sc_trig, 1),
        score_vp=round(sc_vp, 1),
        score_ma=round(sc_ma, 1),
        score_rr=round(sc_rr, 1),
        score_position=round(sc_pos, 1),
        regime_label=regime.label,
        phase=phase_name,
        trigger=trig_name,
        rr_text=f"{rr_txt}（Stop≈{stop_p:.2f}）",
        rr_value=round(rr, 2),
        position_advice=pos_adv,
        reasons=reasons,
        risks=risks,
    )


async def fetch_market_regime_once(
    fund_analyzer: Any,
    *,
    days: int = HISTORY_DAYS,
) -> MarketRegimeSnapshot:
    hist, breadth = await asyncio.gather(
        fund_analyzer.get_sse_index_history(
            days,
            kline_max_retries=KLINE_MAX_RETRIES_SCREENING,
        ),
        fund_analyzer.get_cn_equity_a_breadth(),
    )
    if not hist:
        return evaluate_market_regime([], breadth)
    return evaluate_market_regime(hist, breadth)


def format_wyckoff_report(
    lines: list[WyckoffLine],
    regime: MarketRegimeSnapshot,
    *,
    title: str,
    candidate_count: int,
    valid_count: int,
    truncation_note: str = "",
    min_amount_yi: float = 0.0,
    batch_mode: bool = False,
) -> str:
    idx_note = "✓" if regime.index_ok else "⚠"
    br_note = "✓" if regime.breadth_ok else "⚠"
    header = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{idx_note} 大盘（上证启发式）: {regime.label} — {regime.attitude}\n"
        f"{br_note} 涨跌广度: "
        + (
            f"{regime.breadth.get('source', '?')} 涨占比 "
            f"{float(regime.breadth.get('advance_ratio') or 0) * 100:.1f}%"
            if regime.breadth_ok and regime.breadth
            else "未纳入或样本不足（见下列说明）"
        )
        + "\n"
        + (f"　• " + "\n　• ".join(regime.notes) + "\n" if regime.notes else "")
        + "⚠ 阶段/触发均为算法近似，非人工图谱；不构成投资建议。\n"
    )
    if batch_mode:
        header += f"指定标的: {candidate_count} 只 · 有效: {valid_count} 只\n"
    else:
        header += (
            f"候选 {candidate_count} 只 · 有效 {valid_count} 只"
            + (
                f" · 成交额≥{min_amount_yi}亿"
                if min_amount_yi and min_amount_yi > 0
                else ""
            )
            + "\n"
        )
    header += (
        f"权重: 大盘{W_MARKET} 阶段{W_PHASE} 触发{W_TRIG} "
        f"量价{W_VP} 均线{W_MA} 赔率{W_RR} 仓位{W_POS}\n"
    )

    if not lines:
        return header + "\n无有效结果。\n" + DISCLAIMER

    body: list[str] = []
    for i, ln in enumerate(lines, 1):
        rn = (ln.reasons[:4] + ["…"]) if len(ln.reasons) > 4 else ln.reasons
        rk = ln.risks[:3]
        body.append(
            f"{i}. {ln.code} {ln.name}\n"
            f"　总分 {ln.total}｜大盘{ln.score_market} 阶段{ln.score_phase} "
            f"触发{ln.score_trigger} 量价{ln.score_vp} 均线{ln.score_ma} "
            f"赔率{ln.score_rr} 仓位维{ln.score_position}\n"
            f"　阶段: {ln.phase}｜触发: {ln.trigger}\n"
            f"　{ln.rr_text}\n"
            f"　仓位建议: {ln.position_advice}\n"
            f"　要点: " + " · ".join(rn) + "\n"
            + (f"　风险: " + " · ".join(rk) + "\n" if rk else "")
        )

    return header + truncation_note + "\n".join(body) + DISCLAIMER


async def _batch_wyckoff_lines(
    fund_analyzer: Any,
    pairs: list[tuple[str, str]],
    regime: MarketRegimeSnapshot,
    *,
    max_concurrent: int = DEFAULT_SCREENING_CONCURRENCY,
) -> list[WyckoffLine]:
    sem = asyncio.Semaphore(max_concurrent)
    lo, hi = SCREENING_JITTER_SEC

    async def fetch_kline(code: str) -> Optional[list[dict]]:
        async with sem:
            if SCREENING_JITTER_ENABLED and hi > 0:
                await asyncio.sleep(random.uniform(lo, hi))
            try:
                return await fund_analyzer.get_lof_history(
                    code,
                    days=HISTORY_DAYS,
                    adjust="qfq",
                    prefer_otc=False,
                    kline_max_retries=KLINE_MAX_RETRIES_SCREENING,
                )
            except Exception as e:
                logger.debug(f"威科夫拉取 K 线失败 {code}: {e}")
                return None

    async def one(code: str, nm: str) -> Optional[WyckoffLine]:
        hist = await fetch_kline(code)
        if not hist:
            return None
        return analyze_stock_wyckoff(hist, code=code, name=nm, regime=regime)

    results = await asyncio.gather(*[one(c, n) for c, n in pairs])
    return [r for r in results if r is not None]


async def screen_wyckoff_stocks(
    stock_analyzer: Any,
    fund_analyzer: Any,
    *,
    max_scan: int = 200,
    min_amount_yi: float = 1.0,
    max_concurrent: int = DEFAULT_SCREENING_CONCURRENCY,
    exclude_bse: bool = False,
    exclude_chinext: bool = False,
    exclude_star: bool = False,
    exclude_limit_up: bool = False,
    exclude_st: bool = True,
) -> tuple[list[WyckoffLine], MarketRegimeSnapshot, int, int]:
    regime = await fetch_market_regime_once(fund_analyzer)

    df = await stock_analyzer.get_a_share_spot_for_screening()
    if df is None or len(df) == 0:
        return [], regime, 0, 0

    pairs = _pairs_for_short_term(
        df,
        max_scan=max_scan,
        min_amount_yi=min_amount_yi,
        exclude_bse=exclude_bse,
        exclude_chinext=exclude_chinext,
        exclude_star=exclude_star,
        exclude_limit_up=exclude_limit_up,
        exclude_st=exclude_st,
    )
    attempted = len(pairs)
    if not pairs:
        return [], regime, 0, 0

    lines = await _batch_wyckoff_lines(
        fund_analyzer, pairs, regime, max_concurrent=max_concurrent
    )
    lines.sort(key=lambda x: -x.total)
    return lines, regime, attempted, len(lines)


async def batch_wyckoff_by_codes(
    stock_analyzer: Any,
    fund_analyzer: Any,
    codes: list[str],
    *,
    max_concurrent: int = DEFAULT_SCREENING_CONCURRENCY,
) -> tuple[list[WyckoffLine], MarketRegimeSnapshot, int, int]:
    regime = await fetch_market_regime_once(fund_analyzer)
    if not codes:
        return [], regime, 0, 0

    sem_name = asyncio.Semaphore(max_concurrent)

    async def resolve_name(code: str) -> str:
        async with sem_name:
            try:
                info = await stock_analyzer.get_stock_realtime(code)
            except Exception as e:
                logger.debug(f"威科夫批量名称解析失败 {code}: {e}")
                return ""
        if info is None:
            return ""
        return str(getattr(info, "name", "") or "")

    names = await asyncio.gather(*[resolve_name(c) for c in codes])
    pairs = list(zip(codes, names))

    lines = await _batch_wyckoff_lines(
        fund_analyzer, pairs, regime, max_concurrent=max_concurrent
    )
    lines.sort(key=lambda x: -x.total)
    return lines, regime, len(pairs), len(lines)


# 供 command_parse 批量上限复用
WYCKOFF_BATCH_MAX_CODES = SHORT_TERM_BATCH_MAX_CODES

__all__ = [
    "WYCKOFF_MIN_BARS",
    "WYCKOFF_BATCH_MAX_CODES",
    "MarketRegimeSnapshot",
    "WyckoffLine",
    "evaluate_market_regime",
    "analyze_stock_wyckoff",
    "format_wyckoff_report",
    "screen_wyckoff_stocks",
    "batch_wyckoff_by_codes",
    "fetch_market_regime_once",
    "detect_primary_trigger_from_history",
    "trigger_score_bonus",
    "parse_stock_codes_from_text",
]
