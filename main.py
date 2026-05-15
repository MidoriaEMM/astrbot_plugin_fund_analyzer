"""
AstrBot 基金数据分析插件
使用 AKShare 开源库获取基金数据，进行分析和展示
默认分析：国投瑞银白银期货(LOF)A (代码: 161226)
"""

import asyncio
import json
import math
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.utils.t2i.renderer import HtmlRenderer

# 导入股票分析模块
from .stock import StockAnalyzer, StockInfo
from .stock.exchange_filter import is_effectively_limit_up
from .stock.short_term import (
    SHORT_TERM_BATCH_MAX_CODES,
    SHORT_TERM_MIN_BARS,
    batch_analyze_short_term_by_codes,
    format_short_term_report,
    screen_stocks_short_term,
)
from .stock.position_plan import build_position_plan, format_position_plan_table
from .stock.wyckoff_screen import (
    WYCKOFF_BATCH_MAX_CODES,
    WYCKOFF_MIN_BARS,
    batch_wyckoff_by_codes,
    format_wyckoff_report,
    screen_wyckoff_stocks,
)
from .stock.board_ak import (
    fetch_concept_cons,
    fetch_concept_names_df,
    fetch_etf_spot_df,
    fetch_industry_cons,
    fetch_industry_names_df,
    filter_board_names_by_keyword,
    filter_etf_by_keyword,
)

from .command_parse import (
    DEFAULT_BOARD_DISPLAY_LIMIT,
    DEFAULT_SHORT_TERM_BATCH_TOP,
    MAX_QUANT_STOCK_DEBATE_CAP,
    get_event_plain_text,
    parse_daban_pick_tail,
    parse_daban_check_stock_tail,
    parse_daban_pick_debate_tail,
    parse_stock_backtest_tail,
    parse_keyword_and_limit,
    parse_name_maxscan_top,
    parse_quant_stock_screen_debate_tail,
    parse_quant_stock_screen_position_tail,
    parse_quant_stock_screen_tail,
    parse_stock_smart_analysis_tail,
    parse_short_term_batch_tail,
    parse_short_term_screen_tail,
    parse_wyckoff_batch_tail,
    parse_wyckoff_screen_tail,
    strip_command_prefix,
    ts_code_to_fund_debate_code,
)

# 导入本地图片生成器
from .image_generator import render_fund_image, PLAYWRIGHT_AVAILABLE

# 导入东方财富 API 模块（直接 HTTP 请求，不依赖 akshare）
from .eastmoney_api import get_api as get_eastmoney_api
from .fund_code_parse import parse_fund_code_hint
from .quant_screening import (
    DEFAULT_SCREENING_CONCURRENCY,
    DISCLAIMER,
    MIN_HISTORY_BARS,
    format_screening_plain,
    pairs_from_board_like_df,
    parse_optional_positive_int,
    rank_screening_rows,
    screen_board_like_pairs,
    screen_lof_funds,
    screen_stocks_by_abs_pct,
    screening_report_template_data,
)

# 默认超时时间（秒）- AKShare获取LOF数据需要较长时间
DEFAULT_TIMEOUT = 120  # 2分钟
# 数据缓存有效期（秒）
CACHE_TTL = 1800  # 30分钟


@dataclass
class FundInfo:
    """基金基本信息"""

    code: str  # 基金代码
    name: str  # 基金名称
    latest_price: float  # 最新价
    change_amount: float  # 涨跌额
    change_rate: float  # 涨跌幅
    open_price: float  # 开盘价
    high_price: float  # 最高价
    low_price: float  # 最低价
    prev_close: float  # 昨收
    volume: float  # 成交量
    amount: float  # 成交额
    turnover_rate: float  # 换手率

    @property
    def change_symbol(self) -> str:
        """涨跌符号"""
        if self.change_rate > 0:
            return "📈"
        elif self.change_rate < 0:
            return "📉"
        return "➡️"

    @property
    def trend_emoji(self) -> str:
        """趋势表情"""
        if self.change_rate >= 3:
            return "🚀"
        elif self.change_rate >= 1:
            return "↗️"
        elif self.change_rate > 0:
            return "↑"
        elif self.change_rate <= -3:
            return "💥"
        elif self.change_rate <= -1:
            return "↘️"
        elif self.change_rate < 0:
            return "↓"
        return "➡️"


class FundAnalyzer:
    """基金分析核心类"""

    # 默认基金代码：国投瑞银白银期货(LOF)A
    DEFAULT_FUND_CODE = "161226"
    DEFAULT_FUND_NAME = "国投瑞银白银期货(LOF)A"

    def __init__(self):
        # 使用东方财富 API 模块（不再依赖 akshare）
        self._api = get_eastmoney_api()
        self._initialized = True

    def _safe_float(self, value, default: float = 0.0) -> float:
        """安全地将值转换为float，处理NaN和None"""
        if value is None:
            return default
        try:
            import math

            if isinstance(value, float) and math.isnan(value):
                return default
            result = float(value)
            if math.isnan(result):
                return default
            return result
        except (ValueError, TypeError):
            return default

    async def get_lof_realtime(
        self,
        fund_code: Optional[str] = None,
        *,
        prefer_otc: bool = False,
    ) -> FundInfo | None:
        """
        获取LOF基金实时行情

        Args:
            fund_code: 基金代码，默认为国投瑞银白银期货LOF
            prefer_otc: True=场外估值；False=交易所行情（默认）

        Returns:
            FundInfo 对象或 None
        """
        if fund_code is None:
            fund_code = self.DEFAULT_FUND_CODE

        fund_code = str(fund_code).strip()

        try:
            data = await self._api.get_fund_realtime(fund_code, prefer_otc=prefer_otc)
            if not data:
                logger.warning(f"未找到基金数据: {fund_code}")
                return None

            return FundInfo(
                code=data.get("code", fund_code),
                name=data.get("name", ""),
                latest_price=data.get("latest_price", 0.0),
                change_amount=data.get("change_amount", 0.0),
                change_rate=data.get("change_rate", 0.0),
                open_price=data.get("open_price", 0.0),
                high_price=data.get("high_price", 0.0),
                low_price=data.get("low_price", 0.0),
                prev_close=data.get("prev_close", 0.0),
                volume=data.get("volume", 0.0),
                amount=data.get("amount", 0.0),
                turnover_rate=data.get("turnover_rate", 0.0),
            )
        except Exception as e:
            logger.error(f"获取LOF基金实时行情失败: {e}")
            return None

    async def get_lof_history(
        self,
        fund_code: Optional[str] = None,
        days: int = 30,
        adjust: str = "qfq",
        *,
        prefer_otc: bool = False,
        kline_max_retries: int = 3,
    ) -> list[dict] | None:
        """
        获取LOF基金历史行情

        Args:
            fund_code: 基金代码
            days: 获取天数
            adjust: 复权类型 qfq-前复权, hfq-后复权, ""-不复权
            prefer_otc: 与 get_lof_realtime 一致
            kline_max_retries: 东财场内 K 线 HTTP 最大尝试次数（见 EastMoneyAPI.get_fund_history）

        Returns:
            历史数据列表或 None
        """
        if fund_code is None:
            fund_code = self.DEFAULT_FUND_CODE

        fund_code = str(fund_code).strip()

        try:
            history = await self._api.get_fund_history(
                fund_code,
                days,
                adjust,
                prefer_otc=prefer_otc,
                kline_max_retries=kline_max_retries,
            )
            return history
        except Exception as e:
            logger.error(f"获取LOF基金历史行情失败: {e}")
            return None

    #: 上证指数（东财 secid；勿用六位代码走普通行情以免误判深市）
    SSE_INDEX_SECID = "1.000001"

    async def get_sse_index_history(
        self,
        days: int = 60,
        adjust: str = "qfq",
        *,
        kline_max_retries: int = 1,
    ) -> list[dict] | None:
        """
        上证指数日 K（威科夫/短线「大盘水温」）。
        优先级：Tushare ``index_daily`` → TickFlow 日 K ``000001.SH`` → 东财 ``secid=1.000001``。
        """
        d_need = max(int(days), 1)
        min_required = min(d_need, 5)

        ts_tok = self._api._effective_tushare_token()
        if ts_tok:
            try:
                from .tushare_client import fetch_sse_index_daily_as_eastmoney_history

                hist = await asyncio.to_thread(
                    fetch_sse_index_daily_as_eastmoney_history,
                    ts_tok,
                    days,
                    adjust,
                )
                if hist and len(hist) >= min_required:
                    logger.info(
                        f"上证指数日K 优先 Tushare（大盘水温）: {len(hist)} 条"
                    )
                    return hist
            except Exception as e:
                logger.debug(f"Tushare 上证日K 失败，尝试 Tickflow/东财: {e}")

        tf_key = self._api._effective_tickflow_key()
        if tf_key:
            try:
                from .tickflow_client.index_klines import (
                    fetch_sse_index_daily_as_eastmoney_history as _fetch_sse_tf,
                )

                hist = await asyncio.to_thread(
                    _fetch_sse_tf,
                    tf_key,
                    days,
                    adjust,
                )
                if hist and len(hist) >= min_required:
                    logger.info(
                        f"上证指数日K Tickflow 000001.SH（大盘水温）: {len(hist)} 条"
                    )
                    return hist
            except Exception as e:
                logger.debug(f"Tickflow 上证日K 失败，回退东财: {e}")

        try:
            hist = await self._api.get_kline_history_by_secid(
                self.SSE_INDEX_SECID,
                days,
                adjust,
                max_retries=kline_max_retries,
            )
            if hist:
                logger.debug(
                    f"上证指数日K 东财 secid（大盘水温）: {len(hist)} 条"
                )
            return hist
        except Exception as e:
            logger.debug(f"上证指数 K 线失败: {e}")
            return None

    async def get_cn_equity_a_breadth(self) -> dict[str, Any] | None:
        """
        A 股涨跌广度（与全市场快照列对齐）。
        优先级：Tickflow ``CN_Equity_A`` → Tushare ``rt_k``；失败返回 ``None``。
        """
        tf_key = self._api._effective_tickflow_key()
        if tf_key:
            try:
                from .tickflow_client.breadth import fetch_cn_equity_a_breadth as _tf_breadth

                b = await asyncio.to_thread(_tf_breadth, tf_key)
                if b and int(b.get("valid") or 0) > 0:
                    logger.info("A股涨跌广度 Tickflow CN_Equity_A")
                    return b
            except Exception as e:
                logger.debug(f"Tickflow 涨跌广度失败，尝试 Tushare: {e}")

        ts_tok = self._api._effective_tushare_token()
        if ts_tok:
            try:
                from .tushare_client.breadth import fetch_cn_equity_a_breadth as _ts_breadth

                b = await asyncio.to_thread(_ts_breadth, ts_tok)
                if b and int(b.get("valid") or 0) > 0:
                    logger.info("A股涨跌广度 Tushare rt_k")
                    return b
            except Exception as e:
                logger.debug(f"Tushare 涨跌广度失败: {e}")

        return None

    async def search_fund(self, keyword: str) -> list[dict]:
        """
        搜索LOF基金

        Args:
            keyword: 搜索关键词（基金名称或代码）

        Returns:
            匹配的基金列表
        """
        try:
            results = await self._api.search_fund(keyword)
            return results
        except Exception as e:
            logger.error(f"搜索基金失败: {e}")
            return []

    def calculate_technical_indicators(
        self, history_data: list[dict]
    ) -> dict[str, Any]:
        """
        计算技术指标（委托给 quant.py 中的完整实现）

        Args:
            history_data: 历史数据列表

        Returns:
            技术指标字典
        """
        if not history_data or len(history_data) < 5:
            return {}

        # 使用 quant.py 中的量化分析器
        from .ai_analyzer.quant import QuantAnalyzer

        quant = QuantAnalyzer()
        indicators = quant.calculate_all_indicators(history_data)
        perf = quant.calculate_performance(history_data)

        closes = [d["close"] for d in history_data]
        current_price = closes[-1] if closes else 0

        # 计算区间收益率
        def calc_return(days):
            if len(closes) > days:
                prev = closes[-(days + 1)]
                if prev != 0:
                    return (current_price - prev) / prev * 100
            return None

        # 转换为兼容格式
        return {
            "ma5": round(indicators.ma5, 4) if indicators.ma5 else None,
            "ma10": round(indicators.ma10, 4) if indicators.ma10 else None,
            "ma20": round(indicators.ma20, 4) if indicators.ma20 else None,
            "return_5d": calc_return(5),
            "return_10d": calc_return(10),
            "return_20d": calc_return(20),
            "volatility": perf.volatility if perf else None,
            "high_20d": max(closes[-20:]) if len(closes) >= 20 else max(closes),
            "low_20d": min(closes[-20:]) if len(closes) >= 20 else min(closes),
            "trend": indicators.signal,
            "current_price": current_price,
        }


# 贵金属价格缓存TTL（15分钟）
METAL_CACHE_TTL = 900


