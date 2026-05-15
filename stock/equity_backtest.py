"""A 股区间回测：买入持有绩效 + 可选信号策略（复用 QuantAnalyzer）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ai_analyzer.quant import BacktestResult, PerformanceMetrics, QuantAnalyzer

MIN_BARS_INTERVAL = 2
MIN_BARS_PERFORMANCE = 5
MIN_BARS_ALL_STRATEGIES = 40


class StrategyMode(str, Enum):
    AUTO = "auto"
    OFF = "off"
    ON = "on"


@dataclass
class EquityBacktestConfig:
    strategy_mode: StrategyMode = StrategyMode.AUTO


def _load_quant_module():
    """插件内用相对导入；避免顶层 ``ai_analyzer`` 在 AstrBot 包路径下找不到。"""
    try:
        from ..ai_analyzer.quant import (  # type: ignore[import-not-found]
            BacktestResult,
            PerformanceMetrics,
            QuantAnalyzer,
        )

        return BacktestResult, PerformanceMetrics, QuantAnalyzer
    except ImportError:
        pass

    root = Path(__file__).resolve().parent.parent
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)

    mod_name = "ai_analyzer.quant"
    if mod_name not in sys.modules:
        if "ai_analyzer" not in sys.modules:
            pkg = types.ModuleType("ai_analyzer")
            pkg.__path__ = [str(root / "ai_analyzer")]
            sys.modules["ai_analyzer"] = pkg
        qpath = root / "ai_analyzer" / "quant.py"
        spec = importlib.util.spec_from_file_location(mod_name, qpath)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载量化模块: {qpath}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)

    m = sys.modules[mod_name]
    return m.BacktestResult, m.PerformanceMetrics, m.QuantAnalyzer


BacktestResult, PerformanceMetrics, QuantAnalyzer = _load_quant_module()


@dataclass
class EquityBacktestMeta:
    ts_code: str
    name: str
    start_date: str
    end_date: str
    trading_days: int
    start_close: float
    end_close: float
    buy_hold_return_pct: float


@dataclass
class EquityBacktestReport:
    meta: EquityBacktestMeta
    buy_hold: PerformanceMetrics | None
    strategies: list[BacktestResult]
    strategy_mode: StrategyMode = StrategyMode.AUTO
    strategy_skip_note: str | None = None


def _date_key(d: str) -> str:
    return str(d).replace("-", "")[:8]


def filter_history_by_date_range(
    history: list[dict[str, Any]],
    start_yyyymmdd: str,
    end_yyyymmdd: str,
) -> list[dict[str, Any]]:
    start = start_yyyymmdd.replace("-", "")[:8]
    end = end_yyyymmdd.replace("-", "")[:8]
    return [h for h in history if start <= _date_key(h.get("date", "")) <= end]


def _resolve_strategies(
    history: list[dict[str, Any]],
    config: EquityBacktestConfig,
) -> tuple[list[BacktestResult], str | None]:
    n = len(history)
    mode = config.strategy_mode

    if mode == StrategyMode.OFF:
        return [], "未启用策略回测（仅基准）"

    if mode == StrategyMode.AUTO and n < MIN_BARS_ALL_STRATEGIES:
        return [], (
            f"区间仅 {n} 个交易日，已跳过 MA/RSI/MACD 策略回测"
            f"（全套策略建议≥{MIN_BARS_ALL_STRATEGIES} 日）；"
            f"可加「含策略」强制尝试"
        )

    quant = QuantAnalyzer()
    return quant.run_all_backtests(history), None


def build_equity_backtest_report(
    history: list[dict[str, Any]],
    *,
    ts_code: str,
    name: str = "",
    start_date: str,
    end_date: str,
    config: EquityBacktestConfig | None = None,
) -> EquityBacktestReport:
    cfg = config or EquityBacktestConfig()
    if not history or len(history) < MIN_BARS_INTERVAL:
        raise ValueError(
            f"历史 K 线不足，无法回测（至少需要 {MIN_BARS_INTERVAL} 个交易日）"
        )

    start_close = float(history[0].get("close") or 0)
    end_close = float(history[-1].get("close") or 0)
    if start_close > 0:
        bh_ret = (end_close - start_close) / start_close * 100
    else:
        bh_ret = 0.0

    meta = EquityBacktestMeta(
        ts_code=ts_code,
        name=name or ts_code,
        start_date=start_date,
        end_date=end_date,
        trading_days=len(history),
        start_close=round(start_close, 4),
        end_close=round(end_close, 4),
        buy_hold_return_pct=round(bh_ret, 2),
    )

    quant = QuantAnalyzer()
    buy_hold = (
        quant.calculate_performance(history)
        if len(history) >= MIN_BARS_PERFORMANCE
        else None
    )
    strategies, skip_note = _resolve_strategies(history, cfg)

    return EquityBacktestReport(
        meta=meta,
        buy_hold=buy_hold,
        strategies=strategies,
        strategy_mode=cfg.strategy_mode,
        strategy_skip_note=skip_note,
    )


def format_equity_backtest_report(report: EquityBacktestReport) -> str:
    m = report.meta
    range_s = f"{m.start_date}-{m.end_date}"
    lines = [
        f"股票回测 {m.ts_code} {m.name}",
        f"区间 {range_s}（模式: {report.strategy_mode.value}）",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"【区间概况】交易日 {m.trading_days} 天",
        f"  期初收盘: {m.start_close:.4f} → 期末收盘: {m.end_close:.4f}",
        f"  买入持有涨跌: {m.buy_hold_return_pct:+.2f}%",
    ]

    quant = QuantAnalyzer()
    if report.buy_hold:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("【买入持有 · 绩效】")
        lines.append(quant.format_performance_text(report.buy_hold))
    elif m.trading_days < MIN_BARS_PERFORMANCE:
        lines.append(
            f"  （绩效指标需至少 {MIN_BARS_PERFORMANCE} 个交易日，"
            f"当前仅展示区间涨跌）"
        )

    if report.strategies:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("【策略回测】")
        lines.append(quant.format_backtest_text(report.strategies).rstrip())
    elif report.strategy_skip_note:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"【策略回测】{report.strategy_skip_note}")

    return "\n".join(lines)
