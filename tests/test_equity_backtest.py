"""股票区间回测单元测试。"""

from __future__ import annotations

import logging
import sys
import types
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("test")
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

from command_parse import parse_date_range_token, parse_stock_backtest_tail
from stock.equity_backtest import (
    EquityBacktestConfig,
    MIN_BARS_ALL_STRATEGIES,
    StrategyMode,
    build_equity_backtest_report,
    filter_history_by_date_range,
    format_equity_backtest_report,
)


def _mock_history(n: int = 30, base: float = 10.0) -> list[dict]:
    rows = []
    for i in range(n):
        c = base + i * 0.1
        rows.append(
            {
                "date": f"2024-01-{i + 1:02d}",
                "open": c - 0.05,
                "close": c,
                "high": c + 0.1,
                "low": c - 0.1,
                "volume": 1e6,
                "amount": 1e7,
                "change_rate": 0.5,
            }
        )
    return rows


class TestParseDateRange(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(
            parse_date_range_token("20240101-20240630"),
            ("20240101", "20240630"),
        )

    def test_inverted(self):
        with self.assertRaises(ValueError):
            parse_date_range_token("20240630-20240101")

    def test_bad_format(self):
        with self.assertRaises(ValueError):
            parse_date_range_token("20240101")


class TestParseStockBacktestTail(unittest.TestCase):
    def test_ok_auto(self):
        code, start, end, mode = parse_stock_backtest_tail(
            "600519 20240101-20240630"
        )
        self.assertEqual(code, "600519")
        self.assertEqual(start, "20240101")
        self.assertEqual(end, "20240630")
        self.assertEqual(mode, "auto")

    def test_benchmark_only(self):
        _, _, _, mode = parse_stock_backtest_tail(
            "600519 20240101-20240115 仅基准"
        )
        self.assertEqual(mode, "off")

    def test_include_strategy_priority(self):
        _, _, _, mode = parse_stock_backtest_tail(
            "600519 20240101-20240115 仅基准 含策略"
        )
        self.assertEqual(mode, "on")

    def test_two_dates_rejected(self):
        with self.assertRaises(ValueError):
            parse_stock_backtest_tail("600519 20240101 20240630")


class TestEquityBacktestEngine(unittest.TestCase):
    def test_short_auto_skips_strategies(self):
        hist = _mock_history(5)
        report = build_equity_backtest_report(
            hist,
            ts_code="600519.SH",
            start_date="20240101",
            end_date="20240105",
            config=EquityBacktestConfig(strategy_mode=StrategyMode.AUTO),
        )
        self.assertEqual(report.meta.trading_days, 5)
        self.assertIsNotNone(report.buy_hold)
        self.assertEqual(len(report.strategies), 0)
        self.assertIsNotNone(report.strategy_skip_note)
        text = format_equity_backtest_report(report)
        self.assertIn("含策略", text)

    def test_short_off_no_strategies(self):
        hist = _mock_history(3)
        report = build_equity_backtest_report(
            hist,
            ts_code="600519.SH",
            start_date="20240101",
            end_date="20240103",
            config=EquityBacktestConfig(strategy_mode=StrategyMode.OFF),
        )
        self.assertEqual(len(report.strategies), 0)
        self.assertIn("仅基准", report.strategy_skip_note or "")

    def test_short_on_tries_strategies(self):
        hist = _mock_history(30)
        report = build_equity_backtest_report(
            hist,
            ts_code="600519.SH",
            start_date="20240101",
            end_date="20240130",
            config=EquityBacktestConfig(strategy_mode=StrategyMode.ON),
        )
        self.assertGreater(len(report.strategies), 0)

    def test_long_auto_has_strategies(self):
        hist = _mock_history(MIN_BARS_ALL_STRATEGIES)
        report = build_equity_backtest_report(
            hist,
            ts_code="600519.SH",
            start_date="20240101",
            end_date="20240228",
            config=EquityBacktestConfig(strategy_mode=StrategyMode.AUTO),
        )
        self.assertGreater(len(report.strategies), 0)
        self.assertIsNone(report.strategy_skip_note)

    def test_two_days_no_performance_block(self):
        hist = _mock_history(2)
        report = build_equity_backtest_report(
            hist,
            ts_code="600519.SH",
            start_date="20240101",
            end_date="20240102",
        )
        self.assertIsNone(report.buy_hold)
        text = format_equity_backtest_report(report)
        self.assertIn("买入持有涨跌", text)

    def test_filter_range(self):
        hist = _mock_history(10)
        out = filter_history_by_date_range(hist, "20240103", "20240108")
        self.assertEqual(len(out), 6)


if __name__ == "__main__":
    unittest.main()
