"""
ETF轮动策略 - 核心模块
"""
from .config import *
from .data_loader import load_all_data, get_etf_klines
from .signals import calc_signals, calc_multi_period_momentum, calc_risk_adj_momentum, check_trend
from .risk_control import check_stop_loss, check_portfolio_stop_loss, check_break_ma, calc_dynamic_positions
from .backtest import run_backtest
from .analysis import calc_metrics, run_sensitivity_analysis, print_sensitivity_results
from .utils import (
    get_price_on_date, get_next_trading_day,
    calc_buy_price, calc_sell_price, calc_commission,
    filter_by_correlation, calc_rolling_correlation
)