@register(
    "astrbot_plugin_fund_analyzer",
    "2529huang",
    "基金数据分析插件 - 使用AKShare获取LOF/ETF基金数据",
    "1.0.0",
)
class FundAnalyzerPlugin(Star):
    """基金分析插件主类"""

    # 用户设置文件名
    SETTINGS_FILE = "user_settings.json"

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.analyzer = FundAnalyzer()
        tf_key = (config.get("tickflow_api_key") or "").strip()
        ts_token = (config.get("tushare_token") or "").strip()
        # 配置项为空传 None，以便回退读取环境变量 TICKFLOW_API_KEY / TUSHARE_TOKEN
        self.stock_analyzer = StockAnalyzer(
            tickflow_api_key=tf_key if tf_key else None,
            tushare_token=ts_token if ts_token else None,
        )
        em = get_eastmoney_api()
        em.set_tickflow_api_key(tf_key if tf_key else None)
        em.set_tushare_token(ts_token if ts_token else None)
        # 初始化图片渲染器
        self.image_renderer = HtmlRenderer()
        # 是否使用本地图片生成器（优先使用）
        self.use_local_renderer = PLAYWRIGHT_AVAILABLE
        # 延迟初始化 AI 分析器
        self._ai_analyzer = None
        # 获取插件数据目录
        self._data_dir = Path(StarTools.get_data_dir("fund_analyzer"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        # 加载用户设置
        self.user_fund_settings: dict[str, str] = self._load_user_settings()
        # 贵金属价格缓存
        self._metal_cache: dict = {}
        self._metal_cache_time: datetime | None = None
        # 检查依赖
        self._check_dependencies()
        logger.info("基金分析插件已加载")

    def _check_dependencies(self):
        """检查必要依赖是否已安装"""
        try:
            import akshare  # noqa: F401
            import pandas  # noqa: F401
        except ImportError as e:
            logger.warning(
                f"基金分析插件依赖未完全安装: {e}\n请执行: pip install akshare pandas"
            )

    def _load_user_settings(self) -> dict[str, str]:
        """从文件加载用户设置"""
        settings_path = self._data_dir / self.SETTINGS_FILE
        if settings_path.exists():
            try:
                with open(settings_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"加载用户设置失败: {e}")
        return {}

    def _save_user_settings(self):
        """保存用户设置到文件"""
        settings_path = self._data_dir / self.SETTINGS_FILE
        try:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(self.user_fund_settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存用户设置失败: {e}")

    @property
    def ai_analyzer(self):
        """延迟初始化 AI 分析器"""
        if self._ai_analyzer is None:
            from .ai_analyzer import AIFundAnalyzer

            self._ai_analyzer = AIFundAnalyzer(self.context)
        return self._ai_analyzer

    def _get_user_fund(self, user_id: str) -> str:
        """获取用户设置的默认基金代码"""
        return self.user_fund_settings.get(user_id, FundAnalyzer.DEFAULT_FUND_CODE)

    def _normalize_fund_code(self, code: str | int | None) -> str | None:
        """标准化基金代码，补齐前导0到6位

        Args:
            code: 基金代码，可能是字符串、整数或None

        Returns:
            标准化后的6位基金代码字符串，如果输入为None则返回None
        """
        if code is None:
            return None
        # 转换为字符串并去除空格
        code_str = str(code).strip()
        if not code_str:
            return None
        # 补齐前导0到6位
        return code_str.zfill(6)

    def _parse_fund_command_input(self, code: str | int | None, user_id: str):
        """解析指令中的六位代码与场外/场内偏好；未填时使用用户默认基金。

        Returns:
            (fund_code, prefer_otc, explicit_code_or_None)
            explicit_code_or_None：用户未输入有效数字时为 None。
        """
        parsed, prefer = parse_fund_code_hint(code)
        if parsed is not None:
            return parsed, prefer, parsed
        return self._get_user_fund(user_id), False, None

    def _format_fund_info(self, info: FundInfo) -> str:
        """格式化基金信息为文本"""
        # 价格为0通常表示暂无数据（原始数据为NaN）
        if info.latest_price == 0:
            return f"""
📊 【{info.name}】
━━━━━━━━━━━━━━━━━
⚠️ 暂无实时行情数据
━━━━━━━━━━━━━━━━━
🔢 基金代码: {info.code}
💡 可能原因: 停牌/休市/数据源未更新
⏰ 查询时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
""".strip()

        change_color = (
            "🔴" if info.change_rate < 0 else "🟢" if info.change_rate > 0 else "⚪"
        )

        return f"""
📊 【{info.name}】实时行情 {info.trend_emoji}
━━━━━━━━━━━━━━━━━
💰 最新价: {info.latest_price:.4f}
{change_color} 涨跌额: {info.change_amount:+.4f}
{change_color} 涨跌幅: {info.change_rate:+.2f}%
━━━━━━━━━━━━━━━━━
📈 今开: {info.open_price:.4f}
📊 最高: {info.high_price:.4f}
📉 最低: {info.low_price:.4f}
📋 昨收: {info.prev_close:.4f}
━━━━━━━━━━━━━━━━━
📦 成交量: {info.volume:,.0f}
💵 成交额: {info.amount:,.2f}
🔄 换手率: {info.turnover_rate:.2f}%
━━━━━━━━━━━━━━━━━
🔢 基金代码: {info.code}
⏰ 更新时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
""".strip()

    def _format_analysis(self, info: FundInfo, indicators: dict) -> str:
        """格式化技术分析结果"""
        if not indicators:
            return "📊 暂无足够数据进行技术分析"

        trend_emoji = {
            "强势上涨": "🚀",
            "上涨趋势": "📈",
            "强势下跌": "💥",
            "下跌趋势": "📉",
            "震荡": "↔️",
        }.get(indicators.get("trend", "震荡"), "❓")

        ma_status = []
        current = indicators.get("current_price", 0)
        if indicators.get("ma5"):
            status = "上" if current > indicators["ma5"] else "下"
            ma_status.append(f"MA5({indicators['ma5']:.4f}){status}")
        if indicators.get("ma10"):
            status = "上" if current > indicators["ma10"] else "下"
            ma_status.append(f"MA10({indicators['ma10']:.4f}){status}")
        if indicators.get("ma20"):
            status = "上" if current > indicators["ma20"] else "下"
            ma_status.append(f"MA20({indicators['ma20']:.4f}){status}")

        def _fmt_ret(v):
            return "--" if v is None else f"{v:+.2f}"

        def _fmt_4(v):
            return "--" if v is None else f"{v:.4f}"

        return f"""
📈 【{info.name}】技术分析
━━━━━━━━━━━━━━━━━
{trend_emoji} 趋势判断: {indicators.get("trend", "未知")}
━━━━━━━━━━━━━━━━━
📊 均线分析:
  • {" | ".join(ma_status) if ma_status else "数据不足"}
━━━━━━━━━━━━━━━━━
📈 区间收益率:
  • 5日收益: {_fmt_ret(indicators.get("return_5d"))}%
  • 10日收益: {_fmt_ret(indicators.get("return_10d"))}%
  • 20日收益: {_fmt_ret(indicators.get("return_20d"))}%
━━━━━━━━━━━━━━━━━
📉 波动分析:
  • 20日波动率: {_fmt_4(indicators.get("volatility"))}
  • 20日最高: {_fmt_4(indicators.get("high_20d"))}
  • 20日最低: {_fmt_4(indicators.get("low_20d"))}
━━━━━━━━━━━━━━━━━
💡 投资建议: 请结合自身风险承受能力谨慎投资
""".strip()

    def _format_stock_info(self, info: StockInfo) -> str:
        """格式化A股股票信息为文本"""
        # 价格为0通常表示暂无数据
        if info.latest_price == 0:
            return f"""
📊 【{info.name}】
━━━━━━━━━━━━━━━━━
⚠️ 暂无实时行情数据
━━━━━━━━━━━━━━━━━
🔢 股票代码: {info.code}
💡 可能原因: 停牌/休市/数据源未更新
⏰ 查询时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
""".strip()

        change_color = (
            "🔴" if info.change_rate < 0 else "🟢" if info.change_rate > 0 else "⚪"
        )

        # 格式化市值（转换为亿元）
        def format_market_cap(value):
            if value >= 100000000:  # 亿元
                return f"{value / 100000000:.2f}亿"
            elif value >= 10000:  # 万元
                return f"{value / 10000:.2f}万"
            return f"{value:.2f}"

        return f"""
📊 【{info.name}】实时行情 {info.trend_emoji}
━━━━━━━━━━━━━━━━━
💰 最新价: {info.latest_price:.2f}
{change_color} 涨跌额: {info.change_amount:+.2f}
{change_color} 涨跌幅: {info.change_rate:+.2f}%
📏 振幅: {info.amplitude:.2f}%
━━━━━━━━━━━━━━━━━
📈 今开: {info.open_price:.2f}
📊 最高: {info.high_price:.2f}
📉 最低: {info.low_price:.2f}
📋 昨收: {info.prev_close:.2f}
━━━━━━━━━━━━━━━━━
📦 成交量: {info.volume:,.0f}手
💵 成交额: {format_market_cap(info.amount)}
🔄 换手率: {info.turnover_rate:.2f}%
━━━━━━━━━━━━━━━━━
📈 市盈率(动态): {info.pe_ratio:.2f}
📊 市净率: {info.pb_ratio:.2f}
💰 总市值: {format_market_cap(info.total_market_cap)}
💎 流通市值: {format_market_cap(info.circulating_market_cap)}
━━━━━━━━━━━━━━━━━
🔢 股票代码: {info.code}
⏰ 更新时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
💡 数据缓存10分钟，仅供参考
""".strip()

    def _resolve_screening_template_path(self) -> Optional[Path]:
        fn = "screening_report.html"
        p = self._data_dir / "templates" / fn
        if p.exists():
            return p
        p2 = Path(__file__).parent / "templates" / fn
        return p2 if p2.exists() else None

    async def _emit_screening_report(
        self,
        event: AstrMessageEvent,
        *,
        title: str,
        top_rows: list,
        candidate_count: int,
        valid_count: int,
        prefer_image: bool = True,
    ):
        text_fallback = format_screening_plain(
            title=title,
            screened=top_rows,
            candidate_count=candidate_count,
            valid_count=valid_count,
        )
        if not prefer_image:
            yield event.plain_result(text_fallback)
            return
        tpl = self._resolve_screening_template_path()
        if tpl is None:
            yield event.plain_result(text_fallback)
            return
        data = screening_report_template_data(
            title=title,
            screened=top_rows,
            candidate_count=candidate_count,
            valid_count=valid_count,
        )
        with open(tpl, encoding="utf-8") as f:
            template_str = f.read()
        if self.use_local_renderer:
            try:
                img_path = await render_fund_image(
                    template_path=tpl,
                    template_data=data,
                    width=540,
                )
                yield event.image_result(img_path)
                return
            except Exception as e:
                logger.warning(f"量化精选本地渲染失败，尝试网络渲染: {e}")
                try:
                    img_url = await self.image_renderer.render_custom_template(
                        tmpl_str=template_str,
                        tmpl_data=data,
                        return_url=True,
                    )
                    yield event.image_result(img_url)
                    return
                except Exception as e2:
                    logger.warning(f"量化精选网络渲染失败，降级文本: {e2}")
                    yield event.plain_result(text_fallback)
                    return
        try:
            img_url = await self.image_renderer.render_custom_template(
                tmpl_str=template_str,
                tmpl_data=data,
                return_url=True,
            )
            yield event.image_result(img_url)
        except Exception as e:
            logger.warning(f"量化精选网络渲染失败，降级文本: {e}")
            yield event.plain_result(text_fallback)

    async def _board_quant_pipeline(
        self,
        event: AstrMessageEvent,
        *,
        title: str,
        pairs: list,
        top_n: int,
    ):
        attempted = len(pairs)
        if attempted == 0:
            yield event.plain_result("⚠️ 无有效证券代码可分析。")
            return
        raw = await screen_board_like_pairs(
            self.analyzer, pairs, max_concurrent=DEFAULT_SCREENING_CONCURRENCY
        )
        if not raw:
            yield event.plain_result(
                f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本（<{MIN_HISTORY_BARS} 根），无法排序。"
                + DISCLAIMER
            )
            return
        ranked = rank_screening_rows(raw)
        top_rows = ranked[:top_n]
        async for msg in self._emit_screening_report(
            event,
            title=title,
            top_rows=top_rows,
            candidate_count=attempted,
            valid_count=len(raw),
        ):
            yield msg

    async def _fetch_precious_metal_prices(self) -> dict:
        """
        从NowAPI获取上海黄金交易所贵金属价格
        返回包含金价和银价的字典
        API文档: https://www.nowapi.com/api/finance.shgold
        黄金使用1301，白银使用1302，需分开调用
        缓存15分钟
        """
        import aiohttp

        # 检查缓存是否有效（15分钟）
        now = datetime.now()
        if (
            self._metal_cache
            and self._metal_cache_time is not None
            and (now - self._metal_cache_time).total_seconds() < METAL_CACHE_TTL
        ):
            logger.debug("使用贵金属价格缓存")
            return self._metal_cache

        # NowAPI 接口配置
        api_url = "http://api.k780.com/"
        base_params = {
            "app": "finance.gold_price",
            "appkey": "78365",
            "sign": "776f93b557ce6e6afeb860b103a587c7",
            "format": "json",
        }

        prices = {}

        async def fetch_metal(gold_id: str, key: str, name: str) -> dict | None:
            """获取单个金属品种的价格"""
            params = {**base_params, "goldid": gold_id}
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        api_url, params=params, timeout=aiohttp.ClientTimeout(total=30)
                    ) as response:
                        if response.status != 200:
                            logger.error(f"获取{name}价格失败: HTTP {response.status}")
                            return None

                        data = await response.json()

                        if data.get("success") != "1":
                            error_msg = data.get("msg", "未知错误")
                            logger.error(f"NowAPI返回错误({name}): {error_msg}")
                            return None

                        result = data.get("result", {})
                        dt_list = result.get("dtList", {})

                        if gold_id in dt_list:
                            metal_data = dt_list[gold_id]
                            return {
                                "name": metal_data.get("varietynm", name),
                                "variety": metal_data.get("variety", ""),
                                "price": float(metal_data.get("last_price", 0) or 0),
                                "buy_price": float(metal_data.get("buy_price", 0) or 0),
                                "sell_price": float(
                                    metal_data.get("sell_price", 0) or 0
                                ),
                                "high": float(metal_data.get("high_price", 0) or 0),
                                "low": float(metal_data.get("low_price", 0) or 0),
                                "open": float(metal_data.get("open_price", 0) or 0),
                                "prev_close": float(
                                    metal_data.get("yesy_price", 0) or 0
                                ),
                                "change": float(metal_data.get("change_price", 0) or 0),
                                "change_rate": metal_data.get("change_margin", "0%"),
                                "update_time": metal_data.get("uptime", ""),
                            }
                        return None
            except Exception as e:
                logger.error(f"获取{name}价格出错: {e}")
                return None

        try:
            # 分开调用黄金(1301)和白银(1302)
            gold_data = await fetch_metal("1051", "au_td", "黄金")
            if gold_data:
                prices["au_td"] = gold_data

            silver_data = await fetch_metal("1052", "ag_td", "白银")
            if silver_data:
                prices["ag_td"] = silver_data

            # 更新缓存
            if prices:
                self._metal_cache = prices
                self._metal_cache_time = now
                logger.info("贵金属价格已更新并缓存15分钟")

            return prices

        except Exception as e:
            logger.error(f"获取贵金属价格出错: {e}")
            # 如果有旧缓存，返回旧数据
            if self._metal_cache:
                logger.info("使用过期的贵金属缓存数据")
                return self._metal_cache
            return {}

    def _format_precious_metal_prices(self, prices: dict) -> str:
        """格式化贵金属价格信息"""
        if not prices:
            return "❌ 获取贵金属价格失败，请稍后重试"

        def parse_change_rate(rate_str: str) -> float:
            """解析涨跌幅字符串，如 '1.5%' -> 1.5"""
            try:
                return float(rate_str.replace("%", "").replace("+", ""))
            except (ValueError, AttributeError):
                return 0.0

        def format_item(
            data: dict, unit: str = "美元/盎司", divisor: float = 1.0
        ) -> str:
            """格式化单个金属品种的价格信息

            Args:
                data: 价格数据字典
                unit: 显示单位
                divisor: 除数，用于单位转换（如白银可能需要除以100）
            """
            if not data:
                return "  暂无数据"

            change_rate = parse_change_rate(data.get("change_rate", "0%"))
            change_emoji = (
                "🔴" if change_rate < 0 else "🟢" if change_rate > 0 else "⚪"
            )
            trend_emoji = "📈" if change_rate > 0 else "📉" if change_rate < 0 else "➡️"

            # 应用单位转换
            price = data["price"] / divisor
            change = data.get("change", 0) / divisor
            open_p = data.get("open", 0) / divisor
            high_p = data.get("high", 0) / divisor
            low_p = data.get("low", 0) / divisor
            buy_p = data.get("buy_price", 0) / divisor
            sell_p = data.get("sell_price", 0) / divisor

            return f"""  {trend_emoji} 最新价: {price:.2f} {unit}
  {change_emoji} 涨跌: {change:+.2f} ({data.get("change_rate", "0%")})
  📊 今开: {open_p:.2f} | 最高: {high_p:.2f} | 最低: {low_p:.2f}
  💹 买入: {buy_p:.2f} | 卖出: {sell_p:.2f}"""

        lines = [
            "💰 今日贵金属行情（国际现货）",
            "━━━━━━━━━━━━━━━━━",
        ]

        # 黄金 - 国际金价，单位是美元/盎司
        if "au_td" in prices:
            lines.append("🥇 黄金")
            lines.append(format_item(prices["au_td"], "美元/盎司", 1.0))
            if prices["au_td"].get("update_time"):
                lines.append(f"  🕐 更新: {prices['au_td']['update_time']}")
            lines.append("")

        # 白银 - 国际银价，API返回的是美分/盎司，需要除以100转为美元/盎司
        if "ag_td" in prices:
            lines.append("🥈 白银")
            # 白银价格如果大于1000，说明是美分/盎司，需要除以100
            silver_price = prices["ag_td"].get("price", 0)
            divisor = 100.0 if silver_price > 1000 else 1.0
            lines.append(format_item(prices["ag_td"], "美元/盎司", divisor))
            if prices["ag_td"].get("update_time"):
                lines.append(f"  🕐 更新: {prices['ag_td']['update_time']}")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append("📌 国际现货24小时交易")
        lines.append("💡 数据来源: NowAPI | 缓存15分钟")

        return "\n".join(lines)

    _BOARD_NOTE = "⚠️ 以上内容仅供参考，不构成投资建议。"

    @staticmethod
    def _fmt_num_cell(v: Any, fmt: str, default: str = "-") -> str:
        try:
            x = float(v)
            if math.isnan(x):
                return default
            return fmt % x
        except (TypeError, ValueError):
            return default

    def _format_board_cons_plain(
        self, df: Any, display_limit: int, header: str
    ) -> str:
        total = len(df)
        n = min(display_limit, total)
        sub = df.iloc[:n]
        lines = [
            header,
            f"全量 {total} 只，下列展示 {n} 只",
            "━━━━━━━━━━━━━━━━━",
        ]
        for i in range(n):
            row = sub.iloc[i]
            code = str(row.get("代码", "") or "").strip()
            name = str(row.get("名称", "") or "").strip()
            price = self._fmt_num_cell(row.get("最新价"), "%.3f")
            pct = self._fmt_num_cell(row.get("涨跌幅"), "%+.2f%%")
            lines.append(f"{i + 1}. {name} ({code})  {price}  {pct}")
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append("💡 使用「股票 代码」查看单只行情")
        lines.append(self._BOARD_NOTE)
        return "\n".join(lines)

    def _format_board_name_hits_plain(
        self, df: Any, display_limit: int, header: str
    ) -> str:
        total = len(df)
        n = min(display_limit, total)
        sub = df.iloc[:n]
        lines = [
            header,
            f"命中 {total} 个板块，下列展示 {n} 个",
            "━━━━━━━━━━━━━━━━━",
        ]
        name_col = "板块名称" if "板块名称" in sub.columns else None
        code_col = "板块代码" if "板块代码" in sub.columns else None
        pct_col = "涨跌幅" if "涨跌幅" in sub.columns else None
        for i in range(n):
            row = sub.iloc[i]
            nm = str(row.get(name_col, "") or "").strip() if name_col else ""
            cd = str(row.get(code_col, "") or "").strip() if code_col else ""
            pct = (
                self._fmt_num_cell(row.get(pct_col), "%+.2f%%")
                if pct_col
                else "-"
            )
            lines.append(f"{i + 1}. {nm} ({cd})  {pct}")
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append("💡 复制准确板块名后使用「板块概念」或「板块行业」")
        lines.append(self._BOARD_NOTE)
        return "\n".join(lines)

    def _format_etf_hits_plain(
        self, df: Any, display_limit: int, header: str
    ) -> str:
        total = len(df)
        n = min(display_limit, total)
        sub = df.iloc[:n]
        lines = [
            header,
            f"命中 {total} 只，下列展示 {n} 只",
            "━━━━━━━━━━━━━━━━━",
        ]
        for i in range(n):
            row = sub.iloc[i]
            code = str(row.get("代码", "") or "").strip()
            name = str(row.get("名称", "") or "").strip()
            price = self._fmt_num_cell(row.get("最新价"), "%.3f")
            pct = self._fmt_num_cell(row.get("涨跌幅"), "%+.2f%%")
            lines.append(f"{i + 1}. {name} ({code})  {price}  {pct}")
        lines.append("━━━━━━━━━━━━━━━━━")
        lines.append("💡 使用「基金 代码」查看场内基金行情（六位代码）")
        lines.append(self._BOARD_NOTE)
        return "\n".join(lines)

    @filter.command("今日行情")
    async def today_market(self, event: AstrMessageEvent):
        """
        查询今日贵金属行情
        用法: 今日行情
        返回国际金价、银价及涨跌幅
        """
        try:
            yield event.plain_result("🔍 正在获取今日贵金属行情...")

            prices = await self._fetch_precious_metal_prices()

            if prices:
                yield event.plain_result(self._format_precious_metal_prices(prices))
            else:
                yield event.plain_result("❌ 获取贵金属行情失败，请稍后重试")

        except Exception as e:
            logger.error(f"获取今日行情出错: {e}")
            yield event.plain_result(f"❌ 获取行情失败: {str(e)}")

    @filter.command("股票")
    async def stock_query(self, event: AstrMessageEvent, code: str = ""):
        """
        查询A股实时行情
        用法: 股票 <股票代码>
        示例: 股票 000001
        示例: 股票 600519
        """
        try:
            if not code:
                yield event.plain_result(
                    "❌ 请输入股票代码\n"
                    "💡 用法: 股票 <股票代码>\n"
                    "💡 示例: 股票 000001 (平安银行)\n"
                    "💡 示例: 股票 600519 (贵州茅台)"
                )
                return

            stock_code = str(code).strip().zfill(6)
            yield event.plain_result(f"🔍 正在查询股票 {stock_code} 的实时行情...")

            info = await self.stock_analyzer.get_stock_realtime(stock_code)

            if info:
                yield event.plain_result(self._format_stock_info(info))
            else:
                yield event.plain_result(
                    f"❌ 未找到股票代码 {stock_code}\n"
                    "💡 请使用「搜索股票 关键词」来搜索正确的股票代码\n"
                    "💡 示例: 搜索股票 茅台"
                )

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"查询股票行情出错: {e}")
            yield event.plain_result(f"❌ 查询失败: {str(e)}")

    @filter.command("搜索股票")
    async def search_stock(self, event: AstrMessageEvent, keyword: str = ""):
        """
        搜索A股股票
        用法: 搜索股票 <关键词>
        示例: 搜索股票 茅台
        """
        try:
            if not keyword:
                yield event.plain_result(
                    "❌ 请输入搜索关键词\n"
                    "💡 用法: 搜索股票 <关键词>\n"
                    "💡 示例: 搜索股票 茅台"
                )
                return

            yield event.plain_result(f"🔍 正在搜索包含 '{keyword}' 的股票...")

            results = await self.stock_analyzer.search_stock(keyword)

            if not results:
                yield event.plain_result(f"❌ 未找到包含 '{keyword}' 的股票")
                return

            # 格式化搜索结果
            lines = [f"🔍 搜索结果: '{keyword}'", "━━━━━━━━━━━━━━━━━"]
            for i, stock in enumerate(results, 1):
                change_emoji = (
                    "🔴"
                    if stock["change_rate"] < 0
                    else "🟢"
                    if stock["change_rate"] > 0
                    else "⚪"
                )
                lines.append(
                    f"{i}. {stock['name']} ({stock['code']})\n"
                    f"   💰 {stock['price']:.2f} {change_emoji} {stock['change_rate']:+.2f}%"
                )
            lines.append("━━━━━━━━━━━━━━━━━")
            lines.append("💡 使用「股票 代码」查看详细行情")

            yield event.plain_result("\n".join(lines))

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"搜索股票出错: {e}")
            yield event.plain_result(f"❌ 搜索失败: {str(e)}")

    @filter.command("板块概念")
    async def board_concept_cons_cmd(self, event: AstrMessageEvent):
        """
        东方财富概念板块成份股。
        板块概念 <名称> [展示条数]
        """
        try:
            text = get_event_plain_text(event)
            tail = strip_command_prefix(text, "板块概念")
            name, limit = parse_keyword_and_limit(
                tail, default_limit=DEFAULT_BOARD_DISPLAY_LIMIT
            )
            if not name:
                yield event.plain_result(
                    "❌ 请输入东财概念板块名称\n"
                    "💡 用法: 板块概念 <名称> [展示条数]\n"
                    "💡 示例: 板块概念 车联网 50\n"
                    "💡 不知准确名称时可先用「搜索板块概念 关键词」"
                )
                return
            yield event.plain_result(f"🔍 正在拉取概念「{name}」成份股…")
            df = await fetch_concept_cons(name)
            if df is None:
                yield event.plain_result(
                    f"❌ 未找到概念「{name}」或暂无成份数据。\n"
                    "💡 请用「搜索板块概念」核对与东财一致的板块名称，"
                    "亦可用 BK 开头的板块代码。"
                )
                return
            out = self._format_board_cons_plain(
                df, limit, f"📗 概念板块「{name}」成份"
            )
            yield event.plain_result(out)
        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError:
            yield event.plain_result(
                "⏰ 东财接口超时\n💡 请稍后再试或缩短展示条数"
            )
        except Exception as e:
            logger.error(f"板块概念出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("板块行业")
    async def board_industry_cons_cmd(self, event: AstrMessageEvent):
        """
        东方财富行业板块成份股。
        板块行业 <名称> [展示条数]
        """
        try:
            text = get_event_plain_text(event)
            tail = strip_command_prefix(text, "板块行业")
            name, limit = parse_keyword_and_limit(
                tail, default_limit=DEFAULT_BOARD_DISPLAY_LIMIT
            )
            if not name:
                yield event.plain_result(
                    "❌ 请输入东财行业板块名称\n"
                    "💡 用法: 板块行业 <名称> [展示条数]\n"
                    "💡 示例: 板块行业 小金属 40\n"
                    "💡 不知准确名称时可先用「搜索板块行业 关键词」"
                )
                return
            yield event.plain_result(f"🔍 正在拉取行业「{name}」成份股…")
            df = await fetch_industry_cons(name)
            if df is None:
                yield event.plain_result(
                    f"❌ 未找到行业「{name}」或暂无成份数据。\n"
                    "💡 请用「搜索板块行业」核对与东财一致的板块名称，"
                    "亦可用 BK 开头的板块代码。"
                )
                return
            out = self._format_board_cons_plain(
                df, limit, f"📘 行业板块「{name}」成份"
            )
            yield event.plain_result(out)
        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError:
            yield event.plain_result(
                "⏰ 东财接口超时\n💡 请稍后再试或缩短展示条数"
            )
        except Exception as e:
            logger.error(f"板块行业出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("搜索板块概念")
    async def search_board_concept_cmd(self, event: AstrMessageEvent):
        try:
            text = get_event_plain_text(event)
            tail = strip_command_prefix(text, "搜索板块概念")
            kw, limit = parse_keyword_and_limit(
                tail, default_limit=DEFAULT_BOARD_DISPLAY_LIMIT
            )
            if not kw:
                yield event.plain_result(
                    "❌ 请输入关键词\n"
                    "💡 用法: 搜索板块概念 <关键词> [展示条数]\n"
                    "💡 示例: 搜索板块概念 芯片 30"
                )
                return
            yield event.plain_result(f"🔍 正在匹配概念板块「{kw}」…")
            base = await fetch_concept_names_df()
            if base is None:
                yield event.plain_result("❌ 未能获取概念板块列表，请稍后再试")
                return
            hit = filter_board_names_by_keyword(base, kw)
            if hit is None or len(hit) == 0:
                yield event.plain_result(f"❌ 未匹配到含「{kw}」的概念板块")
                return
            out = self._format_board_name_hits_plain(
                hit, limit, f"🔎 概念板块搜索「{kw}」"
            )
            yield event.plain_result(out)
        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError:
            yield event.plain_result(
                "⏰ 东财接口超时\n💡 请稍后再试"
            )
        except Exception as e:
            logger.error(f"搜索板块概念出错: {e}")
            yield event.plain_result(f"❌ 搜索失败: {e}")

    @filter.command("搜索板块行业")
    async def search_board_industry_cmd(self, event: AstrMessageEvent):
        try:
            text = get_event_plain_text(event)
            tail = strip_command_prefix(text, "搜索板块行业")
            kw, limit = parse_keyword_and_limit(
                tail, default_limit=DEFAULT_BOARD_DISPLAY_LIMIT
            )
            if not kw:
                yield event.plain_result(
                    "❌ 请输入关键词\n"
                    "💡 用法: 搜索板块行业 <关键词> [展示条数]\n"
                    "💡 示例: 搜索板块行业 电力 30"
                )
                return
            yield event.plain_result(f"🔍 正在匹配行业板块「{kw}」…")
            base = await fetch_industry_names_df()
            if base is None:
                yield event.plain_result("❌ 未能获取行业板块列表，请稍后再试")
                return
            hit = filter_board_names_by_keyword(base, kw)
            if hit is None or len(hit) == 0:
                yield event.plain_result(f"❌ 未匹配到含「{kw}」的行业板块")
                return
            out = self._format_board_name_hits_plain(
                hit, limit, f"🔎 行业板块搜索「{kw}」"
            )
            yield event.plain_result(out)
        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError:
            yield event.plain_result(
                "⏰ 东财接口超时\n💡 请稍后再试"
            )
        except Exception as e:
            logger.error(f"搜索板块行业出错: {e}")
            yield event.plain_result(f"❌ 搜索失败: {e}")

    @filter.command("搜索ETF")
    async def search_etf_cmd(self, event: AstrMessageEvent):
        try:
            text = get_event_plain_text(event)
            tail = strip_command_prefix(text, "搜索ETF")
            kw, limit = parse_keyword_and_limit(
                tail, default_limit=DEFAULT_BOARD_DISPLAY_LIMIT
            )
            if not kw:
                yield event.plain_result(
                    "❌ 请输入关键词（名称或代码子串）\n"
                    "💡 用法: 搜索ETF <关键词> [展示条数]\n"
                    "💡 示例: 搜索ETF 红利 25"
                )
                return
            yield event.plain_result(f"🔍 正在筛选场内 ETF「{kw}」…")
            base = await fetch_etf_spot_df()
            if base is None:
                yield event.plain_result("❌ 未能获取 ETF 列表，请稍后再试")
                return
            hit = filter_etf_by_keyword(base, kw)
            if hit is None or len(hit) == 0:
                yield event.plain_result(f"❌ 未匹配到含「{kw}」的 ETF")
                return
            out = self._format_etf_hits_plain(hit, limit, f"🔎 ETF 搜索「{kw}」")
            yield event.plain_result(out)
        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError:
            yield event.plain_result(
                "⏰ 东财接口超时\n💡 请稍后再试"
            )
        except Exception as e:
            logger.error(f"搜索ETF出错: {e}")
            yield event.plain_result(f"❌ 搜索失败: {e}")

    @filter.command("板块量化概念")
    async def board_quant_concept_cmd(self, event: AstrMessageEvent):
        """
        概念板块成份拉日线后按综合分排序（|涨跌幅|优先取样）。
        板块量化概念 <名称> [分析上限] [输出条数]
        """
        try:
            text = get_event_plain_text(event)
            tail = strip_command_prefix(text, "板块量化概念")
            name, max_scan, top_n = parse_name_maxscan_top(tail)
            if not name:
                yield event.plain_result(
                    "❌ 请输入东财概念板块名称\n"
                    "💡 用法: 板块量化概念 <名称> [分析上限] [输出条数]\n"
                    "💡 默认分析至多 80 只成份、输出 TOP 10；双数字依次为分析上限与输出条数。\n"
                    "💡 示例: 板块量化概念 车联网 40 5\n"
                    "💡 不知准确名称时可先用「搜索板块概念」"
                )
                return
            yield event.plain_result(
                f"📊 概念「{name}」：将分析至多 {max_scan} 只成份、输出 TOP {top_n}（约需数分钟）…"
            )
            df = await fetch_concept_cons(name)
            if df is None:
                yield event.plain_result(
                    f"❌ 未找到概念「{name}」或暂无成份数据。\n"
                    "💡 请用「搜索板块概念」核对与东财一致的板块名称。"
                )
                return
            pairs = pairs_from_board_like_df(df, max_scan)
            async for msg in self._board_quant_pipeline(
                event,
                title=f"📈 概念板块「{name}」成份量化排序（|涨跌幅|优先取样）",
                pairs=pairs,
                top_n=top_n,
            ):
                yield msg
        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError:
            yield event.plain_result(
                "⏰ 东财接口超时\n💡 请稍后再试或减少分析上限"
            )
        except Exception as e:
            logger.error(f"板块量化概念出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("板块量化行业")
    async def board_quant_industry_cmd(self, event: AstrMessageEvent):
        """
        行业板块成份拉日线后按综合分排序（|涨跌幅|优先取样）。
        """
        try:
            text = get_event_plain_text(event)
            tail = strip_command_prefix(text, "板块量化行业")
            name, max_scan, top_n = parse_name_maxscan_top(tail)
            if not name:
                yield event.plain_result(
                    "❌ 请输入东财行业板块名称\n"
                    "💡 用法: 板块量化行业 <名称> [分析上限] [输出条数]\n"
                    "💡 默认分析至多 80 只成份、输出 TOP 10。\n"
                    "💡 示例: 板块量化行业 半导体 50 8\n"
                    "💡 不知准确名称时可先用「搜索板块行业」"
                )
                return
            yield event.plain_result(
                f"📊 行业「{name}」：将分析至多 {max_scan} 只成份、输出 TOP {top_n}（约需数分钟）…"
            )
            df = await fetch_industry_cons(name)
            if df is None:
                yield event.plain_result(
                    f"❌ 未找到行业「{name}」或暂无成份数据。\n"
                    "💡 请用「搜索板块行业」核对与东财一致的板块名称。"
                )
                return
            pairs = pairs_from_board_like_df(df, max_scan)
            async for msg in self._board_quant_pipeline(
                event,
                title=f"📈 行业板块「{name}」成份量化排序（|涨跌幅|优先取样）",
                pairs=pairs,
                top_n=top_n,
            ):
                yield msg
        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError:
            yield event.plain_result(
                "⏰ 东财接口超时\n💡 请稍后再试或减少分析上限"
            )
        except Exception as e:
            logger.error(f"板块量化行业出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("板块量化ETF")
    async def board_quant_etf_cmd(self, event: AstrMessageEvent):
        """
        在 fund_etf_spot_em 中按名称/代码子串筛选 ETF，再批量量化排序（非严格指数/概念成份）。
        """
        try:
            text = get_event_plain_text(event)
            tail = strip_command_prefix(text, "板块量化ETF")
            kw, max_scan, top_n = parse_name_maxscan_top(tail)
            if not kw:
                yield event.plain_result(
                    "❌ 请输入关键词（ETF 简称或代码子串，常与板块名相同）\n"
                    "💡 用法: 板块量化ETF <关键词> [分析上限] [输出条数]\n"
                    "💡 默认分析至多 80 只、输出 TOP 10；结果为名称匹配筛选，非严格板块成份。\n"
                    "💡 示例: 板块量化ETF 红利 30 5"
                )
                return
            yield event.plain_result(
                f"📊 ETF 关键词「{kw}」：将分析至多 {max_scan} 只、输出 TOP {top_n}（约需数分钟）…"
            )
            base = await fetch_etf_spot_df()
            if base is None:
                yield event.plain_result("❌ 未能获取 ETF 列表，请稍后再试")
                return
            hit = filter_etf_by_keyword(base, kw)
            if hit is None or len(hit) == 0:
                yield event.plain_result(
                    f"❌ 未匹配到名称或代码含「{kw}」的场内 ETF。\n"
                    "💡 可先用「搜索ETF」试其他关键词。"
                )
                return
            pairs = pairs_from_board_like_df(hit, max_scan)
            async for msg in self._board_quant_pipeline(
                event,
                title=f"📈 场内 ETF「{kw}」名称匹配 · 量化排序（非严格成份）",
                pairs=pairs,
                top_n=top_n,
            ):
                yield msg
        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError:
            yield event.plain_result(
                "⏰ 东财接口超时\n💡 请稍后再试或减少分析上限"
            )
        except Exception as e:
            logger.error(f"板块量化ETF出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("基金")
    async def fund_query(self, event: AstrMessageEvent, code: str = ""):
        """
        查询基金实时行情
        用法: 基金 [基金代码]
        示例: 基金 161226
        """
        try:
            user_id = event.get_sender_id()
            fund_code, prefer_otc, normalized_code = self._parse_fund_command_input(
                code, user_id
            )

            yield event.plain_result(f"🔍 正在查询基金 {fund_code} 的实时行情...")

            info = await self.analyzer.get_lof_realtime(
                fund_code, prefer_otc=prefer_otc
            )

            if info:
                yield event.plain_result(self._format_fund_info(info))
            else:
                # 区分是基金代码错误还是数据源问题
                if not normalized_code:
                    yield event.plain_result(f"❌ 基金代码不能为空")
                    return

                # 如果代码是6位数字，通常是有效的基金代码格式，但未找到数据
                if len(normalized_code) == 6 and normalized_code.isdigit():
                    # 尝试再次搜索确认是否存在
                    try:
                        search_res = await self.analyzer.search_fund(normalized_code)
                        if not search_res:
                            yield event.plain_result(
                                f"❌ 未找到基金代码 {fund_code}\n"
                                "💡 请检查代码是否正确，或使用「搜索基金 关键词」查找"
                            )
                            return
                    except Exception:
                        pass  # 搜索出错忽略，继续下面的判断

                yield event.plain_result(
                    f"⚠️ 暂时无法获取基金 {fund_code} 的数据\n"
                    "💡 可能是数据源暂时不可用，或该基金为非LOF基金\n"
                    "💡 请稍后重试"
                )

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"查询基金行情出错: {e}")
            yield event.plain_result(f"❌ 查询失败: {str(e)}")

    @filter.command("基金分析")
    async def fund_analysis(self, event: AstrMessageEvent, code: str = ""):
        """
        基金技术分析
        用法: 基金分析 [基金代码]
        示例: 基金分析 161226
        """
        try:
            user_id = event.get_sender_id()
            fund_code, prefer_otc, normalized_code = self._parse_fund_command_input(
                code, user_id
            )

            yield event.plain_result(f"📊 正在生成基金 {fund_code} 分析报告...")

            # 获取实时行情
            info = await self.analyzer.get_lof_realtime(
                fund_code, prefer_otc=prefer_otc
            )
            if not info:
                # 区分是基金代码错误还是数据源问题
                if not normalized_code:
                    yield event.plain_result(f"❌ 基金代码不能为空")
                    return

                # 如果代码是6位数字，通常是有效的基金代码格式，但未找到数据
                if len(normalized_code) == 6 and normalized_code.isdigit():
                    # 尝试再次搜索确认是否存在
                    try:
                        search_res = await self.analyzer.search_fund(normalized_code)
                        if not search_res:
                            yield event.plain_result(
                                f"❌ 未找到基金代码 {fund_code}\n"
                                "💡 请检查代码是否正确，或使用「搜索基金 关键词」查找"
                            )
                            return
                    except Exception:
                        pass  # 搜索出错忽略，继续下面的判断

                yield event.plain_result(
                    f"⚠️ 暂时无法获取基金 {fund_code} 的数据\n"
                    "💡 可能是数据源暂时不可用，或该基金为非LOF基金\n"
                    "💡 请稍后重试"
                )
                return

            # 获取历史数据进行分析
            history = await self.analyzer.get_lof_history(
                fund_code, days=30, prefer_otc=prefer_otc
            )

            # 计算技术指标
            indicators = {}
            if history:
                indicators = self.analyzer.calculate_technical_indicators(history)
                # 绘制小图用于报告
                plot_img = await asyncio.to_thread(
                    self._plot_history_chart, history, info.name
                )
            else:
                plot_img = None

            # 准备模板数据
            ma_data = []
            if indicators:
                for ma in ["ma5", "ma10", "ma20"]:
                    if indicators.get(ma):
                        ma_data.append({"name": ma.upper(), "value": indicators[ma]})

            data = {
                "fund_name": info.name,
                "fund_code": info.code,
                "latest_price": info.latest_price,
                "change_amount": info.change_amount,
                "change_rate": info.change_rate,
                "plot_img": plot_img,
                "trend": indicators.get("trend", "数据不足"),
                "volatility": indicators.get("volatility"),
                "return_5d": indicators.get("return_5d"),
                "return_10d": indicators.get("return_10d"),
                "return_20d": indicators.get("return_20d"),
                "ma_data": ma_data,
                "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            # 读取模板
            template_path = self._data_dir / "templates" / "analysis_report.html"
            # 如果不在数据目录，尝试检查插件目录
            if not template_path.exists():
                template_path = (
                    Path(__file__).parent / "templates" / "analysis_report.html"
                )

            if not template_path.exists():
                yield event.plain_result(self._format_analysis(info, indicators))
                return

            with open(template_path, "r", encoding="utf-8") as f:
                template_str = f.read()

            if self.use_local_renderer:
                try:
                    img_path = await render_fund_image(
                        template_path=template_path,
                        template_data=data,
                        width=480,
                    )
                    yield event.image_result(img_path)
                except Exception as e:
                    logger.warning(f"基金分析本地渲染失败，尝试网络渲染: {e}")
                    try:
                        img_url = await self.image_renderer.render_custom_template(
                            tmpl_str=template_str,
                            tmpl_data=data,
                            return_url=True,
                        )
                        yield event.image_result(img_url)
                    except Exception as e2:
                        logger.warning(f"基金分析网络渲染失败，降级文本: {e2}")
                        yield event.plain_result(
                            self._format_analysis(info, indicators)
                        )
            else:
                try:
                    img_url = await self.image_renderer.render_custom_template(
                        tmpl_str=template_str,
                        tmpl_data=data,
                        return_url=True,
                    )
                    yield event.image_result(img_url)
                except Exception as e:
                    logger.warning(f"基金分析网络渲染失败，降级文本: {e}")
                    yield event.plain_result(self._format_analysis(info, indicators))

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"基金分析出错: {e}")
            yield event.plain_result(f"❌ 分析失败: {str(e)}")

    def _plot_history_chart(self, history: list[dict], fund_name: str) -> str | None:
        """
        绘制历史行情走势图 (价格+均线+成交量) 并返回 Base64 字符串
        """
        try:
            import base64
            import io
            import matplotlib.pyplot as plt
            import matplotlib.gridspec as gridspec
            import matplotlib.dates as mdates
            import pandas as pd

            # 设置中文字体，防止乱码
            plt.rcParams["font.sans-serif"] = [
                "SimHei",
                "Arial Unicode MS",
                "Microsoft YaHei",
                "WenQuanYi Micro Hei",
                "sans-serif",
            ]
            plt.rcParams["axes.unicode_minus"] = False

            # 准备数据
            df = pd.DataFrame(history)
            if df.empty:
                return None

            df["date"] = pd.to_datetime(df["date"])
            dates = df["date"]
            closes = df["close"]
            volumes = df["volume"]

            # 计算均线
            df["ma5"] = df["close"].rolling(window=5).mean()
            df["ma10"] = df["close"].rolling(window=10).mean()
            df["ma20"] = df["close"].rolling(window=20).mean()

            # 创建画布
            fig = plt.figure(figsize=(10, 6), dpi=100)
            gs = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.15)

            # 主图：价格 + 均线
            ax1 = plt.subplot(gs[0])
            ax1.plot(dates, closes, label="收盘价", color="#333333", linewidth=1.5)
            ax1.plot(
                dates, df["ma5"], label="MA5", color="#f5222d", linewidth=1.0, alpha=0.8
            )
            ax1.plot(
                dates,
                df["ma10"],
                label="MA10",
                color="#faad14",
                linewidth=1.0,
                alpha=0.8,
            )

            # 只有数据足够时才画MA20
            if len(df) >= 20:
                ax1.plot(
                    dates,
                    df["ma20"],
                    label="MA20",
                    color="#52c41a",
                    linewidth=1.0,
                    alpha=0.8,
                )

            ax1.set_title(f"{fund_name} - 价格走势", fontsize=14, pad=10)
            ax1.grid(True, linestyle="--", alpha=0.3)
            ax1.legend(loc="upper left", frameon=True, fontsize=9)

            # 副图：成交量
            ax2 = plt.subplot(gs[1], sharex=ax1)

            # 根据涨跌设置颜色 (红涨绿跌)
            colors = []
            for i in range(len(df)):
                if i == 0:
                    c = "#f5222d" if df.iloc[i].get("change_rate", 0) > 0 else "#52c41a"
                else:
                    change = df.iloc[i]["close"] - df.iloc[i - 1]["close"]
                    c = "#f5222d" if change >= 0 else "#52c41a"
                colors.append(c)

            ax2.bar(dates, volumes, color=colors, alpha=0.8)
            ax2.set_ylabel("成交量", fontsize=10)
            ax2.grid(True, linestyle="--", alpha=0.3)

            # 日期格式化
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
            plt.setp(ax1.get_xticklabels(), visible=False)  # 隐藏主图X轴标签
            plt.gcf().autofmt_xdate()  # 自动旋转日期

            plt.tight_layout()

            # 保存到内存
            buffer = io.BytesIO()
            plt.savefig(buffer, format="png", bbox_inches="tight")
            buffer.seek(0)

            # 转 Base64
            image_base64 = base64.b64encode(buffer.read()).decode("utf-8")
            plt.close()

            return image_base64
        except Exception as e:
            logger.error(f"绘图失败: {e}")
            return None

    @filter.command("基金历史")
    async def fund_history(
        self, event: AstrMessageEvent, code: str = "", days: str = "10"
    ):
        """
        查询基金历史行情
        用法: 基金历史 [基金代码] [天数]
        示例: 基金历史 161226 10
        """
        try:
            user_id = event.get_sender_id()
            fund_code, prefer_otc, normalized_code = self._parse_fund_command_input(
                code, user_id
            )

            try:
                num_days = int(days)
                if num_days < 1:
                    num_days = 10
                elif num_days > 60:
                    num_days = 60
            except ValueError:
                num_days = 10

            yield event.plain_result(
                f"📜 正在生成基金 {fund_code} 近 {num_days} 日行情报告..."
            )

            # 获取基金名称
            info = await self.analyzer.get_lof_realtime(
                fund_code, prefer_otc=prefer_otc
            )
            fund_name = info.name if info else fund_code

            history = await self.analyzer.get_lof_history(
                fund_code, days=num_days, prefer_otc=prefer_otc
            )

            if history:
                # 绘制走势图
                plot_img = await asyncio.to_thread(
                    self._plot_history_chart, history, fund_name
                )

                # 计算区间统计
                closes = [d["close"] for d in history]
                total_return = (
                    ((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] else 0
                )

                # 准备模板数据
                data = {
                    "fund_name": fund_name,
                    "fund_code": fund_code,
                    "days": num_days,
                    "history_list": list(reversed(history)),  # 倒序显示，最近的在前面
                    "plot_img": plot_img,
                    "total_return": total_return,
                    "max_price": max(closes),
                    "min_price": min(closes),
                    "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }

                # 读取模板
                template_path = (
                    Path(__file__).parent / "templates" / "history_report.html"
                )
                if not template_path.exists():
                    yield event.plain_result(f"❌ 模板文件不存在: {template_path}")
                    return

                # 渲染图片 - 优先使用本地渲染器
                if self.use_local_renderer:
                    try:
                        img_path = await render_fund_image(
                            template_path=template_path, template_data=data, width=420
                        )
                        yield event.image_result(img_path)
                    except Exception as e:
                        logger.warning(f"本地渲染失败，回退到网络渲染: {e}")
                        # 回退到网络渲染
                        with open(template_path, "r", encoding="utf-8") as f:
                            template_str = f.read()
                        img_url = await self.image_renderer.render_custom_template(
                            tmpl_str=template_str,
                            tmpl_data=data,
                            return_url=True,
                        )
                        yield event.image_result(img_url)
                else:
                    # 使用网络渲染
                    with open(template_path, "r", encoding="utf-8") as f:
                        template_str = f.read()
                    img_url = await self.image_renderer.render_custom_template(
                        tmpl_str=template_str,
                        tmpl_data=data,
                        return_url=True,
                    )
                    yield event.image_result(img_url)

            else:
                yield event.plain_result(f"❌ 未找到基金 {fund_code} 的历史数据")

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare matplotlib"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"查询基金历史出错: {e}")
            yield event.plain_result(f"❌ 查询失败: {str(e)}")

    @filter.command("搜索基金")
    async def search_fund(self, event: AstrMessageEvent, keyword: str = ""):
        """
        搜索LOF基金
        用法: 搜索基金 关键词
        示例: 搜索基金 白银
        """
        if not keyword:
            yield event.plain_result(
                "❓ 请输入搜索关键词\n用法: 搜索基金 关键词\n示例: 搜索基金 白银"
            )
            return

        try:
            yield event.plain_result(f"🔍 正在搜索包含「{keyword}」的基金...")

            results = await self.analyzer.search_fund(keyword)

            if results:
                text_lines = [
                    f"📋 搜索结果 (共 {len(results)} 条)",
                    "━━━━━━━━━━━━━━━━━",
                ]

                for fund in results:
                    price = fund.get("latest_price", 0)
                    change = fund.get("change_rate", 0)
                    # 价格为0通常表示暂无数据（原始数据为NaN）
                    if price == 0:
                        price_str = "暂无数据"
                        change_str = ""
                    else:
                        emoji = "🟢" if change > 0 else "🔴" if change < 0 else "⚪"
                        price_str = f"{price:.4f}"
                        change_str = f" {emoji}{change:+.2f}%"
                    text_lines.append(
                        f"{fund['code']} | {fund['name']}\n"
                        f"    💰 {price_str}{change_str}"
                    )

                text_lines.append("━━━━━━━━━━━━━━━━━")
                text_lines.append("💡 使用「基金 代码」查看详情")
                text_lines.append("💡 使用「设置基金 代码」设为默认")

                yield event.plain_result("\n".join(text_lines))
            else:
                yield event.plain_result(
                    f"❌ 未找到包含「{keyword}」的LOF基金\n💡 尝试使用其他关键词搜索"
                )

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"搜索基金出错: {e}")
            yield event.plain_result(f"❌ 搜索失败: {str(e)}")

    @filter.command("设置基金")
    async def set_default_fund(self, event: AstrMessageEvent, code: str = ""):
        """
        设置默认关注的基金
        用法: 设置基金 基金代码
        示例: 设置基金 161226
        """
        if not code:
            user_id = event.get_sender_id()
            current = self._get_user_fund(user_id)
            yield event.plain_result(
                f"💡 当前默认基金: {current}\n"
                "用法: 设置基金 基金代码\n"
                "示例: 设置基金 161226"
            )
            return

        try:
            # 解析六位代码；设置默认基金时需能区分场外以便校验
            parsed, prefer_otc = parse_fund_code_hint(code)
            if not parsed:
                yield event.plain_result(
                    f"❌ 无效的基金代码\n"
                    "💡 请使用六位数字代码，场外可加后缀 .OF 或前缀「场外」"
                )
                return
            code = parsed
            # 验证基金代码是否有效
            info = await self.analyzer.get_lof_realtime(
                parsed, prefer_otc=prefer_otc
            )

            if info:
                user_id = event.get_sender_id()
                self.user_fund_settings[user_id] = code
                self._save_user_settings()  # 持久化保存
                yield event.plain_result(
                    f"✅ 已设置默认基金\n"
                    f"📊 {info.code} - {info.name}\n"
                    f"💰 当前价格: {info.latest_price:.4f}"
                )
            else:
                yield event.plain_result(
                    f"❌ 无效的基金代码: {code}\n"
                    "💡 请使用「搜索基金 关键词」查找正确代码"
                )

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"设置默认基金出错: {e}")
            yield event.plain_result(f"❌ 设置失败: {str(e)}")

    @filter.command("智能分析")
    async def ai_fund_analysis(self, event: AstrMessageEvent, code: str = ""):
        """
        使用大模型进行智能基金分析（含量化数据）
        用法: 智能分析 [基金代码]
        示例: 智能分析 161226
        """
        try:
            user_id = event.get_sender_id()
            fund_code, prefer_otc, normalized_code = self._parse_fund_command_input(
                code, user_id
            )

            yield event.plain_result(
                f"🤖 正在对基金 {fund_code} 进行智能分析...\n"
                "📊 收集数据中，请稍候（约需30秒）..."
            )

            # 1. 获取基金基本信息
            info = await self.analyzer.get_lof_realtime(
                fund_code, prefer_otc=prefer_otc
            )
            if not info:
                # 区分是基金代码错误还是数据源问题
                if not normalized_code:
                    yield event.plain_result(f"❌ 基金代码不能为空")
                    return

                # 如果代码是6位数字，通常是有效的基金代码格式，但未找到数据
                if len(normalized_code) == 6 and normalized_code.isdigit():
                    # 尝试再次搜索确认是否存在
                    try:
                        search_res = await self.analyzer.search_fund(normalized_code)
                        if not search_res:
                            yield event.plain_result(
                                f"❌ 未找到基金代码 {fund_code}\n"
                                "💡 请检查代码是否正确，或使用「搜索基金 关键词」查找"
                            )
                            return
                    except Exception:
                        pass  # 搜索出错忽略，继续下面的判断

                yield event.plain_result(
                    f"⚠️ 暂时无法获取基金 {fund_code} 的数据\n"
                    "💡 可能是数据源暂时不可用，或该基金为非LOF基金\n"
                    "💡 请稍后重试"
                )
                return

            # 2. 获取历史数据（获取60天以支持更多回测策略）
            history = await self.analyzer.get_lof_history(
                fund_code, days=60, prefer_otc=prefer_otc
            )

            # 3. 计算技术指标（保留旧方法兼容性）
            indicators = {}
            if history:
                indicators = self.analyzer.calculate_technical_indicators(history)

            # 4. 检查大模型是否可用
            provider = self.context.get_using_provider()
            if not provider:
                yield event.plain_result(
                    "❌ 未配置大模型提供商\n"
                    "💡 请在 AstrBot 管理面板配置 LLM 提供商后再试"
                )
                return

            yield event.plain_result(
                "🧠 AI 正在分析数据，生成报告中...\n📈 正在计算量化指标和策略回测..."
            )

            # 5. 获取资金流向数据（场内基金）
            fund_flow_text = ""
            try:
                fund_flow = await self.analyzer._api.get_fund_flow(
                    fund_code, days=0, prefer_otc=prefer_otc
                )
                fund_flow_text = self.analyzer._api.format_fund_flow_text(fund_flow)
            except Exception as e:
                logger.debug(f"获取资金流向失败: {e}")
                fund_flow_text = "暂无资金流向数据"

            # 6. 使用 AI 分析器执行分析（含量化数据和资金流向）
            try:
                analysis_result = await self.ai_analyzer.analyze(
                    fund_info=info,
                    history_data=history or [],
                    technical_indicators=indicators,
                    user_id=user_id,
                    fund_flow_text=fund_flow_text,
                )

                # 获取技术信号
                signal, score = self.ai_analyzer.get_technical_signal(history or [])

                # 使用 markdown 库将 Markdown 转换为 HTML
                try:
                    import markdown

                    formatted_content = markdown.markdown(
                        analysis_result, extensions=["nl2br", "tables", "fenced_code"]
                    )
                except ImportError:
                    # 如果 markdown 库不可用，回退到简单的正则替换
                    import re

                    formatted_content = re.sub(
                        r"\*\*(.*?)\*\*", r"<strong>\1</strong>", analysis_result
                    )
                    # 处理换行
                    formatted_content = formatted_content.replace("\n", "<br>")

                # 准备模板数据
                data = {
                    "fund_name": info.name,
                    "fund_code": info.code,
                    "latest_price": info.latest_price,
                    "change_amount": info.change_amount,
                    "change_rate": info.change_rate,
                    "signal": signal,
                    "score": score,
                    "analysis_content": formatted_content,
                    "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }

                # 读取模板
                template_path = self._data_dir / "templates" / "ai_analysis_report.html"
                if not template_path.exists():
                    template_path = (
                        Path(__file__).parent / "templates" / "ai_analysis_report.html"
                    )

                plain_header = f"""
🤖 【{info.name}】智能量化分析报告
━━━━━━━━━━━━━━━━━
📅 分析时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}
💰 当前价格: {info.latest_price:.4f} ({info.change_rate:+.2f}%)
📊 技术信号: {signal} (评分: {score})
━━━━━━━━━━━━━━━━━
""".strip()

                if not template_path.exists():
                    # 降级到文本模式
                    yield event.plain_result(f"{plain_header}\n\n{analysis_result}")
                else:
                    # 渲染图片：本地 → 远程；均失败则文本回退
                    rendered_image = False
                    if self.use_local_renderer:
                        try:
                            img_path = await render_fund_image(
                                template_path=template_path,
                                template_data=data,
                                width=480,
                            )
                            yield event.image_result(img_path)
                            rendered_image = True
                        except Exception as e:
                            logger.warning(f"本地渲染失败，尝试远程渲染: {e}")

                    if not rendered_image:
                        try:
                            with open(template_path, "r", encoding="utf-8") as f:
                                template_str = f.read()
                            img_url = await self.image_renderer.render_custom_template(
                                tmpl_str=template_str, tmpl_data=data, return_url=True
                            )
                            yield event.image_result(img_url)
                            rendered_image = True
                        except Exception as e:
                            logger.warning(f"远程渲染失败，回退文本输出: {e}")

                    if not rendered_image:
                        yield event.plain_result(
                            f"{plain_header}\n\n{analysis_result}"
                        )

                # 添加免责声明 (如果是图片模式，免责声明已包含在图片底部，这里可以省略，或者发一条简短的)
                # yield event.plain_result("⚠️ 投资有风险，决策需谨慎。")

            except ValueError as e:
                yield event.plain_result(f"❌ {str(e)}")
            except Exception as e:
                logger.error(f"AI分析失败: {e}")
                yield event.plain_result(
                    f"❌ AI 分析失败: {str(e)}\n"
                    "💡 可能是大模型服务暂时不可用，请稍后再试"
                )

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"智能分析出错: {e}")
            yield event.plain_result(f"❌ 分析失败: {str(e)}")

    @filter.command("量化精选基金")
    async def quant_screen_funds(
        self,
        event: AstrMessageEvent,
        cap_arg: str = "",
        top_arg: str = "",
    ):
        """
        对东财场内 LOF 列表批量拉取日线，按综合分排序后输出偏买入方向的标的。
        用法: 量化精选基金 [分析数量上限] [输出条数]
        省略参数时分析东财单页 LOF 列表（约≤500 只），默认输出 10 条；仅一个参数时视为分析上限，输出条数仍为 10。
        示例: 量化精选基金 400 10
        """
        try:
            if not cap_arg.strip() and not top_arg.strip():
                cap_v, top_n = None, 10
            elif cap_arg.strip() and not top_arg.strip():
                cap_v = parse_optional_positive_int(None, cap_arg)
                top_n = 10
            else:
                cap_v = parse_optional_positive_int(None, cap_arg)
                top_n = parse_optional_positive_int(10, top_arg) or 10

            n_preview = cap_v if cap_v is not None else "全部(单页列表)"
            yield event.plain_result(
                f"📊 量化精选基金：准备分析至多 {n_preview} 只，"
                f"输出 TOP {top_n}（约需数分钟）..."
            )

            raw, attempted = await screen_lof_funds(
                self.analyzer, cap=cap_v, max_concurrent=DEFAULT_SCREENING_CONCURRENCY
            )
            if attempted == 0:
                yield event.plain_result(
                    "⚠️ 未获取到场内 LOF 列表，请检查网络或稍后重试。"
                )
                return
            ranked = rank_screening_rows(raw)
            top_rows = ranked[:top_n]

            if not raw:
                yield event.plain_result(
                    f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本（<{MIN_HISTORY_BARS} 根），无法排序。"
                    + DISCLAIMER
                )
                return

            async for msg in self._emit_screening_report(
                event,
                title="📈 场内 LOF 量化精选（综合分由高到低排序）",
                top_rows=top_rows,
                candidate_count=attempted,
                valid_count=len(raw),
            ):
                yield msg
        except Exception as e:
            logger.error(f"量化精选基金出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("量化精选股票")
    async def quant_screen_stocks(
        self,
        event: AstrMessageEvent,
        max_scan_arg: str = "",
        top_arg: str = "",
    ):
        """
        从 A 股全市场行情中按 |涨跌幅| 取前 N 只，逐只拉日线并量化排序。
        用法: 量化精选股票 [候选只数] [输出条数] [去北交所/含北交所 …] [去创业板/含创业板 …] [去科技/含科技 …] [去涨停…] [含ST 可选]
        关键词可与数字任意顺序；默认候选150只、输出10条；默认剔除北交所、创业板、科创板（可用 含北交/含创/含科技 等恢复）；
        默认输出纯文本榜单；需 HTML 报告图时加「发图」或「出图」等；
        「去*」与同条中「含*」并存时以「去*」为准；「去涨停」类：前筛剔除当日涨跌幅>9%（不按板块区分涨跌停幅度）；
        默认剔除 *ST/ST 风险警示股，写「含ST」「带ST」「不去ST」「保留ST」则保留。
        """
        try:
            tail = strip_command_prefix(
                get_event_plain_text(event), "量化精选股票"
            )
            max_scan, top_n, exclude_bse, exclude_chinext, exclude_limit_up, exclude_star, exclude_st, want_image = (
                parse_quant_stock_screen_tail(tail)
            )
            ex_notes: list[str] = []
            if exclude_bse:
                ex_notes.append("北交所")
            if exclude_chinext:
                ex_notes.append("创业板")
            if exclude_star:
                ex_notes.append("科创板")
            if exclude_limit_up:
                ex_notes.append("涨>9%")
            if exclude_st:
                ex_notes.append("*ST/ST")
            ex_suffix = (
                "，剔除：" + "、".join(ex_notes)
                if ex_notes
                else "（全市场）"
            )
            if not exclude_st:
                ex_suffix += "；含风险警示股"

            yield event.plain_result(
                f"📊 量化精选股票{ex_suffix}：按 |涨跌幅| 取前 {max_scan} 只拉取60日K线，"
                f"输出 TOP {top_n}（约需数分钟）..."
            )

            raw, attempted = await screen_stocks_by_abs_pct(
                self.stock_analyzer,
                self.analyzer,
                max_scan=max_scan,
                max_concurrent=DEFAULT_SCREENING_CONCURRENCY,
                exclude_bse=exclude_bse,
                exclude_chinext=exclude_chinext,
                exclude_star=exclude_star,
                exclude_limit_up=exclude_limit_up,
                exclude_st=exclude_st,
            )
            if attempted == 0:
                yield event.plain_result(
                    "⚠️ 未得到行情候选（需 akshare 或涨跌幅/代码列缺失），"
                    "请检查网络或缩小 max_scan。"
                )
                return
            ranked = rank_screening_rows(raw)
            top_rows = ranked[:top_n]

            if not raw:
                yield event.plain_result(
                    f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本（<{MIN_HISTORY_BARS} 根），无法排序。"
                    + DISCLAIMER
                )
                return

            async for msg in self._emit_screening_report(
                event,
                title="📈 A股量化精选（|涨跌幅|前筛 + 综合分排序）",
                top_rows=top_rows,
                candidate_count=attempted,
                valid_count=len(raw),
                prefer_image=want_image,
            ):
                yield msg
        except ImportError as e:
            yield event.plain_result(
                "❌ 需要 akshare 拉取 A 股行情\n请执行: pip install akshare"
            )
        except Exception as e:
            logger.error(f"量化精选股票出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("量化精选股票多空")
    async def quant_screen_stocks_debate_plain(self, event: AstrMessageEvent):
        """
        与「量化精选股票」相同筛股排序后，对前若干只依次执行「股票智能分析」，仅输出纯文本多空结论。
        用法: 量化精选股票多空 [候选只数] [输出条数] [智能分析只数上限] [板块/涨停/ST/涨停分析 …]（第三个数字可选）
        智能分析只数为 min(第三个数字, 输出条数, 全局上限)；未写第三个数字时为 min(输出条数, 全局上限)。
        剔除关键词、默认板块范围与「量化精选股票」相同（默认剔北交所/创业板/科创板，可用含*恢复）。
        默认：按板块近似判涨停则跳过 LLM，仅输出「涨停」。写「涨停分析」时涨停标的也跑多智能体辩论。
        成功行在方向后附精简「参考买入/止损」价位演示（规则化，非投资建议）。
        """
        try:
            if not self.context.get_using_provider():
                yield event.plain_result(
                    "❌ 未配置大模型提供商\n"
                    "💡 请在 AstrBot 管理面板配置 LLM 提供商后再试"
                )
                return

            tail = strip_command_prefix(
                get_event_plain_text(event), "量化精选股票多空"
            )
            (
                max_scan,
                top_n,
                debate_cap,
                exclude_bse,
                exclude_chinext,
                exclude_limit_up,
                exclude_star,
                exclude_st,
                debate_on_limit_up,
            ) = parse_quant_stock_screen_debate_tail(tail)

            ex_notes: list[str] = []
            if exclude_bse:
                ex_notes.append("北交所")
            if exclude_chinext:
                ex_notes.append("创业板")
            if exclude_star:
                ex_notes.append("科创板")
            if exclude_limit_up:
                ex_notes.append("涨>9%")
            if exclude_st:
                ex_notes.append("*ST/ST")
            ex_suffix = (
                "，剔除：" + "、".join(ex_notes)
                if ex_notes
                else "（全市场）"
            )
            if not exclude_st:
                ex_suffix += "；含风险警示股"

            zt_llm_note = (
                "涨停标的同样参与智能分析（约 9 次 LLM/只）。\n"
                if debate_on_limit_up
                else "涨停标的跳过 LLM。\n"
            )
            yield event.plain_result(
                f"⚖️ 量化精选股票多空{ex_suffix}：候选至多 {max_scan} 只 → TOP {top_n} → "
                f"依次智能分析前 {debate_cap} 只（每只约 9 次 LLM，约 3～5 分钟；单指令上限 "
                f"{MAX_QUANT_STOCK_DEBATE_CAP} 只）。{zt_llm_note}"
                "下面每条形如：代码 名称 看涨/看跌/中性（当前 ±x.xx%）；"
                "成功行另附一行参考买入/止损价位（演示）。"
            )

            raw, attempted = await screen_stocks_by_abs_pct(
                self.stock_analyzer,
                self.analyzer,
                max_scan=max_scan,
                max_concurrent=DEFAULT_SCREENING_CONCURRENCY,
                exclude_bse=exclude_bse,
                exclude_chinext=exclude_chinext,
                exclude_star=exclude_star,
                exclude_limit_up=exclude_limit_up,
                exclude_st=exclude_st,
            )
            if attempted == 0:
                yield event.plain_result(
                    "⚠️ 未得到行情候选（需 akshare 或涨跌幅/代码列缺失），"
                    "请检查网络或缩小 max_scan。"
                )
                return
            ranked = rank_screening_rows(raw)
            top_rows = ranked[:top_n]

            if not raw:
                yield event.plain_result(
                    f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本（<{MIN_HISTORY_BARS} 根），无法排序。"
                    + DISCLAIMER
                )
                return

            targets = top_rows[:debate_cap]
            if not targets:
                yield event.plain_result(
                    "⚠️ 排序结果为空，无法做多智能体分析。" + DISCLAIMER
                )
                return

            self.stock_analyzer.invalidate_stock_cache()

            from .stock.debate_trade_hint import format_trade_levels_lines

            ok_n = 0
            fail_n = 0
            skip_zt_n = 0
            dir_ok = frozenset({"看涨", "看跌", "中性"})
            for row in targets:
                rt = await self.stock_analyzer.get_stock_realtime(row.code)
                pct_txt = (
                    f"（当前 {rt.change_rate:+.2f}%）" if rt is not None else ""
                )
                if (
                    not debate_on_limit_up
                    and rt is not None
                    and is_effectively_limit_up(
                        rt.code, rt.name, rt.change_rate
                    )
                ):
                    skip_zt_n += 1
                    yield event.plain_result(
                        f"{row.code} {row.name} 涨停{pct_txt}"
                    )
                    continue

                debate_result, _info, err, align = await self._run_debate_pipeline_for_code(
                    row.code,
                    False,
                    progress_callback=None,
                    with_alignment=True,
                )
                if err:
                    fail_n += 1
                    short = (err.strip().split("\n") or [err])[0].strip()
                    yield event.plain_result(
                        f"{row.code} {row.name} 失败：{short}{pct_txt}"
                    )
                else:
                    ok_n += 1
                    d = debate_result.final_direction
                    label = d if d in dir_ok else "中性"
                    align_dict = (
                        align.as_alignment_dict()
                        if align is not None
                        and hasattr(align, "as_alignment_dict")
                        else None
                    )
                    hints = format_trade_levels_lines(
                        direction=label,
                        latest_price=float(debate_result.stock_price or 0.0),
                        alignment=align_dict,
                    )
                    hint_one = " ".join(h for h in hints if h)
                    yield event.plain_result(
                        f"{row.code} {row.name} {label}{pct_txt}\n{hint_one}"
                    )

            yield event.plain_result(
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"完成：成功 {ok_n} 条，失败 {fail_n} 条"
                f"{'' if skip_zt_n == 0 else f'，跳过涨停 {skip_zt_n} 条'}。\n"
                f"{DISCLAIMER.strip()}"
            )

        except ImportError:
            yield event.plain_result(
                "❌ 需要 akshare 拉取 A 股行情\n请执行: pip install akshare"
            )
        except Exception as e:
            logger.error(f"量化精选股票多空出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("量化精选仓位计划")
    async def quant_screen_stocks_position_plan(self, event: AstrMessageEvent):
        """
        与「量化精选股票多空」同源筛股与辩论；额外解析本金与风控参数，输出结构化快照与日 K 对齐指标，
        并按 score 加权给出整手仓位演示（不构成投资建议）。
        用法示例：量化精选仓位计划 本金100万 150 10 3 风险1% 止损2ATR 分0.7 额均2亿 单票20% 最多5只 去涨停
        （筛股板块默认与「量化精选股票」相同：默认剔北交所/创业板/科创板。）可选「涨停分析」使涨停标的也跑辩论。
        """
        try:
            if not self.context.get_using_provider():
                yield event.plain_result(
                    "❌ 未配置大模型提供商\n"
                    "💡 请在 AstrBot 管理面板配置 LLM 提供商后再试"
                )
                return

            tail = strip_command_prefix(
                get_event_plain_text(event), "量化精选仓位计划"
            )
            (
                max_scan,
                top_n,
                debate_cap,
                exclude_bse,
                exclude_chinext,
                exclude_limit_up,
                exclude_star,
                exclude_st,
                debate_on_limit_up,
                principal,
                risk_fraction,
                k_atr,
                min_score_01,
                min_avg_amount_yi,
                single_cap_fraction,
                max_positions,
            ) = parse_quant_stock_screen_position_tail(tail)

            if principal is None or principal <= 0:
                yield event.plain_result(
                    "❌ 请在尾部指定本金，例如：本金100万、本金50w、本金1000000（元）\n"
                    "💡 可选：风险1% 或 风险0.01 | 止损2ATR | 分0.7 | 额均2亿 | 单票20% | 最多5只\n"
                    "（其余数字与板块/涨停/ST/涨停分析 等关键词与「量化精选股票多空」相同；默认剔北交所、创业板、科创板。）"
                )
                return

            ex_notes: list[str] = []
            if exclude_bse:
                ex_notes.append("北交所")
            if exclude_chinext:
                ex_notes.append("创业板")
            if exclude_star:
                ex_notes.append("科创板")
            if exclude_limit_up:
                ex_notes.append("涨>9%")
            if exclude_st:
                ex_notes.append("*ST/ST")
            ex_suffix = (
                "，剔除：" + "、".join(ex_notes)
                if ex_notes
                else "（全市场）"
            )
            if not exclude_st:
                ex_suffix += "；含风险警示股"

            yield event.plain_result(
                f"📐 量化精选仓位计划{ex_suffix}\n"
                f"本金={principal:.2f} 组合风险={risk_fraction:.4f} "
                f"k_ATR={k_atr} 最低分={min_score_01} "
                f"额均≥{min_avg_amount_yi}亿 单票上限={single_cap_fraction:.2%}"
                f"{'' if max_positions is None else f' 最多{max_positions}只'}\n"
                f"候选至多 {max_scan} → TOP {top_n} → 辩论前 {debate_cap} 只（每只约 9 次 LLM）。"
                + (
                    "涨停标的同样参与智能分析。\n"
                    if debate_on_limit_up
                    else "涨停跳过 LLM。\n"
                )
                + "每条形如：代码 名称 dir=… score=… bar=… atr=… 额均=… ts=…"
            )

            raw, attempted = await screen_stocks_by_abs_pct(
                self.stock_analyzer,
                self.analyzer,
                max_scan=max_scan,
                max_concurrent=DEFAULT_SCREENING_CONCURRENCY,
                exclude_bse=exclude_bse,
                exclude_chinext=exclude_chinext,
                exclude_star=exclude_star,
                exclude_limit_up=exclude_limit_up,
                exclude_st=exclude_st,
            )
            if attempted == 0:
                yield event.plain_result(
                    "⚠️ 未得到行情候选（需 akshare 或涨跌幅/代码列缺失），"
                    "请检查网络或缩小 max_scan。"
                )
                return
            ranked = rank_screening_rows(raw)
            top_rows = ranked[:top_n]

            if not raw:
                yield event.plain_result(
                    f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本（<{MIN_HISTORY_BARS} 根），无法排序。"
                    + DISCLAIMER
                )
                return

            targets = top_rows[:debate_cap]
            if not targets:
                yield event.plain_result(
                    "⚠️ 排序结果为空，无法执行辩论。" + DISCLAIMER
                )
                return

            self.stock_analyzer.invalidate_stock_cache()

            ok_n = 0
            fail_n = 0
            skip_zt_n = 0
            snapshots_ok: list[dict[str, Any]] = []
            dir_ok = frozenset({"看涨", "看跌", "中性"})
            for row in targets:
                rt = await self.stock_analyzer.get_stock_realtime(row.code)
                pct_txt = (
                    f"（当前 {rt.change_rate:+.2f}%）" if rt is not None else ""
                )
                if (
                    not debate_on_limit_up
                    and rt is not None
                    and is_effectively_limit_up(
                        rt.code, rt.name, rt.change_rate
                    )
                ):
                    skip_zt_n += 1
                    yield event.plain_result(
                        f"{row.code} {row.name} 涨停{pct_txt}"
                    )
                    continue

                debate_result, _info, err, alignment = (
                    await self._run_debate_pipeline_for_code(
                        row.code,
                        False,
                        progress_callback=None,
                        with_alignment=True,
                    )
                )
                if err:
                    fail_n += 1
                    short = (err.strip().split("\n") or [err])[0].strip()
                    yield event.plain_result(
                        f"{row.code} {row.name} 失败：{short}{pct_txt}"
                    )
                else:
                    ok_n += 1
                    d = debate_result.final_direction
                    label = d if d in dir_ok else "中性"
                    align_dict = (
                        alignment.as_alignment_dict()
                        if alignment is not None
                        else {}
                    )
                    snap = debate_result.to_snapshot_dict(
                        alignment=align_dict if align_dict else None
                    )
                    if rt is not None:
                        lp = float(getattr(rt, "latest_price", 0.0) or 0.0)
                        if lp > 0:
                            snap["price"] = lp
                    snapshots_ok.append(snap)
                    atr_txt = (
                        f"{align_dict['atr14']:.4f}"
                        if align_dict.get("atr14") is not None
                        else "-"
                    )
                    avg_txt = (
                        f"{align_dict['avg_amount_5d_yi']:.4f}"
                        if align_dict.get("avg_amount_5d_yi") is not None
                        else "-"
                    )
                    bar_txt = align_dict.get("last_bar_date") or "-"
                    ts_txt = snap.get("completed_at") or "-"
                    sc = snap.get("score", 0.0)
                    yield event.plain_result(
                        f"{row.code} {row.name} dir={label}{pct_txt} "
                        f"score={sc:.3f} bar={bar_txt} atr={atr_txt} "
                        f"额均={avg_txt}亿 ts={ts_txt}"
                    )

            plan = build_position_plan(
                principal,
                risk_fraction,
                k_atr,
                min_score_01=min_score_01,
                min_avg_amount_yi=min_avg_amount_yi,
                single_cap_fraction=single_cap_fraction,
                max_positions=max_positions,
                snapshots=snapshots_ok,
            )

            yield event.plain_result(format_position_plan_table(plan))
            if plan.warnings:
                yield event.plain_result(
                    "【配比说明 / 跳过原因】\n" + "\n".join(plan.warnings)
                )

            batch_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
            payload = {
                "completed_batch_at": batch_at,
                "principal": principal,
                "risk_fraction": risk_fraction,
                "k_atr": k_atr,
                "min_score_01": min_score_01,
                "min_avg_amount_yi": min_avg_amount_yi,
                "single_cap_fraction": single_cap_fraction,
                "max_positions": max_positions,
                "snapshots": snapshots_ok,
                "plan_rows": [asdict(r) for r in plan.rows],
                "warnings": plan.warnings,
                "total_notional": plan.total_notional,
            }
            yield event.plain_result(
                "```json\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
                + "\n```"
            )

            yield event.plain_result(
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"辩论完成：成功 {ok_n} 条，失败 {fail_n} 条"
                f"{'' if skip_zt_n == 0 else f'，跳过涨停 {skip_zt_n} 条'}。\n"
                "⚠️ A 股整手、T+1、涨跌停及成交不确定性未建模；以下为演示用配比。\n"
                f"{DISCLAIMER.strip()}"
            )

        except ImportError:
            yield event.plain_result(
                "❌ 需要 akshare 拉取 A 股行情\n请执行: pip install akshare"
            )
        except Exception as e:
            logger.error(f"量化精选仓位计划出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("打板资金")
    async def tushare_daban_limit_flow_migrate(self, event: AstrMessageEvent):
        """已合并至「打板选股」。"""
        yield event.plain_result(
            "「打板资金」已合并为「打板选股」。\n"
            "用法: 打板选股 [YYYYMMDD] [条数] [综合|首板|接力|龙头] "
            "[不含同花顺] [不含天梯] [不含龙虎榜] [去北交所]\n"
            "示例: 打板选股 20250514 15 接力\n"
            "说明: 盘后涨停池评分选股，默认综合模式；建议 Tushare 积分≥8000。"
        )

    @filter.command("打板选股")
    async def tushare_daban_pick_codes(self, event: AstrMessageEvent):
        """
        打板选股：涨停池 + 分位数综合评分，支持综合/首板/接力/龙头模式。
        用法: 打板选股 [YYYYMMDD] [条数] [综合|首板|接力|龙头] …
        未写日期用最近上交所开市日；盘后数据，供次日计划，非投资建议。
        """
        ts_tok = getattr(self.stock_analyzer, "_tushare_token", None)
        if not (ts_tok or "").strip():
            yield event.plain_result(
                "❌ 未配置 Tushare token\n"
                "💡 请在插件配置填写 tushare_token 或设置环境变量 TUSHARE_TOKEN"
            )
            return

        tail = strip_command_prefix(get_event_plain_text(event), "打板选股")
        try:
            (
                trade_date_in,
                top_n,
                mode_str,
                want_ths,
                want_step,
                want_top_list,
                exclude_bj,
                exclude_st,
            ) = parse_daban_pick_tail(tail)
        except Exception as e:
            yield event.plain_result(f"❌ 参数解析失败: {e}")
            return

        try:
            from .tushare_client.daban_pick_engine import (
                DabanPickConfig,
                DabanPickMode,
                fetch_daban_pick,
            )
            from .tushare_client.daban_pick_format import format_daban_pick_v2
            from .tushare_client.limit_up_fetch import fetch_last_sse_trade_date

            try:
                mode = DabanPickMode(mode_str)
            except ValueError:
                mode = DabanPickMode.MIXED

            if trade_date_in:
                trade_date = trade_date_in
            else:
                resolved = await asyncio.to_thread(
                    fetch_last_sse_trade_date, ts_tok
                )
                if not resolved:
                    yield event.plain_result(
                        "❌ 无法解析默认交易日，请显式传入 YYYYMMDD。"
                    )
                    return
                trade_date = resolved

            cfg = DabanPickConfig(
                top_n=top_n,
                mode=mode,
                exclude_st=exclude_st,
                exclude_bj=exclude_bj,
                want_ths=want_ths,
                want_step=want_step,
                want_top_list=want_top_list,
            )

            def _run():
                return fetch_daban_pick(ts_tok, trade_date, cfg)

            rows, stats = await asyncio.to_thread(_run)
            body = format_daban_pick_v2(
                rows, trade_date=trade_date, mode=mode, stats=stats
            )
            yield event.plain_result(
                body
                + "\n━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ 盘后涨停池评分；资金以东财 moneyflow_dc 为准；非投资建议。\n"
                + DISCLAIMER.strip()
            )
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
        except Exception as e:
            logger.error(f"打板选股出错: {e}")
            yield event.plain_result(
                f"❌ 执行失败: {e}\n"
                "💡 若提示权限/积分不足，请核对 Tushare 积分（建议≥8000）。"
            )

    @filter.command("打板选股多空")
    async def tushare_daban_pick_debate(self, event: AstrMessageEvent):
        """
        打板选股 A 档后对前 K 只多智能体辩论。
        用法: 打板选股多空 [YYYYMMDD] [条数] [辩论只数] [综合|首板|接力|龙头] …
        未写辩论只数时 min(条数, 15)；每只约 9 次 LLM。
        """
        if not self.context.get_using_provider():
            yield event.plain_result(
                "❌ 未配置大模型提供商\n"
                "💡 请在 AstrBot 管理面板配置 LLM 提供商后再试"
            )
            return

        ts_tok = getattr(self.stock_analyzer, "_tushare_token", None)
        if not (ts_tok or "").strip():
            yield event.plain_result(
                "❌ 未配置 Tushare token\n"
                "💡 请在插件配置填写 tushare_token 或设置环境变量 TUSHARE_TOKEN"
            )
            return

        tail = strip_command_prefix(get_event_plain_text(event), "打板选股多空")
        try:
            (
                trade_date_in,
                top_n,
                debate_cap,
                mode_str,
                want_ths,
                want_step,
                want_top_list,
                exclude_bj,
                exclude_st,
            ) = parse_daban_pick_debate_tail(tail)
        except Exception as e:
            yield event.plain_result(f"❌ 参数解析失败: {e}")
            return

        try:
            from .tushare_client.daban_pick_engine import (
                DabanPickConfig,
                DabanPickMode,
                fetch_daban_pick,
                mode_label,
            )
            from .tushare_client.limit_up_fetch import fetch_last_sse_trade_date
            from .stock.debate_engine import DebateEngine

            try:
                mode = DabanPickMode(mode_str)
            except ValueError:
                mode = DabanPickMode.MIXED

            if trade_date_in:
                trade_date = trade_date_in
            else:
                resolved = await asyncio.to_thread(
                    fetch_last_sse_trade_date, ts_tok
                )
                if not resolved:
                    yield event.plain_result(
                        "❌ 无法解析默认交易日，请显式传入 YYYYMMDD。"
                    )
                    return
                trade_date = resolved

            cfg = DabanPickConfig(
                top_n=top_n,
                mode=mode,
                exclude_st=exclude_st,
                exclude_bj=exclude_bj,
                want_ths=want_ths,
                want_step=want_step,
                want_top_list=want_top_list,
            )

            def _run_merge():
                return fetch_daban_pick(ts_tok, trade_date, cfg)

            merged, stats = await asyncio.to_thread(_run_merge)
            a_rows = [r for r in merged if str(r.get("tier", "")).upper() == "A"]
            targets = a_rows[:debate_cap]
            if not targets:
                yield event.plain_result(
                    f"{trade_date} [{mode_label(mode)}] 无 A 档候选可辩论。"
                    + DISCLAIMER
                )
                return

            yield event.plain_result(
                f"打板选股多空 {trade_date} 模式={mode_label(mode)} "
                f"盘面={stats.get('market_regime', '-')} | "
                f"涨停池{stats.get('n_limit', 0)}→过滤{stats.get('n_after_mode', 0)}"
                f"→评分{stats.get('n_scored', 0)} | A档{stats.get('n_a', len(a_rows))}只 "
                f"辩论前{len(targets)}只（约9次LLM/只）"
            )

            engine = DebateEngine(self.context)
            ok_n = 0
            fail_n = 0
            dir_ok = frozenset({"看涨", "看跌", "中性"})
            for row in targets:
                ts_code = str(row.get("ts_code") or "").strip()
                name = str(row.get("name") or "")
                fund_code = ts_code_to_fund_debate_code(ts_code)
                if not fund_code:
                    fail_n += 1
                    yield event.plain_result(f"{ts_code} {name} 失败：代码无效")
                    continue
                debate_result, _info, err, align = (
                    await self._run_debate_pipeline_for_code(
                        fund_code,
                        False,
                        progress_callback=None,
                        with_alignment=True,
                    )
                )
                if err:
                    fail_n += 1
                    short = (err.strip().split("\n") or [err])[0].strip()
                    yield event.plain_result(
                        f"{ts_code} {name} 失败：{short}"
                    )
                else:
                    ok_n += 1
                    d = debate_result.final_direction
                    label = d if d in dir_ok else "中性"
                    align_dict = (
                        align.as_alignment_dict()
                        if align is not None
                        and hasattr(align, "as_alignment_dict")
                        else None
                    )
                    summary = engine.format_debate_summary(
                        debate_result, alignment_dict=align_dict
                    )
                    yield event.plain_result(
                        f"━━ {ts_code} {name} {label} ━━\n{summary}"
                    )

            yield event.plain_result(
                f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"完成：成功 {ok_n} 条，失败 {fail_n} 条。\n"
                f"{DISCLAIMER.strip()}"
            )
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
        except Exception as e:
            logger.error(f"打板选股多空出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("股票回测")
    async def stock_equity_backtest(self, event: AstrMessageEvent):
        """
        A 股区间回测：买入持有（≥2 交易日）+ 可选 MA/RSI/MACD 策略。
        用法: 股票回测 <代码> <YYYYMMDD-YYYYMMDD> [仅基准|含策略]
        默认短区间仅基准；≥40 交易日自动含策略。示例:
        股票回测 600519 20240101-20240115
        股票回测 600519 20240101-20240630 含策略
        """
        tail = strip_command_prefix(get_event_plain_text(event), "股票回测")
        try:
            code_raw, start_date, end_date, strategy_mode_str = (
                parse_stock_backtest_tail(tail)
            )
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            return

        try:
            from datetime import datetime

            from .stock.equity_backtest import (
                EquityBacktestConfig,
                MIN_BARS_INTERVAL,
                StrategyMode,
                build_equity_backtest_report,
                filter_history_by_date_range,
                format_equity_backtest_report,
            )
            from .tushare_client.klines import fetch_daily_klines_date_range
            from .tushare_client.symbols import normalize_tickflow_symbol

            ts_code = normalize_tickflow_symbol(code_raw)
            code6 = ts_code_to_fund_debate_code(ts_code)
            if not code6:
                yield event.plain_result("❌ 股票代码无效")
                return

            try:
                strategy_mode = StrategyMode(strategy_mode_str)
            except ValueError:
                strategy_mode = StrategyMode.AUTO
            bt_cfg = EquityBacktestConfig(strategy_mode=strategy_mode)

            ts_tok = getattr(self.stock_analyzer, "_tushare_token", None)
            range_label = f"{start_date}-{end_date}"
            yield event.plain_result(
                f"📊 正在回测 {ts_code} 区间 {range_label}…"
            )

            history = None
            if (ts_tok or "").strip():
                try:
                    history = await asyncio.to_thread(
                        fetch_daily_klines_date_range,
                        ts_tok,
                        ts_code,
                        start_date,
                        end_date,
                        "qfq",
                        min_bars=MIN_BARS_INTERVAL,
                    )
                except Exception as e:
                    logger.warning(f"Tushare 区间 K 线失败: {e}")

            if not history:
                try:
                    d0 = datetime.strptime(start_date, "%Y%m%d")
                    d1 = datetime.strptime(end_date, "%Y%m%d")
                    span_days = max((d1 - d0).days + 60, 90)
                except ValueError:
                    span_days = 365
                em_hist = await get_eastmoney_api().get_fund_history(
                    code6, days=span_days, adjust="qfq"
                )
                if em_hist:
                    history = filter_history_by_date_range(
                        em_hist, start_date, end_date
                    )

            if not history or len(history) < MIN_BARS_INTERVAL:
                yield event.plain_result(
                    f"❌ 区间 {range_label} 有效交易日不足（需≥{MIN_BARS_INTERVAL} 天）\n"
                    "💡 请检查代码、日期是否为交易日，或配置 tushare_token 后重试"
                )
                return

            name = ts_code
            try:
                rt = await self.stock_analyzer.get_stock_realtime(ts_code)
                if rt and rt.name:
                    name = rt.name
            except Exception:
                pass

            report = build_equity_backtest_report(
                history,
                ts_code=ts_code,
                name=name,
                start_date=start_date,
                end_date=end_date,
                config=bt_cfg,
            )
            body = format_equity_backtest_report(report)
            yield event.plain_result(
                body
                + "\n━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ 历史回测基于盘后日 K，不代表未来表现。\n"
                + DISCLAIMER.strip()
            )
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
        except Exception as e:
            logger.error(f"股票回测出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("打板查股")
    async def tushare_daban_check_stock(self, event: AstrMessageEvent):
        """
        打板查股：输入代码，输出该票当日基本信息、涨停/开板/封单、资金流向等。
        用法: 打板查股 <代码或ts_code> [YYYYMMDD]
        示例: 打板查股 000001.SZ 20250514
        """
        ts_tok = getattr(self.stock_analyzer, "_tushare_token", None)
        if not (ts_tok or "").strip():
            yield event.plain_result(
                "❌ 未配置 Tushare token\n"
                "💡 请在插件配置填写 tushare_token 或设置环境变量 TUSHARE_TOKEN"
            )
            return

        tail = strip_command_prefix(get_event_plain_text(event), "打板查股")
        trade_date_in, code_raw = parse_daban_check_stock_tail(tail)
        if not code_raw:
            yield event.plain_result(
                "❌ 请输入股票代码\n💡 用法: 打板查股 <代码或ts_code> [YYYYMMDD]"
            )
            return

        try:
            from .tushare_client.limit_up_fetch import (
                fetch_last_sse_trade_date,
                fetch_limit_list_d_one,
                fetch_moneyflow_dc_one,
            )
            from .tushare_client.symbols import normalize_tickflow_symbol

            if trade_date_in:
                trade_date = trade_date_in
            else:
                resolved = await asyncio.to_thread(
                    fetch_last_sse_trade_date, ts_tok
                )
                if not resolved:
                    yield event.plain_result(
                        "❌ 无法解析默认交易日，请显式传入 YYYYMMDD。"
                    )
                    return
                trade_date = resolved

            ts_code = normalize_tickflow_symbol(code_raw)
            code6 = ts_code_to_fund_debate_code(ts_code)

            rt = await self.stock_analyzer.get_stock_realtime(ts_code)
            limit_row = await asyncio.to_thread(
                fetch_limit_list_d_one, ts_tok, trade_date, ts_code
            )
            flow_row = await asyncio.to_thread(
                fetch_moneyflow_dc_one, ts_tok, trade_date, ts_code
            )

            lines: list[str] = []
            lines.append(f"🔎 打板查股 {trade_date} {ts_code}")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")

            if rt is not None:
                lines.append(
                    f"名称: {rt.name} 现价: {rt.latest_price:.3f} 涨跌幅: {rt.change_rate:+.2f}%"
                )
                lines.append(
                    f"今开: {rt.open_price:.3f} 最高: {rt.high_price:.3f} 最低: {rt.low_price:.3f} 昨收: {rt.prev_close:.3f}"
                )
                lines.append(
                    f"成交额: {rt.amount/1e8:.2f}亿 换手: {rt.turnover_rate:.2f}% 来源: {getattr(self.stock_analyzer, '_current_source', '-')}"
                )
            else:
                lines.append("行情: ⚠️ 未取到实时行情（可稍后重试）")

            if limit_row:
                open_times = limit_row.get("open_times")
                fd_amount = limit_row.get("fd_amount")
                limit_times = limit_row.get("limit_times")
                first_time = limit_row.get("first_time") or "-"
                last_time = limit_row.get("last_time") or "-"
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(
                    "涨停池: "
                    f"连板={limit_times} 开板={open_times} 封单额={fd_amount} 首封={first_time} 末封={last_time}"
                )
            else:
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append("涨停池: (limit_list_d) 未命中（可能非涨停/无数据/权限不足）")

            if flow_row:
                net_amount = flow_row.get("net_amount")
                net_rate = flow_row.get("net_amount_rate")
                elg = flow_row.get("buy_elg_amount")
                lg = flow_row.get("buy_lg_amount")
                md = flow_row.get("buy_md_amount")
                sm = flow_row.get("buy_sm_amount")
                tier_sum = 0.0
                for x in (elg, lg, md, sm):
                    try:
                        tier_sum += float(x)
                    except Exception:
                        pass
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append(
                    f"资金(东财/DC): 主力净流入={net_amount}万 占比={net_rate}% 分档累加={tier_sum:.2f}万"
                )
                lines.append(
                    f"  超大={elg}万 大={lg}万 中={md}万 小={sm}万"
                )
            else:
                lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
                lines.append("资金(东财/DC): ⚠️ 未取到 moneyflow_dc（可能无数据/权限不足/非交易日）")

            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("⚠️ 以上为数据汇总与规则化展示，不构成投资建议。")
            yield event.plain_result("\n".join(lines))

        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
        except Exception as e:
            logger.error(f"打板查股出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("短线选股")
    async def stock_short_term_screen(
        self,
        event: AstrMessageEvent,
        max_scan_arg: str = "",
        top_arg: str = "",
    ):
        """
        基于量价因子的 1-3 日短线选股：动量 + 量能突破 + 量价共振 + 换手情绪 + 蓄势/位置 + 短期反弹。
        可选「加资金流」「加大盘」「加触发」（详见尾部关键词）。
        用法: 短线选股 [候选数] [输出条数] [额X亿] [加资金流] [加大盘] [加触发] [去北交所/含北交所 …] …
        默认剔除北交所、创业板、科创板（可用 含北交/含创/含科技 等纳入；与「量化精选股票」规则一致）；
        默认剔除 *ST/ST；「含ST」「带ST」「不去ST」「保留ST」可保留风险警示股。
        关键词与数字可任意顺序；默认候选 200、输出 10、最小日成交额 1 亿；
        「加大盘」拉上证指数并按档位温和缩放总分；「加触发」展示威科夫主触发并小额加减分。
        """
        try:
            tail = strip_command_prefix(get_event_plain_text(event), "短线选股")
            (
                max_scan,
                top_n,
                min_amount_yi,
                exclude_bse,
                exclude_chinext,
                exclude_limit_up,
                exclude_star,
                exclude_st,
                with_fund_flow,
                with_market_regime,
                with_wyckoff_trigger,
            ) = parse_short_term_screen_tail(tail)

            ex_notes: list[str] = []
            if exclude_bse:
                ex_notes.append("北交所")
            if exclude_chinext:
                ex_notes.append("创业板")
            if exclude_star:
                ex_notes.append("科创板")
            if exclude_limit_up:
                ex_notes.append("涨>9%")
            if exclude_st:
                ex_notes.append("*ST/ST")
            ex_suffix = (
                "，剔除：" + "、".join(ex_notes)
                if ex_notes
                else "（全市场）"
            )
            if not exclude_st:
                ex_suffix += "；含风险警示股"

            flow_notice = ""
            time_hint = "约数分钟"
            if with_fund_flow:
                flow_notice = " + 主力流向（顶部派发/底部吸筹/主散对冲）"
                time_hint = "约 5~7 分钟（含主力流向）"
            opt_bits: list[str] = []
            if with_market_regime:
                opt_bits.append("上证水温缩放总分")
            if with_wyckoff_trigger:
                opt_bits.append("威科夫触发列与小加分")
            opt_txt = ""
            if opt_bits:
                opt_txt = "；" + "、".join(opt_bits)
                if not with_fund_flow:
                    time_hint = "约数分钟（略增解析）"

            yield event.plain_result(
                f"🎯 短线选股{ex_suffix}：按 |涨跌幅| 取候选 {max_scan} 只"
                f"（成交额≥{min_amount_yi}亿），输出 TOP {top_n}\n"
                f"因子：动量 + 量能 + 量价共振 + 换手情绪 + 蓄势/位置 + 短期RSI{flow_notice}{opt_txt}\n"
                f"⏳ 正在拉取全市场行情与 60 日 K 线（{time_hint}）…"
            )

            lines, attempted, valid, with_flow_n, regime_snap = (
                await screen_stocks_short_term(
                    self.stock_analyzer,
                    self.analyzer,
                    max_scan=max_scan,
                    min_amount_yi=min_amount_yi,
                    max_concurrent=DEFAULT_SCREENING_CONCURRENCY,
                    exclude_bse=exclude_bse,
                    exclude_chinext=exclude_chinext,
                    exclude_star=exclude_star,
                    exclude_limit_up=exclude_limit_up,
                    exclude_st=exclude_st,
                    with_fund_flow=with_fund_flow,
                    with_market_regime=with_market_regime,
                    with_wyckoff_trigger=with_wyckoff_trigger,
                )
            )
            if attempted == 0:
                yield event.plain_result(
                    "⚠️ 未得到候选标的（可能成交额阈值过高、关键词剔除过多或行情列缺失）。"
                    + DISCLAIMER
                )
                return
            if valid == 0:
                yield event.plain_result(
                    f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本"
                    f"（<{SHORT_TERM_MIN_BARS} 根），无法计算量价因子。"
                    + DISCLAIMER
                )
                return

            trunc = ""
            if len(lines) > top_n:
                trunc = f"💡 共 {len(lines)} 只入榜，仅展示前 {top_n}。\n"

            extra_pool = ""
            if regime_snap is not None:
                lab = str(getattr(regime_snap, "label", "") or "")
                att = str(getattr(regime_snap, "attitude", "") or "")
                extra_pool = f"上证盘面: {lab} — {att}\n"
                nlist = getattr(regime_snap, "notes", None) or []
                if nlist:
                    extra_pool += "  · " + "\n  · ".join(
                        str(x) for x in nlist[:4]
                    ) + "\n"

            text = format_short_term_report(
                lines[:top_n],
                candidate_count=attempted,
                valid_count=valid,
                truncation_note=trunc,
                min_amount_yi=min_amount_yi,
                with_fund_flow=with_fund_flow,
                fund_flow_count=with_flow_n,
                extra_pool_lines=extra_pool,
                with_wyckoff_trigger=with_wyckoff_trigger,
            )
            yield event.plain_result(text)
        except ImportError as e:
            yield event.plain_result(
                "❌ 需要 akshare 拉取 A 股行情\n请执行: pip install akshare"
            )
            logger.debug("短线选股 ImportError: %s", e)
        except Exception as e:
            logger.error(f"短线选股出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("短线批量分析")
    async def stock_short_term_batch(self, event: AstrMessageEvent):
        """
        对指定多只 A 股复用「短线选股」同源因子与打分，批量输出报告。
        用法: 短线批量分析 <代码…> [加资金流] [加大盘] [加触发] [展示条数1-50｜前N]
        单次最多分析的只数见 SHORT_TERM_BATCH_MAX_CODES。
        """
        try:
            tail = strip_command_prefix(get_event_plain_text(event), "短线批量分析")
            (
                codes,
                with_fund_flow,
                top_show,
                with_market_regime,
                with_wyckoff_trigger,
            ) = parse_short_term_batch_tail(tail)
            if not codes:
                yield event.plain_result(
                    "用法: 短线批量分析 <代码1> [代码2] … [加资金流｜含主力] [加大盘] [加触发] [展示条数]\n"
                    "• 同源因子：短线选股的量价评分（1-3 日视角）\n"
                    "• 可加「加资金流」启用主力流向\n"
                    "• 「加大盘」「加触发」与短线选股尾部语义一致\n"
                    "• 末尾 1～50 的数字或「前N」为展示条数（默认 "
                    f"{DEFAULT_SHORT_TERM_BATCH_TOP}）\n"
                    "示例:\n"
                    "  短线批量分析 600519 000001 300750\n"
                    "  短线批量分析 688981 加资金流\n"
                    "  短线批量分析 601398 601288 加大盘 加触发 15\n"
                    + DISCLAIMER
                )
                return

            trunc_head = ""
            if len(codes) > SHORT_TERM_BATCH_MAX_CODES:
                codes = codes[:SHORT_TERM_BATCH_MAX_CODES]
                trunc_head = (
                    f"⚠️ 单次最多分析 {SHORT_TERM_BATCH_MAX_CODES} 只，已截断多余代码。\n"
                )

            flow_notice = ""
            time_hint = "约 1~3 分钟"
            if with_fund_flow:
                flow_notice = " + 主力流向"
                time_hint = "约 2~5 分钟（含主力流向）"
            opt_bits: list[str] = []
            if with_market_regime:
                opt_bits.append("上证水温")
            if with_wyckoff_trigger:
                opt_bits.append("威科夫触发")
            if opt_bits:
                flow_notice += " +" + "+".join(opt_bits)

            yield event.plain_result(
                trunc_head
                + f"🎯 短线批量分析：共 {len(codes)} 只（同源因子）{flow_notice}\n"
                + f"⏳ 正在解析名称并拉取 60 日 K 线…（{time_hint}）"
            )

            lines, attempted, valid, with_flow_n, regime_snap = (
                await batch_analyze_short_term_by_codes(
                    self.stock_analyzer,
                    self.analyzer,
                    codes,
                    max_concurrent=DEFAULT_SCREENING_CONCURRENCY,
                    with_fund_flow=with_fund_flow,
                    with_market_regime=with_market_regime,
                    with_wyckoff_trigger=with_wyckoff_trigger,
                )
            )

            if valid == 0:
                yield event.plain_result(
                    f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本"
                    f"（<{SHORT_TERM_MIN_BARS} 根），无法计算量价因子。"
                    + DISCLAIMER
                )
                return

            trunc_note = ""
            if len(lines) > top_show:
                trunc_note = (
                    f"💡 共 {len(lines)} 只入榜，仅展示前 {top_show} 条。\n"
                )

            extra_pool = ""
            if regime_snap is not None:
                lab = str(getattr(regime_snap, "label", "") or "")
                att = str(getattr(regime_snap, "attitude", "") or "")
                extra_pool = f"上证盘面: {lab} — {att}\n"
                nlist = getattr(regime_snap, "notes", None) or []
                if nlist:
                    extra_pool += "  · " + "\n  · ".join(
                        str(x) for x in nlist[:4]
                    ) + "\n"

            text = format_short_term_report(
                lines[:top_show],
                title="🎯 短线批量分析（同源因子）",
                candidate_count=attempted,
                valid_count=valid,
                truncation_note=trunc_note,
                min_amount_yi=0.0,
                with_fund_flow=with_fund_flow,
                fund_flow_count=with_flow_n,
                batch_mode=True,
                extra_pool_lines=extra_pool,
                with_wyckoff_trigger=with_wyckoff_trigger,
            )
            yield event.plain_result(text)
        except ImportError as e:
            yield event.plain_result(
                "❌ 需要 akshare 等依赖拉取行情\n请执行: pip install akshare"
            )
            logger.debug("短线批量分析 ImportError: %s", e)
        except Exception as e:
            logger.error(f"短线批量分析出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("威科夫选股")
    async def stock_wyckoff_screen(self, event: AstrMessageEvent):
        """
        威科夫启发式七维打分（大盘/阶段/触发/量价/均线/赔率/仓位），与「短线选股」候选规则相近但因子独立。
        用法: 威科夫选股 [候选数] [输出条数] [额X亿] [板块关键词…] [去涨停] [含ST 可选]
        默认剔除北交所、创业板、科创板（可用 含北交/含创/含科技 等恢复；规则同「量化精选股票」）；
        默认剔除 *ST/ST；「含ST」等同理可保留风险警示股。
        （不支持「加资金流」）
        """
        try:
            tail = strip_command_prefix(get_event_plain_text(event), "威科夫选股")
            (
                max_scan,
                top_n,
                min_amount_yi,
                exclude_bse,
                exclude_chinext,
                exclude_limit_up,
                exclude_star,
                exclude_st,
            ) = parse_wyckoff_screen_tail(tail)

            ex_notes: list[str] = []
            if exclude_bse:
                ex_notes.append("北交所")
            if exclude_chinext:
                ex_notes.append("创业板")
            if exclude_star:
                ex_notes.append("科创板")
            if exclude_limit_up:
                ex_notes.append("涨>9%")
            if exclude_st:
                ex_notes.append("*ST/ST")
            ex_suffix = (
                "，剔除：" + "、".join(ex_notes) if ex_notes else "（全市场）"
            )
            if not exclude_st:
                ex_suffix += "；含风险警示股"

            yield event.plain_result(
                f"📐 威科夫选股{ex_suffix}：按 |涨跌幅| 候选 {max_scan} 只"
                f"（成交额≥{min_amount_yi}亿），输出 TOP {top_n}\n"
                "维度：上证盘面 + 阶段近似 + Spring/SOS/LPS… + 量价 + 均线 + R:R + 仓位建议\n"
                "⏳ 正在拉取上证指数与个股 60 日 K 线…"
            )

            lines, regime, attempted, valid = await screen_wyckoff_stocks(
                self.stock_analyzer,
                self.analyzer,
                max_scan=max_scan,
                min_amount_yi=min_amount_yi,
                max_concurrent=DEFAULT_SCREENING_CONCURRENCY,
                exclude_bse=exclude_bse,
                exclude_chinext=exclude_chinext,
                exclude_star=exclude_star,
                exclude_limit_up=exclude_limit_up,
                exclude_st=exclude_st,
            )
            if attempted == 0:
                yield event.plain_result(
                    "⚠️ 未得到候选标的（成交额阈值或剔除条件过严、或行情列缺失）。"
                    + DISCLAIMER
                )
                return
            if valid == 0:
                yield event.plain_result(
                    f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本"
                    f"（<{WYCKOFF_MIN_BARS} 根），无法进行威科夫启发式评分。"
                    + DISCLAIMER
                )
                return

            trunc = ""
            if len(lines) > top_n:
                trunc = f"💡 共 {len(lines)} 只入榜，仅展示前 {top_n}。\n"

            text = format_wyckoff_report(
                lines[:top_n],
                regime,
                title="📐 威科夫选股（启发式 · 七维）",
                candidate_count=attempted,
                valid_count=valid,
                truncation_note=trunc,
                min_amount_yi=min_amount_yi,
                batch_mode=False,
            )
            yield event.plain_result(text)
        except ImportError as e:
            yield event.plain_result(
                "❌ 需要 akshare 等依赖拉取行情\n请执行: pip install akshare"
            )
            logger.debug("威科夫选股 ImportError: %s", e)
        except Exception as e:
            logger.error(f"威科夫选股出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("威科夫批量分析")
    async def stock_wyckoff_batch(self, event: AstrMessageEvent):
        """
        对指定多只 A 股输出威科夫启发式报告（与「短线批量分析」相同的代码列表格式）。
        用法: 威科夫批量分析 <代码…> [展示条数1-50｜前N]
        「加资金流」若写入将被忽略（本指令不含资金流因子）。
        """
        try:
            tail = strip_command_prefix(get_event_plain_text(event), "威科夫批量分析")
            codes, top_show = parse_wyckoff_batch_tail(tail)
            if not codes:
                yield event.plain_result(
                    "用法: 威科夫批量分析 <代码1> [代码2] … [展示条数]\n"
                    "• 七维：上证盘面 + 阶段近似 + 触发 + 量价 + 均线 + 赔率 + 仓位建议\n"
                    "• 与「短线选股」打分无关；末尾数字或「前N」为展示条数（默认 "
                    f"{DEFAULT_SHORT_TERM_BATCH_TOP}）\n"
                    "• 单次最多 "
                    f"{WYCKOFF_BATCH_MAX_CODES} 只\n"
                    "示例:\n"
                    "  威科夫批量分析 600519 000001 300750\n"
                    "  威科夫批量分析 601398 601288 15\n"
                    + DISCLAIMER
                )
                return

            trunc_head = ""
            if len(codes) > WYCKOFF_BATCH_MAX_CODES:
                codes = codes[:WYCKOFF_BATCH_MAX_CODES]
                trunc_head = (
                    f"⚠️ 单次最多分析 {WYCKOFF_BATCH_MAX_CODES} 只，已截断多余代码。\n"
                )

            yield event.plain_result(
                trunc_head
                + f"📐 威科夫批量分析：共 {len(codes)} 只（启发式七维）\n"
                + "⏳ 正在拉取上证指数与个股 60 日 K 线…"
            )

            lines, regime, attempted, valid = await batch_wyckoff_by_codes(
                self.stock_analyzer,
                self.analyzer,
                codes,
                max_concurrent=DEFAULT_SCREENING_CONCURRENCY,
            )

            if valid == 0:
                yield event.plain_result(
                    f"⚠️ 已对 {attempted} 只拉取日线，均无足够 K 线样本"
                    f"（<{WYCKOFF_MIN_BARS} 根），无法评分。"
                    + DISCLAIMER
                )
                return

            trunc_note = ""
            if len(lines) > top_show:
                trunc_note = (
                    f"💡 共 {len(lines)} 只入榜，仅展示前 {top_show} 条。\n"
                )

            text = format_wyckoff_report(
                lines[:top_show],
                regime,
                title="📐 威科夫批量分析（启发式 · 七维）",
                candidate_count=attempted,
                valid_count=valid,
                truncation_note=trunc_note,
                min_amount_yi=0.0,
                batch_mode=True,
            )
            yield event.plain_result(text)
        except ImportError as e:
            yield event.plain_result(
                "❌ 需要 akshare 等依赖拉取行情\n请执行: pip install akshare"
            )
            logger.debug("威科夫批量分析 ImportError: %s", e)
        except Exception as e:
            logger.error(f"威科夫批量分析出错: {e}")
            yield event.plain_result(f"❌ 执行失败: {e}")

    @filter.command("量化分析")
    async def quant_analysis(self, event: AstrMessageEvent, code: str = ""):
        """
        纯量化分析（无需大模型）
        包含绩效指标、技术指标、策略回测
        用法: 量化分析 [基金代码] [发图|出图|要图|图片]
        默认输出文本；需报告图时在尾部加「发图」等关键词。
        示例: 量化分析 161226、量化分析 161226 发图
        """
        try:
            user_id = event.get_sender_id()
            tail = strip_command_prefix(
                get_event_plain_text(event), "量化分析"
            )
            if not tail.strip() and (str(code or "").strip()):
                tail = str(code).strip()
            code_tail, want_image, _ = parse_stock_smart_analysis_tail(tail)
            fund_code, prefer_otc, normalized_code = self._parse_fund_command_input(
                code_tail, user_id
            )

            yield event.plain_result(f"📊 正在对 {fund_code} 进行量化分析…")

            # 1. 获取基金基本信息
            info = await self.analyzer.get_lof_realtime(
                fund_code, prefer_otc=prefer_otc
            )
            if not info:
                # 区分是基金代码错误还是数据源问题
                if not normalized_code:
                    yield event.plain_result(f"❌ 基金代码不能为空")
                    return

                # 如果代码是6位数字，通常是有效的基金代码格式，但未找到数据
                if len(normalized_code) == 6 and normalized_code.isdigit():
                    # 尝试再次搜索确认是否存在
                    try:
                        search_res = await self.analyzer.search_fund(normalized_code)
                        if not search_res:
                            yield event.plain_result(
                                f"❌ 未找到基金代码 {fund_code}\n"
                                "💡 请检查代码是否正确，或使用「搜索基金 关键词」查找"
                            )
                            return
                    except Exception:
                        pass  # 搜索出错忽略，继续下面的判断

                yield event.plain_result(
                    f"⚠️ 暂时无法获取基金 {fund_code} 的数据\n"
                    "💡 可能是数据源暂时不可用，或该基金为非LOF基金\n"
                    "💡 请稍后重试"
                )
                return

            # 2. 获取60天历史数据
            history = await self.analyzer.get_lof_history(
                fund_code, days=60, prefer_otc=prefer_otc
            )

            if not history or len(history) < 20:
                yield event.plain_result(
                    f"📊 【{info.name}】\n"
                    "⚠️ 历史数据不足（需要至少20天），无法进行量化分析"
                )
                return

            # 3. 使用量化分析器生成报告（无需 LLM）
            quant_report = self.ai_analyzer.get_quant_summary(history)
            signal, score = self.ai_analyzer.get_technical_signal(history)

            # 4. 输出报告
            header = f"""
📈 【{info.name}】量化分析报告
━━━━━━━━━━━━━━━━━
🔢 基金代码: {info.code}
💰 当前价格: {info.latest_price:.4f}
📊 今日涨跌: {info.change_rate:+.2f}%
📅 分析时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}
━━━━━━━━━━━━━━━━━
""".strip()

            footer_notes = (
                "━━━━━━━━━━━━━━━━━\n"
                "📌 指标说明:\n"
                "• 夏普比率 > 1 表示风险调整后收益较好\n"
                "• 最大回撤反映历史最大亏损幅度\n"
                "• VaR 95% 表示95%概率下的最大日亏损\n"
                "• 策略回测基于历史数据模拟\n"
                "━━━━━━━━━━━━━━━━━\n"
                "💡 使用「智能分析」可获取 AI 深度解读"
            )

            if not want_image:
                yield event.plain_result(f"{header}\n\n{quant_report}")
                yield event.plain_result(footer_notes)
                return

            try:
                import markdown

                formatted_content = markdown.markdown(
                    quant_report, extensions=["nl2br", "tables", "fenced_code"]
                )
            except ImportError:
                import re

                formatted_content = re.sub(
                    r"\*\*(.*?)\*\*", r"<strong>\1</strong>", quant_report
                )
                formatted_content = formatted_content.replace("\n", "<br>")

            data = {
                "fund_name": info.name,
                "fund_code": info.code,
                "latest_price": info.latest_price,
                "change_amount": info.change_amount,
                "change_rate": info.change_rate,
                "signal": signal,
                "score": score,
                "analysis_content": formatted_content,
                "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            template_path = self._data_dir / "templates" / "quant_analysis_report.html"
            if not template_path.exists():
                template_path = (
                    Path(__file__).parent / "templates" / "quant_analysis_report.html"
                )

            if not template_path.exists():
                yield event.plain_result(f"{header}\n\n{quant_report}")
                yield event.plain_result(footer_notes)
                return

            rendered_image = False
            if self.use_local_renderer:
                try:
                    img_path = await render_fund_image(
                        template_path=template_path,
                        template_data=data,
                        width=480,
                    )
                    yield event.image_result(img_path)
                    rendered_image = True
                except Exception as e:
                    logger.warning(f"量化分析本地渲染失败，尝试远程: {e}")

            if not rendered_image:
                try:
                    with open(template_path, "r", encoding="utf-8") as f:
                        template_str = f.read()
                    img_url = await self.image_renderer.render_custom_template(
                        tmpl_str=template_str,
                        tmpl_data=data,
                        return_url=True,
                    )
                    yield event.image_result(img_url)
                    rendered_image = True
                except Exception as e:
                    logger.warning(f"量化分析远程渲染失败，回退文本: {e}")

            if not rendered_image:
                yield event.plain_result(f"{header}\n\n{quant_report}")
                yield event.plain_result(footer_notes)

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"量化分析出错: {e}")
            yield event.plain_result(f"❌ 分析失败: {str(e)}")

    def _plot_comparison_chart(
        self,
        history_a: list[dict],
        name_a: str,
        history_b: list[dict],
        name_b: str,
    ) -> str | None:
        """
        绘制双基金对比走势图 (归一化收益率)
        """
        try:
            import base64
            import io
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
            import pandas as pd

            # 设置中文字体
            plt.rcParams["font.sans-serif"] = [
                "SimHei",
                "Arial Unicode MS",
                "Microsoft YaHei",
                "WenQuanYi Micro Hei",
                "sans-serif",
            ]
            plt.rcParams["axes.unicode_minus"] = False

            # 转换为DataFrame
            df_a = pd.DataFrame(history_a)
            df_b = pd.DataFrame(history_b)

            if df_a.empty or df_b.empty:
                return None

            df_a["date"] = pd.to_datetime(df_a["date"])
            df_b["date"] = pd.to_datetime(df_b["date"])

            # 确保按日期排序
            df_a = df_a.sort_values("date")
            df_b = df_b.sort_values("date")

            # 找到公共日期范围
            common_dates = pd.merge(
                df_a[["date"]], df_b[["date"]], on="date", how="inner"
            )["date"]

            if common_dates.empty:
                return None

            # 过滤只保留公共日期的数据
            df_a = df_a[df_a["date"].isin(common_dates)]
            df_b = df_b[df_b["date"].isin(common_dates)]

            # 计算累计收益率 (归一化)
            base_a = df_a.iloc[0]["close"]
            base_b = df_b.iloc[0]["close"]

            if base_a == 0 or base_b == 0:
                return None

            df_a["norm_close"] = (df_a["close"] - base_a) / base_a * 100
            df_b["norm_close"] = (df_b["close"] - base_b) / base_b * 100

            # 绘图
            fig, ax = plt.subplots(figsize=(10, 5), dpi=100)

            ax.plot(
                df_a["date"],
                df_a["norm_close"],
                label=f"{name_a}",
                color="#1890ff",
                linewidth=2,
            )
            ax.plot(
                df_b["date"],
                df_b["norm_close"],
                label=f"{name_b}",
                color="#eb2f96",
                linewidth=2,
            )

            # 填充差异区域
            ax.fill_between(
                df_a["date"],
                df_a["norm_close"],
                df_b["norm_close"],
                where=(df_a["norm_close"] > df_b["norm_close"]),
                interpolate=True,
                color="#1890ff",
                alpha=0.1,
            )
            ax.fill_between(
                df_a["date"],
                df_a["norm_close"],
                df_b["norm_close"],
                where=(df_a["norm_close"] < df_b["norm_close"]),
                interpolate=True,
                color="#eb2f96",
                alpha=0.1,
            )

            ax.set_title("累计收益率对比 (%)", fontsize=14, pad=10)
            ax.grid(True, linestyle="--", alpha=0.3)
            ax.legend(loc="upper left", frameon=True)

            # 格式化Y轴百分比
            import matplotlib.ticker as mtick

            ax.yaxis.set_major_formatter(mtick.PercentFormatter())

            # 日期格式化
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
            plt.gcf().autofmt_xdate()

            plt.tight_layout()

            # 保存
            buffer = io.BytesIO()
            plt.savefig(buffer, format="png", bbox_inches="tight")
            buffer.seek(0)

            image_base64 = base64.b64encode(buffer.read()).decode("utf-8")
            plt.close()

            return image_base64

        except Exception as e:
            logger.error(f"对比绘图失败: {e}")
            return None

    # ============================================================
    # 多智能体博弈分析指令
    # ============================================================
    async def _run_debate_pipeline_for_code(
        self,
        fund_code: str,
        prefer_otc: bool,
        *,
        normalized_explicit_code: Optional[str] = None,
        progress_callback: Any = None,
        with_alignment: bool = False,
    ) -> tuple[Any, Any, Optional[str], Any]:
        """
        拉取行情与资讯并执行 DebateEngine.run_debate。
        成功返回 (DebateResult, FundInfo, None, alignment)，失败返回 (None, None, 文案, None)。
        with_alignment=True 时第四项为 DailyAlignmentMetrics，否则为 None。
        """
        from .stock.debate_alignment_metrics import compute_daily_alignment_metrics
        from .stock.debate_engine import DebateEngine

        info = await self.analyzer.get_lof_realtime(
            fund_code, prefer_otc=prefer_otc
        )
        if not info:
            if (
                normalized_explicit_code
                and len(normalized_explicit_code) == 6
                and normalized_explicit_code.isdigit()
            ):
                try:
                    search_res = await self.analyzer.search_fund(
                        normalized_explicit_code
                    )
                    if not search_res:
                        return None, None, (
                            f"❌ 未找到基金代码 {fund_code}\n"
                            "💡 请检查代码是否正确，或使用「搜索基金 关键词」查找"
                        ), None
                except Exception:
                    pass
            return None, None, (
                f"⚠️ 暂时无法获取基金 {fund_code} 的数据\n"
                "💡 可能是数据源暂时不可用，请稍后重试"
            ), None

        provider = self.context.get_using_provider()
        if not provider:
            return None, None, (
                "❌ 未配置大模型提供商\n"
                "💡 请在 AstrBot 管理面板配置 LLM 提供商后再试"
            ), None

        history_task = self.analyzer.get_lof_history(
            fund_code, days=60, prefer_otc=prefer_otc
        )
        flow_task = self.analyzer._api.get_fund_flow(
            fund_code, days=0, prefer_otc=prefer_otc
        )

        history_data = await history_task
        fund_flow_data = []
        try:
            fund_flow_data = await flow_task
        except Exception as e:
            logger.debug(f"获取资金流向失败: {e}")

        if not history_data or len(history_data) < 10:
            return None, None, (
                f"⚠️ 基金 {fund_code} 历史数据不足，无法进行深度分析"
            ), None

        alignment_obj = None
        if with_alignment:
            alignment_obj = compute_daily_alignment_metrics(
                history_data,
                self.ai_analyzer.quant,
            )

        news_summary = await self.ai_analyzer.get_news_summary(info.name, info.code)
        factors_text = self.ai_analyzer.factors.format_factors_text(info.name)
        global_situation_text = (
            self.ai_analyzer.factors.format_global_situation_text(info.name)
        )

        engine = DebateEngine(self.context)
        debate_result = await engine.run_debate(
            fund_info=info,
            history_data=history_data,
            fund_flow_data=fund_flow_data,
            news_summary=news_summary,
            factors_text=factors_text,
            global_situation_text=global_situation_text,
            quant_analyzer=self.ai_analyzer.quant,
            eastmoney_api=self.analyzer._api,
            progress_callback=progress_callback,
        )
        return debate_result, info, None, alignment_obj if with_alignment else None

    @filter.command("股票智能分析")
    async def multi_agent_debate(self, event: AstrMessageEvent, code: str = ""):
        """
        多智能体博弈分析（6 Agent + 多空辩论 + 博弈论裁定）
        用法: 股票智能分析 [基金/股票代码] [发图|出图|要图|图片] [详进度|详细进度|显示进度]
        默认仅输出结论文本（含参考买入/止损价位演示）；需 HTML 报告图时在尾部加「发图」等关键词；「详进度」可输出各阶段说明。
        示例: 股票智能分析 161226、股票智能分析 600519 发图
        """
        try:
            user_id = event.get_sender_id()
            tail = strip_command_prefix(
                get_event_plain_text(event), "股票智能分析"
            )
            if not tail.strip() and (str(code or "").strip()):
                tail = str(code).strip()
            code_tail, want_image, want_verbose_progress = (
                parse_stock_smart_analysis_tail(tail)
            )
            fund_code, prefer_otc, normalized_code = self._parse_fund_command_input(
                code_tail, user_id
            )

            yield event.plain_result(
                f"⚖️ 正在分析 {fund_code}（约 3～5 分钟）…"
            )

            progress_messages: list[str] = []

            async def on_progress(msg: str):
                progress_messages.append(msg)

            debate_result, info, err, _align = await self._run_debate_pipeline_for_code(
                fund_code,
                prefer_otc,
                normalized_explicit_code=normalized_code,
                progress_callback=on_progress,
                with_alignment=True,
            )
            if err:
                yield event.plain_result(err)
                return

            from .stock.debate_engine import DebateEngine

            engine = DebateEngine(self.context)
            align_dict = (
                _align.as_alignment_dict()
                if _align is not None and hasattr(_align, "as_alignment_dict")
                else None
            )
            summary = engine.format_debate_summary(
                debate_result, alignment_dict=align_dict
            )

            if want_verbose_progress and progress_messages:
                yield event.plain_result("\n".join(progress_messages))

            if not want_image:
                yield event.plain_result(summary)
                return

            template_path = self._data_dir / "templates" / "debate_report.html"
            if not template_path.exists():
                template_path = (
                    Path(__file__).parent / "templates" / "debate_report.html"
                )

            if not template_path.exists():
                yield event.plain_result(summary)
                return

            # 以下为报告图渲染（仅「发图」等关键词时执行）
            def _md_to_html(text: str) -> str:
                """将 Markdown 文本转换为 HTML（内置实现，无外部依赖）"""
                import re as _re

                if not text:
                    return ""

                lines = text.split("\n")
                html_parts: list[str] = []
                i = 0

                while i < len(lines):
                    line = lines[i]
                    stripped = line.strip()

                    # 空行 → 段落间距
                    if not stripped:
                        html_parts.append("")
                        i += 1
                        continue

                    # 标题 h1-h6
                    h_match = _re.match(r"^(#{1,6})\s+(.+)$", stripped)
                    if h_match:
                        level = len(h_match.group(1))
                        content = _inline_md(h_match.group(2))
                        fs = max(18 - level * 2, 12)
                        html_parts.append(
                            f"<h{level} style='margin:8px 0 4px;"
                            f"font-size:{fs}px'>"
                            f"{content}</h{level}>"
                        )
                        i += 1
                        continue

                    # 水平线
                    if _re.match(r"^[-*_]{3,}\s*$", stripped):
                        html_parts.append(
                            "<hr style='border:none;"
                            "border-top:1px solid #e0e0e0;"
                            "margin:8px 0'>"
                        )
                        i += 1
                        continue

                    # 表格（以 | 开头的连续行）
                    if stripped.startswith("|") and "|" in stripped[1:]:
                        table_lines = []
                        while i < len(lines) and lines[i].strip().startswith("|"):
                            table_lines.append(lines[i].strip())
                            i += 1
                        html_parts.append(_table_to_html(table_lines))
                        continue

                    # 无序列表（- / * / + 开头）
                    if _re.match(r"^[-*+]\s+", stripped):
                        items = []
                        while i < len(lines):
                            li_match = _re.match(
                                r"^\s*[-*+]\s+(.+)$", lines[i].strip()
                            )
                            if li_match:
                                items.append(_inline_md(li_match.group(1)))
                                i += 1
                            elif lines[i].strip() == "":
                                i += 1
                                break
                            else:
                                break
                        li_html = "".join(f"<li>{it}</li>" for it in items)
                        html_parts.append(
                            f"<ul style='margin:4px 0;padding-left:20px'>{li_html}</ul>"
                        )
                        continue

                    # 有序列表（1. 开头）
                    if _re.match(r"^\d+[.)]\s+", stripped):
                        items = []
                        while i < len(lines):
                            ol_match = _re.match(
                                r"^\s*\d+[.)]\s+(.+)$", lines[i].strip()
                            )
                            if ol_match:
                                items.append(_inline_md(ol_match.group(1)))
                                i += 1
                            elif lines[i].strip() == "":
                                i += 1
                                break
                            else:
                                break
                        li_html = "".join(f"<li>{it}</li>" for it in items)
                        html_parts.append(
                            f"<ol style='margin:4px 0;padding-left:20px'>{li_html}</ol>"
                        )
                        continue

                    # 普通文本行
                    content = _inline_md(stripped)
                    html_parts.append(
                        f"<p style='margin:4px 0'>{content}</p>"
                    )
                    i += 1

                return "\n".join(html_parts)

            def _inline_md(text: str) -> str:
                """处理行内 Markdown 格式"""
                import re as _re

                # 加粗+斜体 ***text***
                text = _re.sub(
                    r"\*{3}(.+?)\*{3}",
                    r"<strong><em>\1</em></strong>",
                    text,
                )
                # 加粗 **text**
                text = _re.sub(
                    r"\*{2}(.+?)\*{2}",
                    r"<strong>\1</strong>",
                    text,
                )
                # 斜体 *text*
                text = _re.sub(
                    r"\*(.+?)\*",
                    r"<em>\1</em>",
                    text,
                )
                # 行内代码 `code`
                code_style = (
                    "background:#f5f5f5;padding:1px 4px;"
                    "border-radius:3px;font-size:12px"
                )
                text = _re.sub(
                    r"`([^`]+)`",
                    rf"<code style='{code_style}'>\1</code>",
                    text,
                )
                # 链接 [text](url)
                text = _re.sub(
                    r"\[([^\]]+)\]\(([^\)]+)\)",
                    r'<a href="\2">\1</a>',
                    text,
                )
                # emoji 标记保留（🔺🔻等已是 unicode）
                return text

            def _table_to_html(table_lines: list[str]) -> str:
                """将 Markdown 表格行转换为 HTML 表格"""
                import re as _re

                if len(table_lines) < 2:
                    return "<br>".join(table_lines)

                def _parse_row(row: str) -> list[str]:
                    cells = row.strip().strip("|").split("|")
                    return [_inline_md(c.strip()) for c in cells]

                rows = []
                for tl in table_lines:
                    # 跳过分隔行 |---|---|
                    if _re.match(r"^\|[\s\-:|]+\|$", tl):
                        continue
                    rows.append(_parse_row(tl))

                if not rows:
                    return ""

                style = (
                    "width:100%;border-collapse:collapse;font-size:12px;margin:6px 0"
                )
                td_style = "border:1px solid #e0e0e0;padding:4px 6px"
                th_style = f"{td_style};background:#f5f5f5;font-weight:600"

                # 第一行当表头
                header = rows[0]
                th_html = "".join(f"<th style='{th_style}'>{c}</th>" for c in header)
                body_html = ""
                for row in rows[1:]:
                    td_html = "".join(f"<td style='{td_style}'>{c}</td>" for c in row)
                    body_html += f"<tr>{td_html}</tr>"

                return (
                    f"<table style='{style}'>"
                    f"<thead><tr>{th_html}</tr></thead>"
                    f"<tbody>{body_html}</tbody>"
                    f"</table>"
                )

            direction_class_map = {
                "看涨": "bullish",
                "看跌": "bearish",
                "中性": "neutral",
            }
            direction_emoji_map = {"看涨": "📈", "看跌": "📉", "中性": "↔️"}

            agents_data = []
            for r in debate_result.agent_reports:
                agents_data.append(
                    {
                        "emoji": r.agent_emoji,
                        "name": r.agent_name,
                        "direction": r.direction,
                        "direction_class": direction_class_map.get(
                            r.direction, "neutral"
                        ),
                        "confidence": f"{r.confidence:.0f}",
                    }
                )

            tmpl_data = {
                "fund_name": info.name,
                "fund_code": info.code,
                "final_direction": debate_result.final_direction,
                "direction_class": direction_class_map.get(
                    debate_result.final_direction, "neutral"
                ),
                "direction_emoji": direction_emoji_map.get(
                    debate_result.final_direction, "❓"
                ),
                "confidence": f"{debate_result.confidence:.0f}",
                "bull_win_rate": f"{debate_result.bull_win_rate:.0f}",
                "bear_win_rate": f"{debate_result.bear_win_rate:.0f}",
                "agents": agents_data,
                "bull_argument_html": _md_to_html(debate_result.bull_argument),
                "bear_argument_html": _md_to_html(debate_result.bear_argument),
                "judge_verdict_html": _md_to_html(debate_result.judge_verdict),
                "total_llm_calls": debate_result.total_llm_calls,
                "total_time": f"{debate_result.total_time_seconds:.0f}",
                "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            # 渲染图片：本地 → 远程；均失败则仅文本摘要
            rendered_image = False
            if self.use_local_renderer:
                try:
                    img_path = await render_fund_image(
                        template_path=template_path,
                        template_data=tmpl_data,
                        width=520,
                    )
                    yield event.image_result(img_path)
                    rendered_image = True
                except Exception as e:
                    logger.warning(f"本地渲染失败，尝试远程渲染: {e}")

            if not rendered_image:
                try:
                    with open(template_path, encoding="utf-8") as f:
                        template_str = f.read()
                    img_url = await self.image_renderer.render_custom_template(
                        tmpl_str=template_str,
                        tmpl_data=tmpl_data,
                        return_url=True,
                    )
                    yield event.image_result(img_url)
                    rendered_image = True
                except Exception as e:
                    logger.warning(f"远程渲染失败，回退文本输出: {e}")

            yield event.plain_result(summary)

        except ImportError:
            yield event.plain_result(
                "❌ AKShare 库未安装\n请管理员执行: pip install akshare"
            )
        except TimeoutError as e:
            yield event.plain_result(f"⏰ {str(e)}\n💡 数据源响应较慢，请稍后再试")
        except Exception as e:
            logger.error(f"多智能体博弈分析出错: {e}")
            yield event.plain_result(f"❌ 博弈分析失败: {str(e)}")

    @filter.command("基金对比")
    async def fund_compare(
        self, event: AstrMessageEvent, code1: str = "", code2: str = ""
    ):
        """
        对比两只基金的表现
        用法: 基金对比 [代码1] [代码2]
        示例: 基金对比 161226 160220
        """
        if not code1 or not code2:
            yield event.plain_result(
                "❌ 请提供两个基金代码\n用法: 基金对比 代码1 代码2\n示例: 基金对比 161226 160220"
            )
            return

        try:
            p1, o1 = parse_fund_code_hint(code1)
            p2, o2 = parse_fund_code_hint(code2)
            code1 = p1 if p1 else (self._normalize_fund_code(code1) or code1.strip())
            code2 = p2 if p2 else (self._normalize_fund_code(code2) or code2.strip())
            pref1 = o1 if p1 else False
            pref2 = o2 if p2 else False

            yield event.plain_result(f"⚖️ 正在对比基金 {code1} vs {code2}...")

            # 并发获取两个基金的信息和历史数据
            task1 = self.analyzer.get_lof_realtime(code1, prefer_otc=pref1)
            task2 = self.analyzer.get_lof_realtime(code2, prefer_otc=pref2)
            task3 = self.analyzer.get_lof_history(code1, days=60, prefer_otc=pref1)
            task4 = self.analyzer.get_lof_history(code2, days=60, prefer_otc=pref2)

            info1, info2, hist1, hist2 = await asyncio.gather(
                task1, task2, task3, task4
            )

            if not info1:
                # 尝试区分错误原因 (基金1)
                if len(code1) == 6 and code1.isdigit():
                    try:
                        search_res = await self.analyzer.search_fund(code1)
                        if not search_res:
                            yield event.plain_result(
                                f"❌ 未找到基金代码 {code1}\n"
                                "💡 请检查代码是否正确，或使用「搜索基金 关键词」查找"
                            )
                            return
                    except Exception:
                        pass

                yield event.plain_result(
                    f"⚠️ 暂时无法获取基金 {code1} 的数据\n"
                    "💡 可能是数据源暂时不可用，或该基金为非LOF基金\n"
                    "💡 请稍后重试"
                )
                return

            if not info2:
                # 尝试区分错误原因 (基金2)
                if len(code2) == 6 and code2.isdigit():
                    try:
                        search_res = await self.analyzer.search_fund(code2)
                        if not search_res:
                            yield event.plain_result(
                                f"❌ 未找到基金代码 {code2}\n"
                                "💡 请检查代码是否正确，或使用「搜索基金 关键词」查找"
                            )
                            return
                    except Exception:
                        pass

                yield event.plain_result(
                    f"⚠️ 暂时无法获取基金 {code2} 的数据\n"
                    "💡 可能是数据源暂时不可用，或该基金为非LOF基金\n"
                    "💡 请稍后重试"
                )
                return
            if not hist1 or len(hist1) < 10:
                yield event.plain_result(f"⚠️ 基金 {code1} 历史数据不足")
                return
            if not hist2 or len(hist2) < 10:
                yield event.plain_result(f"⚠️ 基金 {code2} 历史数据不足")
                return

            # 计算量化指标
            from .ai_analyzer.quant import QuantAnalyzer

            quant = QuantAnalyzer()

            perf1 = quant.calculate_performance(hist1)
            perf2 = quant.calculate_performance(hist2)

            if not perf1 or not perf2:
                yield event.plain_result("❌ 计算绩效指标失败")
                return

            # 绘制对比图
            plot_img = await asyncio.to_thread(
                self._plot_comparison_chart, hist1, info1.name, hist2, info2.name
            )

            # 准备模板数据
            data = {
                "fund_a_name": info1.name,
                "fund_b_name": info2.name,
                "fund_a_code": info1.code,
                "fund_b_code": info2.code,
                "days": 60,
                "metrics_a": perf1,
                "metrics_b": perf2,
                "plot_img": plot_img,
                "generated_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

            # 渲染模板
            template_path = self._data_dir / "templates" / "comparison_report.html"
            if not template_path.exists():
                template_path = (
                    Path(__file__).parent / "templates" / "comparison_report.html"
                )

            if not template_path.exists():
                yield event.plain_result("❌ 模板文件缺失")
                return

            with open(template_path, "r", encoding="utf-8") as f:
                template_str = f.read()

            img_url = await self.image_renderer.render_custom_template(
                tmpl_str=template_str, tmpl_data=data, return_url=True
            )

            yield event.image_result(img_url)

        except Exception as e:
            logger.error(f"基金对比出错: {e}")
            yield event.plain_result(f"❌ 对比失败: {str(e)}")

    @filter.command("基金帮助")
    async def fund_help(self, event: AstrMessageEvent):
        """显示基金分析插件帮助信息"""
        help_text = """
📊 基金/股票分析插件帮助
━━━━━━━━━━━━━━━━━
💰 贵金属行情:
🔹 今日行情 - 查询金价银价实时行情
━━━━━━━━━━━━━━━━━
📈 A股实时行情 (缓存10分钟):
🔹 股票 <代码> - 查询A股实时行情
🔹 搜索股票 关键词 - 搜索A股股票
━━━━━━━━━━━━━━━━━
📂 东财板块成分 / 场内 ETF (AKShare，多词名保留空格；末尾可选数字为展示条数，默认40、最大200):
🔹 板块概念 <名称> [条数] - 概念板块成份（含行情列）
🔹 板块行业 <名称> [条数] - 行业板块成份
🔹 搜索板块概念 <关键词> [条数] - 子串匹配概念板块名，便于核对东财准确名称
🔹 搜索板块行业 <关键词> [条数] - 子串匹配行业板块名
🔹 搜索ETF <关键词> [条数] - 场内 ETF 简称/代码筛选
🔹 板块量化概念 <名称> [分析上限] [输出条数] - 成份股批量量化排序（默认80/10，|涨跌幅|优先取样）
🔹 板块量化行业 <名称> [分析上限] [输出条数] - 同上（行业成份）
🔹 板块量化ETF <关键词> [分析上限] [输出条数] - ETF 列表名称/代码子串匹配后量化排序（非严格板块成份）
💡 成份为空或报错时，先用「搜索板块*」确认与数据源完全一致的板块名。
💡 板块量化输出与「量化精选」相同（图/文 + 免责声明）；批量拉 K 线可能较慢或受限速。
━━━━━━━━━━━━━━━━━
📊 LOF基金功能:
🔹 基金 [代码] - 查询基金实时行情
🔹 基金分析 [代码] - 技术分析(均线/趋势)
🔹 基金对比 [代码1] [代码2] - ⚖️对比两只基金
🔹 量化精选基金 [分析上限] [输出条数] - 场内 LOF 列表批量量化排序（默认单页/top10，非投资建议）
🔹 量化精选股票 [候选数] [输出条数] [发图可选] [去北交所/含北交 …] … - |涨跌幅|前筛+排序（默认150/10）；默认**文本**结果；需图加「发图」等；默认剔除北交所、创业板、科创板（可用含*恢复）；「去涨停」为剔除涨跌幅>9%，默认关闭
🔹 量化精选股票多空 [候选数] [输出条数] [智能分析上限] … - 同上筛选与剔除；第三数字可选；可选「涨停分析」；默认涨停跳过 LLM；成功行输出方向并附一行参考买入/止损价位（演示）
🔹 量化精选仓位计划 [本金…] [候选数] [输出条数] [智能分析上限] … - 同上辩论流程；须指定本金（如本金100万）；可选「涨停分析」；可选 风险1%/风险0.01、止损2ATR、分0.7、额均2亿、单票20%、最多5只；输出结构化快照（score/ts/日K对齐）与整手仓位演示+JSON（非投资建议）
🔹 打板选股 [YYYYMMDD] [条数] [综合|首板|接力|龙头] … - 涨停池分位数评分；A/B档；末行A档代码串；可写「不含同花顺」「不含天梯」「不含龙虎榜」「去北交所」；须 tushare_token；建议积分≥8000；盘后复盘用
🔹 打板选股多空 [YYYYMMDD] [条数] [辩论只数] [模式…] … - 对A档前K只辩论（K≤15）；结论含参考买入/止损（演示）；须 LLM+tushare_token
🔹 打板资金 - 已合并为「打板选股」，输入会提示新用法
🔹 打板查股 <代码或ts_code> [YYYYMMDD可选] - 输出当日行情+涨停池信息+资金流向分档（DC）；须 tushare_token
🔹 股票回测 <代码> <YYYYMMDD-YYYYMMDD> [仅基准|含策略] - ≥2日可看买入持有；默认<40日跳过策略；示例: 股票回测 600519 20240101-20240115 / … 20240630 含策略
🔹 短线选股 [候选数] [输出条数] [额X亿] [加资金流] [加大盘] [加触发] [剔除关键词…] - |涨跌幅|候选 + 量价因子排序；默认剔北交所/创业板/科创板（可用含*恢复）；「加大盘」拉上证并按档位缩放总分；「加触发」展示威科夫主触发并小额加减分；与「威科夫选股」候选近似但默认打分不同
🔹 短线批量分析 <代码…> [加资金流] [加大盘] [加触发] [展示条数] - 同源量价因子批量打分；单次最多约40只；展示条数默认20、最大50
🔹 威科夫选股 [候选数] [输出条数] [额X亿] [剔除关键词…] - 上证盘面水温 + 阶段/触发/量价/均线/赔率/仓位建议（启发式，不含资金流）；默认剔北交所/创业板/科创板（可用含*恢复）；候选规则贴近短线选股；单次拉上证指数一次；有效样本需不少于约52根日K
🔹 威科夫批量分析 <代码…> [展示条数] - 对指定代码输出同上威科夫七维报告（与短线打分无关）；「加资金流」写入会被忽略；单次最多约40只
💡 东财快照含原生量比；新浪源多为自建近似。分时依赖当日分钟 K。
💡 量化精选基金 / 板块量化等仍以发图为优先（失败则文本）；「量化精选股票」默认文本，加「发图」出图。
💡 并发拉多档 K 线时若频繁断连，多为数据源限流或网络原因，可稍后重试或减少分析数量。
🔹 量化分析 [代码] [发图|出图|要图|图片] - 绩效/技术/回测（无LLM）；默认文本报告；需图加「发图」等
🔹 智能分析 [代码] - 🤖AI量化深度分析
🔹 股票智能分析 [代码] [发图|出图|要图|图片] [详进度…] - ⚖️多智能体博弈；结论文本含参考买入/止损价位（演示）；默认仅结论文本；需报告图时加「发图」等；「详进度」输出阶段说明
🔹 基金历史 [代码] [天数] - 查看历史行情
🔹 搜索基金 关键词 - 搜索LOF基金
🔹 设置基金 代码 - 设置默认基金
🔹 基金帮助 - 显示本帮助
━━━━━━━━━━━━━━━━━
💡 默认基金: 国投瑞银白银期货(LOF)A
   基金代码: 161226
━━━━━━━━━━━━━━━━━
📌 场外基金请在代码后加 .OF 或前缀「场外」；仅六位数字默认走交易所行情
━━━━━━━━━━━━━━━━━
📈 示例:
  • 今日行情 (金银价格)
  • 股票 000001 (平安银行)
  • 搜索股票 茅台
  • 板块概念 车联网 50
  • 搜索板块概念 芯片 30
  • 板块行业 半导体 30
  • 搜索板块行业 白酒 20
  • 搜索ETF 红利 40
  • 板块量化概念 车联网 30 5
  • 板块量化行业 半导体 40 8
  • 板块量化ETF 红利 25 5
  • 基金 161226
  • 基金分析
  • 基金对比 161226 513100
  • 量化精选股票 150 10
  • 量化精选股票 发图 150 10
  • 量化精选股票 含科技 含创业板 150 10
  • 量化精选股票 去涨停 150 10
  • 量化精选股票多空 150 10 3
  • 量化精选股票多空 去创业板 150 10
  • 量化精选仓位计划 本金100万 150 10 3 风险1% 止损2ATR 分0.7
  • 量化精选仓位计划 本金50w 150 10 额均2亿 单票20% 最多5只
  • 短线选股 200 10
  • 短线选股 含创业板 额2亿 200 10
  • 短线选股 加资金流 200 10
  • 短线选股 加触发 200 10
  • 短线选股 加大盘 加触发 150 8
  • 短线批量分析 601398 601288 加大盘 前15
  • 短线批量分析 688981 加资金流
  • 短线批量分析 601398 601288 前15
  • 威科夫选股 200 10
  • 威科夫选股 含创业板 额2亿 150 8
  • 威科夫批量分析 600519 000001
  • 威科夫批量分析 601288 300750 前12
  • 量化精选基金 400 10
  • 量化分析 161226
  • 量化分析 161226 发图
  • 智能分析 161226
  • 股票智能分析 161226
  • 股票智能分析 600519 发图
  • 基金历史 161226 20
  • 搜索基金 白银
━━━━━━━━━━━━━━━━━
🤖 智能分析功能说明:
  调用AI大模型+量化数据，综合分析:
  - 量化绩效评估和风险分析
  - 技术指标深度解读
  - 策略回测结果解读
  - 相关市场动态和新闻
  - 上涨趋势和概率预测
━━━━━━━━━━━━━━━━━
⚖️ 多智能体博弈分析说明:
  6 位 AI 分析师独立研判 + 多空辩论 + 博弈论裁定:
  - 📰 舆情Agent: 情绪因子与市场舆论
  - 🦈 游资Agent: 龙虎榜与游资行为
  - 🛡️ 风控Agent: 政策风险与红线监控
  - 📊 技术Agent: 技术指标与趋势信号
  - 🧩 筹码Agent: 主力行为与筹码分布
  - ⚡ 大单Agent: 实时资金流向与异动
━━━━━━━━━━━━━━━━━
⚠️ 数据来源: AKShare/国际金价网
💡 A股数据缓存10分钟，仅供参考
💡 投资有风险，入市需谨慎！
""".strip()
        yield event.plain_result(help_text)

    async def terminate(self):
        """插件停止时的清理工作"""
        logger.info("基金分析插件已停止")
