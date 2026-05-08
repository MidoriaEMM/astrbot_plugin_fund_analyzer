"""
短线选股（1-3 日持仓）：基于量价因子的综合打分与排序。
不构成投资建议。

设计要点
- 与「量化精选股票」（中线综合分 + 夏普）和「股票一日游」（当日分时博弈）形成差异化：
  本模块专注于 60 日日线之上的「量价共振 / 量能突破 / 短期动量 / 蓄势待发」等
  适合 1-3 天持仓的短线信号。
- 仅依赖已存在的 60 日 K 线（每条含 open/close/high/low/volume/amount/change_rate），
  不发起额外行情请求。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from astrbot.api import logger

from .exchange_filter import should_exclude_a_share
from ..quant_screening import (
    DEFAULT_SCREENING_CONCURRENCY,
    DISCLAIMER,
    HISTORY_DAYS,
    MIN_HISTORY_BARS,
    SCREENING_EXCLUDE_IF_CHANGE_PCT_GT,
    screen_lof_batch,
)

# 候选筛选默认阈值
SHORT_TERM_MIN_AMOUNT_YI = 1.0  # 默认日均成交额下限（亿元），过滤流动性陷阱
SHORT_TERM_DEFAULT_MAX_SCAN = 200
SHORT_TERM_DEFAULT_TOP = 10

# 因子计算所需最少 K 线根数（短线核心窗口 5/10/20）
SHORT_TERM_MIN_BARS = 22


def _normalize_screening_code(raw: str) -> str:
    c = str(raw).strip()
    digits = "".join(ch for ch in c if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:].zfill(6)
    if c:
        return c.zfill(6)
    return ""


def _is_b_share(code: str) -> bool:
    c = (code or "").zfill(6)
    return c.startswith("200") or c.startswith("900")


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


# ============================================================
# 量价因子
# ============================================================


@dataclass
class ShortTermFactors:
    """短线量价因子（数值化中间结果，用于打分）。"""

    # 动量因子
    momentum_5d: float = 0.0      # 近 5 日累计涨跌幅 (%)
    momentum_3d: float = 0.0      # 近 3 日累计涨跌幅 (%)

    # 量能因子（窗口平滑视角）
    volume_ratio: float = 0.0     # 近 5 日均量 / 近 20 日均量
    today_volume_ratio: float = 0.0  # 当日量 / 5 日均量
    amount_burst: float = 0.0     # 当日成交额 / 20 日均成交额（基于 amount，金额维度）

    # 邻日量能脉冲（瞬态颗粒度，补充窗口平滑因子盲区）
    volume_pulse_today: float = 1.0      # vol_t / vol_{t-1}（今日量能脉冲）
    volume_pulse_yesterday: float = 1.0  # vol_{t-1} / vol_{t-2}（昨日量能脉冲）
    volume_pulse_chain: float = 1.0      # vol_t / vol_{t-2}（2 日累计放量倍数）

    # 当日涨跌幅（用于配合量能脉冲判定放量上涨/下跌、缩量假涨）
    today_change_rate: float = 0.0

    # 换手率（情绪因子；K 线源不含时全部为 0，下游降级处理）
    turnover_today: float = 0.0           # 当日换手率 (%)
    turnover_5d_avg: float = 0.0          # 近 5 日均换手率 (%)
    turnover_burst: float = 1.0           # 当日换手率 / 20 日均换手率
    turnover_pct_20d: float = 0.5         # 当日换手率在过去 20 日的分位 [0, 1]
    has_turnover: bool = False            # 是否有可用换手率数据

    # 振幅（多空博弈强度，K 线已有 high/low 即可）
    amplitude_today: float = 0.0          # 当日振幅 (%)，(high - low) / prev_close * 100
    amplitude_5d_avg: float = 0.0         # 近 5 日均振幅 (%)

    # 主力资金流向（可选；需调用方提供 flow_data，否则全部默认）
    main_inflow_3d_total: float = 0.0     # 3 日主力净流入金额（元）
    main_inflow_3d_rate: float = 0.0      # 3 日主力净流入率 = main_total / amount_3d * 100 (%)
    super_large_ratio: float = 0.0        # 超大单 / 主力 比例（约 -1 ~ +1+）
    is_diverge_top: bool = False          # 价涨 ≥5% 但主力净流出（顶部派发）
    is_diverge_bottom: bool = False       # 价跌 ≤-3% 但主力净流入（底部吸筹）
    main_vs_small_state: str = "中性"     # "吸筹" / "出货" / "中性"
    has_fund_flow: bool = False           # 是否有可用资金流数据

    # 量价共振
    up_volume_days: int = 0       # 近 5 日中「上涨且放量（>5日均量）」天数
    down_shrink_days: int = 0     # 近 5 日中「下跌且缩量」天数（健康洗盘）

    # 趋势/突破
    above_ma5: bool = False       # 收盘价 > MA5
    above_ma10: bool = False      # 收盘价 > MA10
    above_ma20: bool = False      # 收盘价 > MA20
    ma_bullish_align: bool = False  # MA5>MA10>MA20 多头排列
    breakout_20d_high: bool = False  # 当日收盘 > 过去 20 日（不含今日）最高价 * 0.998

    # 波动率压缩 → 蓄势
    atr_compression: float = 1.0  # 近 5 日 ATR / 近 20 日 ATR（<1 表示压缩）

    # 价格位置
    price_position_20d: float = 0.5  # (今收 - 20日最低) / (20日最高 - 20日最低)，0~1

    # 短期 RSI（用于反弹捕捉，非主因子）
    rsi_3: float = 50.0

    # 风险面
    cum_gain_5d: float = 0.0      # 近 5 日累计涨幅，用于「涨太多回避」判定
    distance_to_limit_pct: float = 999.0  # 当日涨幅离涨停限度的距离（百分点）

    # 流动性
    avg_amount_5d: float = 0.0    # 近 5 日均成交额（元）

    # 是否有足够数据
    valid: bool = False


def _ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = (v - e) * multiplier + e
    return e


def _sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(prices: list[float], period: int = 3) -> float:
    if len(prices) < period + 1:
        return 50.0
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(0.0, c) for c in changes[:period]]
    losses = [abs(min(0.0, c)) for c in changes[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for c in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(0.0, c)) / period
        avg_loss = (avg_loss * (period - 1) + abs(min(0.0, c))) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


def compute_short_term_factors(
    history: list[dict],
    *,
    code: str = "",
    name: str = "",
    flow_data: Optional[list[dict]] = None,
) -> ShortTermFactors:
    """
    从 60 日 K 线计算短线量价因子。

    Args:
        history: 日 K 线列表，每条含 open/close/high/low/volume/amount/change_rate/turnover_rate
        flow_data: 可选，资金流向列表（来自 EastMoneyAPI.get_fund_flow），
                   每条含 main_net_inflow/super_large_inflow/large_inflow/medium_inflow/small_inflow。
                   提供时会计算 G 段「主力流向」相关因子；为空时整段降级为默认值且 has_fund_flow=False。
    """
    f = ShortTermFactors()
    if not history or len(history) < SHORT_TERM_MIN_BARS:
        return f

    closes = [_safe_float(h.get("close")) for h in history]
    opens = [_safe_float(h.get("open")) for h in history]
    highs = [_safe_float(h.get("high"), c) for h, c in zip(history, closes)]
    lows = [_safe_float(h.get("low"), c) for h, c in zip(history, closes)]
    volumes = [_safe_float(h.get("volume")) for h in history]
    amounts = [_safe_float(h.get("amount")) for h in history]
    change_rates = [_safe_float(h.get("change_rate")) for h in history]
    turnovers = [_safe_float(h.get("turnover_rate")) for h in history]

    if closes[-1] <= 0:
        return f

    today_close = closes[-1]
    today_change = change_rates[-1] if change_rates else 0.0
    today_volume = volumes[-1]
    today_amount = amounts[-1]

    # 动量因子
    if len(closes) >= 6 and closes[-6] > 0:
        f.momentum_5d = (today_close / closes[-6] - 1.0) * 100.0
    if len(closes) >= 4 and closes[-4] > 0:
        f.momentum_3d = (today_close / closes[-4] - 1.0) * 100.0
    f.cum_gain_5d = f.momentum_5d

    # 量能因子
    vol_ma5 = _sma(volumes, 5) or 0.0
    vol_ma20 = _sma(volumes, 20) or 0.0
    f.volume_ratio = (vol_ma5 / vol_ma20) if vol_ma20 > 0 else 0.0
    f.today_volume_ratio = (today_volume / vol_ma5) if vol_ma5 > 0 else 0.0

    amt_ma20 = _sma(amounts, 20) or 0.0
    f.amount_burst = (today_amount / amt_ma20) if amt_ma20 > 0 else 0.0
    f.avg_amount_5d = _sma(amounts, 5) or 0.0

    # 邻日量能脉冲：捕捉「窗口均值会平滑掉」的瞬态启动信号
    if len(volumes) >= 3:
        v_t, v_t1, v_t2 = volumes[-1], volumes[-2], volumes[-3]
        if v_t1 > 0:
            f.volume_pulse_today = v_t / v_t1
        if v_t2 > 0:
            f.volume_pulse_yesterday = v_t1 / v_t2
            f.volume_pulse_chain = v_t / v_t2

    # 当日涨跌幅
    f.today_change_rate = today_change

    # 换手率（情绪因子）：仅当 K 线源提供时启用，否则全部留默认值（has_turnover=False）
    if any(t > 0 for t in turnovers[-20:]):
        f.has_turnover = True
        f.turnover_today = turnovers[-1]
        f.turnover_5d_avg = _sma(turnovers, 5) or 0.0
        turnover_ma20 = _sma(turnovers, 20) or 0.0
        f.turnover_burst = (
            (f.turnover_today / turnover_ma20) if turnover_ma20 > 0 else 1.0
        )
        # 当日在过去 20 日（含今日）的分位（rank-based，0~1）
        if len(turnovers) >= 20:
            window = turnovers[-20:]
            today_t = f.turnover_today
            # 严格小于今日的天数 / 20，取 [0,1]
            rank_below = sum(1 for v in window if v < today_t)
            f.turnover_pct_20d = rank_below / 20.0

    # 振幅（多空博弈强度）：(high - low) / prev_close * 100
    if len(closes) >= 6:
        amps: list[float] = []
        for i in range(-5, 0):
            prev_c = closes[i - 1] if (i - 1) >= -len(closes) else 0.0
            if prev_c > 0:
                amps.append((highs[i] - lows[i]) / prev_c * 100.0)
        if amps:
            f.amplitude_5d_avg = sum(amps) / len(amps)
            f.amplitude_today = amps[-1]

    # 量价共振：近 5 日逐日观察
    if len(closes) >= 6 and vol_ma5 > 0:
        up_vol = 0
        down_shrink = 0
        for i in range(-5, 0):
            if closes[i - 1] <= 0:
                continue
            day_chg = closes[i] - closes[i - 1]
            day_vol = volumes[i]
            if day_chg > 0 and day_vol > vol_ma5:
                up_vol += 1
            if day_chg < 0 and day_vol < vol_ma5:
                down_shrink += 1
        f.up_volume_days = up_vol
        f.down_shrink_days = down_shrink

    # 趋势 / 均线
    ma5 = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    if ma5:
        f.above_ma5 = today_close > ma5
    if ma10:
        f.above_ma10 = today_close > ma10
    if ma20:
        f.above_ma20 = today_close > ma20
    if ma5 and ma10 and ma20:
        f.ma_bullish_align = ma5 > ma10 > ma20

    # 突破 20 日高点（不含今日，避免自己突破自己）
    if len(highs) >= 21:
        prior_high = max(highs[-21:-1])
        if prior_high > 0:
            f.breakout_20d_high = today_close > prior_high * 0.998

    # 波动率压缩
    atr_5 = _atr(highs, lows, closes, 5)
    atr_20 = _atr(highs, lows, closes, 20)
    if atr_5 and atr_20 and atr_20 > 0:
        f.atr_compression = atr_5 / atr_20

    # 价格在 20 日区间位置
    if len(highs) >= 20 and len(lows) >= 20:
        h20 = max(highs[-20:])
        l20 = min(lows[-20:])
        rng = h20 - l20
        if rng > 0:
            f.price_position_20d = max(0.0, min(1.0, (today_close - l20) / rng))

    # 短期 RSI(3)
    f.rsi_3 = _rsi(closes, 3)

    # 风险面：离涨停的距离（不细分板块，统一近似 10%）
    f.distance_to_limit_pct = 10.0 - today_change

    # 主力资金流（可选）：3 日累计 + 价量背离 + 主散对冲 + 超大单占比
    if flow_data and len(flow_data) >= 3:
        recent3 = flow_data[-3:]
        main_total = sum(_safe_float(it.get("main_net_inflow")) for it in recent3)
        super_total = sum(_safe_float(it.get("super_large_inflow")) for it in recent3)
        small_total = sum(_safe_float(it.get("small_inflow")) for it in recent3)

        # 同期成交额：取 K 线最后 3 日 amount（注意可能与 flow_data 日期略有错位，
        # 但东财同源对齐度高，作为简单近似可接受）
        amount_3d = sum(amounts[-3:]) if len(amounts) >= 3 else 0.0

        f.has_fund_flow = True
        f.main_inflow_3d_total = main_total
        f.main_inflow_3d_rate = (
            main_total / amount_3d * 100.0 if amount_3d > 0 else 0.0
        )
        # 超大单占比：当主力净流入接近 0 时设为 0 避免发散
        if abs(main_total) >= 1e6:  # 至少百万级才计算占比
            f.super_large_ratio = super_total / main_total
        else:
            f.super_large_ratio = 0.0

        # 主散对冲
        if main_total > 0 and small_total < 0:
            f.main_vs_small_state = "吸筹"
        elif main_total < 0 and small_total > 0:
            f.main_vs_small_state = "出货"
        else:
            f.main_vs_small_state = "中性"

        # 价量资金背离（基于 3 日累计涨跌幅 momentum_3d）
        if f.momentum_3d >= 5.0 and main_total < 0:
            f.is_diverge_top = True
        elif f.momentum_3d <= -3.0 and main_total > 0:
            f.is_diverge_bottom = True

    # 引用未使用变量以兼顾静态检查器
    _ = (opens, code, name)

    f.valid = True
    return f


# ============================================================
# 综合打分 + 信号分类
# ============================================================


@dataclass
class ShortTermScore:
    """短线综合评分及解释。"""

    score: float = 0.0       # 0 ~ 100
    signal: str = "观望"     # 趋势加速 / 蓄势爆发 / 缩量企稳 / 超跌反弹 / 观望 / 规避
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score_short_term_factors(f: ShortTermFactors) -> ShortTermScore:
    """
    对 1-3 日持仓打分（0~100），并给出信号标签与多空理由。

    评分构成（满分约 100）
    - 动量 (max 20)
    - 量能突破 (max 20)
    - 量价共振 (max 15)
    - 趋势/突破 (max 20)
    - 蓄势/位置 (max 15)
    - 短期反弹 RSI(3) (max 10)
    再叠加风险扣分（涨幅过高、贴近涨停）
    """
    s = ShortTermScore()
    if not f.valid:
        s.signal = "数据不足"
        return s

    score = 0.0
    reasons: list[str] = []
    risks: list[str] = []

    # 1) 动量（5日）
    if f.momentum_5d >= 8:
        score += 20
        reasons.append(f"5日动量强（{f.momentum_5d:+.2f}%）")
    elif f.momentum_5d >= 4:
        score += 14
        reasons.append(f"5日动量偏强（{f.momentum_5d:+.2f}%）")
    elif f.momentum_5d >= 1:
        score += 8
    elif f.momentum_5d >= -2:
        score += 3
    elif f.momentum_5d >= -5:
        score += 0
    else:
        score -= 6
        risks.append(f"5日跌幅较深（{f.momentum_5d:+.2f}%），趋势仍弱")

    # 3 日动量加成（捕捉急速启动）
    if f.momentum_3d >= 5:
        score += 4
        reasons.append(f"3日加速（{f.momentum_3d:+.2f}%）")

    # ====================================================================
    # 「资金活跃度」综合块：量能（窗口）+ 邻日脉冲 + 换手率（情绪）+ 振幅
    # 总权重控制在 +25 / -15 内，避免单一维度堆分
    # ====================================================================
    chg = f.today_change_rate

    # A) 中期量能阶梯（5日均量 vs 20日均量；最大 +6 / -3）
    if f.volume_ratio >= 1.5:
        score += 6
        reasons.append(f"中期量能阶梯放大（5/20={f.volume_ratio:.2f}）")
    elif f.volume_ratio >= 1.2:
        score += 3
    elif f.volume_ratio >= 0.9:
        score += 0
    else:
        score -= 3
        risks.append(f"中期量能持续萎缩（5/20={f.volume_ratio:.2f}）")

    # B) 当日量能 / 成交额爆发（最大 +4）
    # 当日量倍数（vs 5日均量）与成交额倍数（vs 20日均额）取较强者计分，避免双重叠加
    if f.today_volume_ratio >= 2.0 or f.amount_burst >= 2.5:
        score += 4
        reasons.append(
            f"当日量能爆发（{f.today_volume_ratio:.2f}× 5日均量，"
            f"{f.amount_burst:.2f}× 20日均额）"
        )
    elif f.today_volume_ratio >= 1.5 or f.amount_burst >= 1.8:
        score += 2

    # C) 邻日量能脉冲（互斥分支，最大 +6 / -4）
    vt = f.volume_pulse_today
    vy = f.volume_pulse_yesterday
    chain = f.volume_pulse_chain

    # C.1) 连续阶梯放量（最强信号）
    if vt >= 1.2 and vy >= 1.2 and chain >= 2.5:
        score += 6
        reasons.append(
            f"连续阶梯放量（前→昨 {vy:.2f}×、昨→今 {vt:.2f}×，累计 {chain:.2f}×）"
        )
    elif vt >= 2.0 and chg > 0:
        score += 5
        reasons.append(f"今日量能脉冲爆发（{vt:.2f}× 昨日量）")
    elif vt >= 1.5 and chg > 0:
        score += 3
        reasons.append(f"今日量能温和放大（{vt:.2f}× 昨日量）")

    # C.2) 邻日反向风险
    if vt >= 1.8 and chg <= -2:
        score -= 4
        risks.append(f"放量下跌（{vt:.2f}× 昨日量，{chg:+.2f}%），警惕主力出货")
    if vt <= 0.6 and chg >= 1.5:
        score -= 2
        risks.append(f"缩量上涨（{vt:.2f}× 昨日量，{chg:+.2f}%），动能不济")

    # D) 换手率（情绪因子）；K 线源缺失时整段降级跳过
    if f.has_turnover:
        # D.1) 情绪点火 vs 恐慌扩散（最大 +4 / -5）
        if f.turnover_burst >= 2.0:
            if chg > 0:
                score += 4
                reasons.append(
                    f"情绪激活：换手率 {f.turnover_today:.2f}%，"
                    f"{f.turnover_burst:.2f}×20日均（涨 {chg:+.2f}%）"
                )
            elif chg <= -2:
                score -= 5
                risks.append(
                    f"恐慌扩散：换手率 {f.turnover_today:.2f}%，"
                    f"{f.turnover_burst:.2f}×20日均（跌 {chg:+.2f}%）"
                )

        # D.2) 情绪顶部预警（最大 -5）：高分位 + 累涨过高
        if f.turnover_pct_20d >= 0.95 and f.cum_gain_5d >= 8:
            score -= 5
            risks.append(
                f"⚠️ 情绪过热：换手率处近 20 日 {int(f.turnover_pct_20d * 100)}% 分位 + "
                f"5日累涨 {f.cum_gain_5d:+.2f}%，警惕反转"
            )
        elif 0.6 <= f.turnover_pct_20d <= 0.85:
            score += 2
            reasons.append(
                f"温和热度（换手率 {f.turnover_today:.2f}%，"
                f"近 20 日 {int(f.turnover_pct_20d * 100)}% 分位）"
            )
        elif f.turnover_pct_20d <= 0.15:
            score -= 2
            risks.append(
                f"关注度不足（换手率 {f.turnover_today:.2f}% 处近 20 日低分位）"
            )

    # E) 振幅（多空博弈强度，最大 +3 / -2）
    if f.amplitude_5d_avg > 0:
        amp_ratio = f.amplitude_today / f.amplitude_5d_avg
        if amp_ratio >= 1.5 and chg > 0:
            score += 3
            reasons.append(
                f"放量博弈多头胜出（振幅 {f.amplitude_today:.2f}% > 5日均×1.5）"
            )
        elif amp_ratio >= 1.5 and chg <= -2:
            score -= 2
            risks.append(f"放量博弈空头压制（振幅 {f.amplitude_today:.2f}%，跌幅深）")

    # ====================================================================
    # 量价共振（5 日逐日观察，独立小块；最大 +8）
    # ====================================================================
    if f.up_volume_days >= 3:
        score += 8
        reasons.append(f"5日内 {f.up_volume_days} 天放量上涨")
    elif f.up_volume_days >= 2:
        score += 4
    if f.down_shrink_days >= 2:
        score += 3
        reasons.append(f"5日内 {f.down_shrink_days} 天缩量回踩（健康洗盘）")

    # ====================================================================
    # G) 「主力流向」打分块（可选启用：has_fund_flow=True 才执行）
    # 净权重 +13 / -15；G.1 与 G.2 互斥，避免双重加扣分
    # ====================================================================
    if f.has_fund_flow:
        rate = f.main_inflow_3d_rate
        main_yi = f.main_inflow_3d_total / 1e8
        # 显示用 rate：cap 到 ±99.99%，避免数据口径异常时输出离谱百分比
        rate_disp = max(-99.99, min(99.99, rate))

        # G.1 / G.2 互斥：背离信号（顶部派发 / 底部吸筹）优先
        if f.is_diverge_top:
            # 价涨 ≥5% 但主力净流出（最强反向警示，比 G.1 单独的派发更危险）
            score -= 6
            risks.append(
                f"⚠️ 顶部派发：3日涨 {f.momentum_3d:+.2f}% 但主力净流出 "
                f"{main_yi:+.2f}亿（占成交 {rate_disp:+.2f}%）"
            )
        elif f.is_diverge_bottom:
            score += 5
            reasons.append(
                f"底部吸筹：3日跌 {f.momentum_3d:+.2f}% 但主力净流入 "
                f"{main_yi:+.2f}亿（占成交 {rate_disp:+.2f}%）"
            )
        else:
            # G.1 3日累计主力净流入率
            if rate >= 5.0:
                score += 6
                reasons.append(
                    f"主力强势吸筹（3日净流入 {main_yi:+.2f}亿，占成交 {rate_disp:+.2f}%）"
                )
            elif rate >= 2.0:
                score += 3
                reasons.append(f"主力温和流入（3日 {rate_disp:+.2f}%）")
            elif rate <= -5.0:
                score -= 5
                risks.append(
                    f"主力派发（3日净流出 {main_yi:+.2f}亿，占成交 {rate_disp:+.2f}%）"
                )
            elif rate <= -2.0:
                score -= 2

        # G.3 主散对冲（独立维度，可与 G.1/G.2 叠加）
        if f.main_vs_small_state == "吸筹":
            score += 3
            reasons.append("主力吸筹 + 散户割肉，结构健康")
        elif f.main_vs_small_state == "出货":
            score -= 4
            risks.append("⚠️ 主力出货 + 散户接盘，警惕套牢")

        # G.4 超大单占比（仅在主力流入显著时作为确定性加成）
        if f.super_large_ratio > 0.7 and rate >= 2.0:
            score += 2
            reasons.append(f"超大单主导（占主力 {f.super_large_ratio*100:.0f}%）")

    # 4) 趋势 / 突破
    if f.ma_bullish_align:
        score += 10
        reasons.append("均线多头排列（MA5>MA10>MA20）")
    elif f.above_ma5 and f.above_ma10:
        score += 6
        reasons.append("收于 MA5 与 MA10 上方")
    elif f.above_ma5:
        score += 3
    else:
        score -= 4
        risks.append("收盘跌破 MA5，短期偏弱")

    if f.breakout_20d_high:
        score += 10
        reasons.append("有效突破 20 日高点")

    # 5) 蓄势 / 价格位置
    if f.atr_compression <= 0.7:
        score += 8
        reasons.append(f"波动率压缩（5/20 ATR={f.atr_compression:.2f}），蓄势状态")
    elif f.atr_compression <= 0.85:
        score += 4

    # 价格位置：靠近 20 日高点（≥0.7）配合突破/放量更优；
    # 处于中部（0.3~0.7）次之；过低（<0.2）多为弱势
    if 0.7 <= f.price_position_20d <= 0.95:
        score += 7
        reasons.append(
            f"价格位于 20 日区间高位（{f.price_position_20d:.2f}），强势"
        )
    elif 0.3 <= f.price_position_20d < 0.7:
        score += 3
    elif f.price_position_20d > 0.95:
        score += 1
        risks.append("已触 20 日区间顶部，进一步上行需放量")
    else:
        score -= 2

    # 6) 短期反弹 RSI(3)
    if 20 <= f.rsi_3 <= 35 and f.momentum_5d <= -3:
        score += 10
        reasons.append(f"RSI3={f.rsi_3:.1f} 处于超卖区，具备反弹动能")
    elif f.rsi_3 >= 90:
        score -= 8
        risks.append(f"RSI3={f.rsi_3:.1f} 极度超买，1-3 日有回调风险")
    elif f.rsi_3 >= 80:
        score -= 4
        risks.append(f"RSI3={f.rsi_3:.1f} 短线超买")

    # 7) 风险扣分：涨太多 / 贴近涨停
    if f.cum_gain_5d >= 18:
        score -= 12
        risks.append(f"5日累计涨幅 {f.cum_gain_5d:+.2f}%，追高风险高")
    elif f.cum_gain_5d >= 12:
        score -= 6
        risks.append(f"5日累计涨幅 {f.cum_gain_5d:+.2f}%，注意回撤")

    if f.distance_to_limit_pct <= 0.5:
        score -= 10
        risks.append("当日已贴近涨停（10cm 板），短线接力风险大")
    elif f.distance_to_limit_pct <= 2:
        score -= 4
        risks.append("当日涨幅靠近板上，谨慎追高")

    # 限幅 0~100
    s.score = round(_clip(score, 0.0, 100.0), 1)
    s.reasons = reasons
    s.risks = risks

    # 信号标签
    if s.score >= 70 and f.breakout_20d_high and f.volume_ratio >= 1.2:
        s.signal = "趋势加速"
    elif s.score >= 60 and f.atr_compression <= 0.85 and f.up_volume_days >= 1:
        s.signal = "蓄势爆发"
    elif (
        s.score >= 50
        and f.momentum_5d <= -3
        and 20 <= f.rsi_3 <= 40
        and f.down_shrink_days >= 1
    ):
        s.signal = "超跌反弹"
    elif s.score >= 50 and f.down_shrink_days >= 2 and f.above_ma20:
        s.signal = "缩量企稳"
    elif s.score >= 55:
        s.signal = "短线偏多"
    elif s.score <= 25:
        s.signal = "规避"
    else:
        s.signal = "观望"

    return s


# ============================================================
# 输出行 / 数据流
# ============================================================


@dataclass
class ShortTermLine:
    code: str
    name: str
    score: float
    signal: str
    momentum_5d: float
    today_volume_ratio: float
    volume_ratio: float
    breakout: bool
    price_position_20d: float
    avg_amount_yi: float
    turnover_today: float       # 当日换手率 %（无数据时为 0）
    turnover_pct_20d: float     # 当日换手率在 20 日的分位（无数据时为 -1）
    main_inflow_3d_rate: float  # 3 日主力净流入率 %（无数据时为 0；以 has_fund_flow 区分）
    has_fund_flow: bool         # 是否启用并取得了资金流数据
    top_reason: str
    top_risk: str


def _pairs_for_short_term(
    df: Any,
    *,
    max_scan: int,
    min_amount_yi: float,
    exclude_bse: bool,
    exclude_chinext: bool,
    exclude_star: bool,
    exclude_limit_up: bool,
) -> list[tuple[str, str]]:
    """
    候选筛选：
    - 必有 代码/名称 列；
    - 优先按 |涨跌幅| 降序选活跃股；
    - 用「成交额」列过滤流动性陷阱（min_amount_yi 亿元，东财快照单位为元，
      取近似快照成交额，等价于今日成交额）；
    - 可选剔除 北交所/创业板/科创板/B 股/涨幅 > SCREENING_EXCLUDE_IF_CHANGE_PCT_GT。
    """
    import pandas as pd

    if df is None or len(df) == 0 or max_scan <= 0:
        return []

    code_col = "代码" if "代码" in df.columns else None
    name_col = "名称" if "名称" in df.columns else None
    rate_col = None
    for col in ("涨跌幅", "changepercent", "振幅"):
        if col in df.columns:
            rate_col = col
            break
    amt_col = None
    for col in ("成交额", "amount"):
        if col in df.columns:
            amt_col = col
            break

    if not code_col or not rate_col:
        logger.warning(
            "短线选股：缺少必要列（代码/涨跌幅）。列预览：%s",
            list(df.columns)[:20],
        )
        return []

    dd = df.copy()
    dd["_abs"] = pd.to_numeric(dd[rate_col], errors="coerce").fillna(0).abs()
    dd = dd.sort_values("_abs", ascending=False)

    min_amount = max(0.0, float(min_amount_yi)) * 1e8

    pairs: list[tuple[str, str]] = []
    for _, row in dd.iterrows():
        if len(pairs) >= max_scan:
            break
        c = _normalize_screening_code(row.get(code_col, ""))
        if not c or _is_b_share(c):
            continue
        if should_exclude_a_share(
            c,
            exclude_bse=exclude_bse,
            exclude_chinext=exclude_chinext,
            exclude_star=exclude_star,
        ):
            continue
        if amt_col is not None and min_amount > 0:
            amt = pd.to_numeric(row.get(amt_col), errors="coerce")
            if pd.isna(amt) or float(amt) < min_amount:
                continue
        if exclude_limit_up:
            raw_pct = pd.to_numeric(row.get(rate_col), errors="coerce")
            pct_f = 0.0 if pd.isna(raw_pct) else float(raw_pct)
            if pct_f > SCREENING_EXCLUDE_IF_CHANGE_PCT_GT:
                continue
        nm = str(row[name_col]) if name_col else ""
        pairs.append((c, nm))
    return pairs


def _line_from_history(
    code: str,
    name: str,
    history: list[dict],
    flow_data: Optional[list[dict]] = None,
) -> Optional[ShortTermLine]:
    if not history or len(history) < SHORT_TERM_MIN_BARS:
        return None
    f = compute_short_term_factors(history, code=code, name=name, flow_data=flow_data)
    if not f.valid:
        return None
    s = score_short_term_factors(f)
    top_reason = s.reasons[0] if s.reasons else "—"
    top_risk = s.risks[0] if s.risks else "—"
    return ShortTermLine(
        code=code,
        name=name,
        score=s.score,
        signal=s.signal,
        momentum_5d=f.momentum_5d,
        today_volume_ratio=f.today_volume_ratio,
        volume_ratio=f.volume_ratio,
        breakout=f.breakout_20d_high,
        price_position_20d=f.price_position_20d,
        avg_amount_yi=f.avg_amount_5d / 1e8 if f.avg_amount_5d else 0.0,
        turnover_today=f.turnover_today if f.has_turnover else 0.0,
        turnover_pct_20d=f.turnover_pct_20d if f.has_turnover else -1.0,
        main_inflow_3d_rate=f.main_inflow_3d_rate if f.has_fund_flow else 0.0,
        has_fund_flow=f.has_fund_flow,
        top_reason=top_reason,
        top_risk=top_risk,
    )


async def screen_stocks_short_term(
    stock_analyzer: Any,
    fund_analyzer: Any,
    *,
    max_scan: int = SHORT_TERM_DEFAULT_MAX_SCAN,
    min_amount_yi: float = SHORT_TERM_MIN_AMOUNT_YI,
    max_concurrent: int = DEFAULT_SCREENING_CONCURRENCY,
    exclude_bse: bool = False,
    exclude_chinext: bool = False,
    exclude_star: bool = False,
    exclude_limit_up: bool = False,
    with_fund_flow: bool = False,
) -> tuple[list[ShortTermLine], int, int, int]:
    """
    主流程：A 股快照 → 候选筛选 → 批量拉 60 日 K 线（可选并行拉资金流）→ 综合打分。

    Args:
        with_fund_flow: 是否启用主力资金流向因子（G 段）。开启后每只标的会
                        额外发一次资金流 HTTP，整体耗时 +30~50%。

    Returns:
        lines:        已按 score 降序排列的 ShortTermLine 列表
        attempted:    实际尝试拉 K 线的标的数
        valid:        K 线足够形成因子的有效样本数
        with_flow_n:  with_fund_flow=True 时，成功取得资金流数据的样本数；False 时为 0
    """
    df = await stock_analyzer.get_a_share_spot_for_screening()
    if df is None or len(df) == 0:
        return [], 0, 0, 0

    pairs = _pairs_for_short_term(
        df,
        max_scan=max_scan,
        min_amount_yi=min_amount_yi,
        exclude_bse=exclude_bse,
        exclude_chinext=exclude_chinext,
        exclude_star=exclude_star,
        exclude_limit_up=exclude_limit_up,
    )
    attempted = len(pairs)
    if not pairs:
        return [], 0, 0, 0

    lines = await _batch_compute_lines(
        fund_analyzer,
        pairs,
        max_concurrent=max_concurrent,
        with_fund_flow=with_fund_flow,
    )
    valid = len(lines)
    with_flow_n = sum(1 for ln in lines if ln.has_fund_flow)
    if not lines:
        return [], attempted, 0, 0
    lines.sort(key=lambda x: -x.score)
    return lines, attempted, valid, with_flow_n


async def _batch_compute_lines(
    fund_analyzer: Any,
    pairs: list[tuple[str, str]],
    *,
    max_concurrent: int = DEFAULT_SCREENING_CONCURRENCY,
    with_fund_flow: bool = False,
) -> list[ShortTermLine]:
    """
    并发拉 K 线（可选并行拉资金流）→ 因子化 → ShortTermLine 列表。

    资金流拉取策略：
    - 与 K 线请求 **并行**（asyncio.gather）以最大化吞吐
    - 使用独立的小并发槽（避免与 K 线接口共享限流）
    - 拉取失败时静默降级：has_fund_flow=False，G 段打分跳过
    """
    import asyncio
    import random

    from ..quant_screening import (
        KLINE_MAX_RETRIES_SCREENING,
        SCREENING_JITTER_ENABLED,
        SCREENING_JITTER_SEC,
    )

    sem_kline = asyncio.Semaphore(max_concurrent)
    # 资金流接口走独立限流槽（与 K 线分离）；并发与 K 线相同
    sem_flow = asyncio.Semaphore(max_concurrent)
    lo, hi = SCREENING_JITTER_SEC

    async def fetch_kline(code: str) -> Optional[list[dict]]:
        async with sem_kline:
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
                logger.debug(f"短线选股拉取行情失败 {code}: {e}")
                return None

    async def fetch_flow(code: str) -> Optional[list[dict]]:
        if not with_fund_flow:
            return None
        async with sem_flow:
            if SCREENING_JITTER_ENABLED and hi > 0:
                await asyncio.sleep(random.uniform(lo, hi))
            try:
                # 资金流只需最近 5 天即可（G 段用 3 日累计）
                return await fund_analyzer._api.get_fund_flow(
                    code, days=5, prefer_otc=False
                )
            except Exception as e:
                logger.debug(f"短线选股拉取资金流失败 {code}: {e}")
                return None

    async def one(code: str, nm: str) -> Optional[ShortTermLine]:
        # K 线与资金流并行拉取
        hist, flow = await asyncio.gather(fetch_kline(code), fetch_flow(code))
        if not hist:
            return None
        return _line_from_history(code, nm, hist, flow_data=flow)

    results = await asyncio.gather(*[one(c, n) for c, n in pairs])
    return [r for r in results if r is not None]


# ============================================================
# 文本报告
# ============================================================


def format_short_term_report(
    lines: list[ShortTermLine],
    *,
    title: str = "🎯 短线选股（量价因子 · 1-3 日持仓）",
    candidate_count: int,
    valid_count: int,
    truncation_note: str = "",
    min_amount_yi: float = SHORT_TERM_MIN_AMOUNT_YI,
    with_fund_flow: bool = False,
    fund_flow_count: int = 0,
) -> str:
    flow_meta = (
        f" · 含主力流向: {fund_flow_count}/{valid_count}"
        if with_fund_flow
        else ""
    )
    header = (
        f"{title}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"候选: {candidate_count} 只 · 有效: {valid_count} 只 · "
        f"成交额≥{min_amount_yi}亿{flow_meta}\n"
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    if not lines:
        body = "无满足条件的标的。\n"
        return header + body + (truncation_note or "") + DISCLAIMER

    # 仅在开启资金流时展示「主力%」列；关闭时省略以保持紧凑
    if with_fund_flow:
        col = (
            f"{'排名':<4}{'代码':<8}{'名称':<11}{'评分':<6}{'信号':<8}"
            f"{'5日%':<8}{'量比5/20':<9}{'换手%':<8}{'主力3日%':<10}"
            f"{'位置':<6}{'突破':<5}\n"
        )
    else:
        col = (
            f"{'排名':<4}{'代码':<8}{'名称':<11}{'评分':<6}{'信号':<8}"
            f"{'5日%':<8}{'量比5/20':<9}{'换手%':<8}{'位置':<6}{'突破':<5}\n"
        )
    body_lines = [col]
    for i, r in enumerate(lines, 1):
        nm = r.name if len(r.name) <= 10 else r.name[:9] + "…"
        turnover_cell = (
            f"{r.turnover_today:<8.2f}"
            if r.turnover_pct_20d >= 0
            else f"{'—':<8}"
        )
        if with_fund_flow:
            if r.has_fund_flow:
                # cap 到 ±99.99% 防御异常数据口径
                rate_disp = max(-99.99, min(99.99, r.main_inflow_3d_rate))
                flow_cell = f"{rate_disp:+.2f}%{'':>3}"
            else:
                flow_cell = f"{'—':<10}"
            body_lines.append(
                f"{i:<4}{r.code:<8}{nm:<11}{r.score:<6.1f}{r.signal:<8}"
                f"{r.momentum_5d:+.2f}%{'':>1}{r.volume_ratio:<9.2f}"
                f"{turnover_cell}{flow_cell}{r.price_position_20d:<6.2f}"
                f"{'是' if r.breakout else '否':<5}\n"
            )
        else:
            body_lines.append(
                f"{i:<4}{r.code:<8}{nm:<11}{r.score:<6.1f}{r.signal:<8}"
                f"{r.momentum_5d:+.2f}%{'':>1}{r.volume_ratio:<9.2f}"
                f"{turnover_cell}{r.price_position_20d:<6.2f}"
                f"{'是' if r.breakout else '否':<5}\n"
            )

    detail_lines: list[str] = []
    for i, r in enumerate(lines[:5], 1):  # 仅展示前 5 名详情
        detail_lines.append(
            f"#{i} {r.code} {r.name}\n"
            f"   ✓ {r.top_reason}\n"
            f"   ⚠ {r.top_risk}"
        )
    detail = "\n详情（前 5 名）:\n" + "\n".join(detail_lines) if detail_lines else ""

    return (
        header
        + "".join(body_lines)
        + (truncation_note or "")
        + detail
        + DISCLAIMER
    )
