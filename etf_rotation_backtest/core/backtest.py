"""
回测引擎模块
==========
实现ETF轮动策略的核心回测逻辑。

回测流程：
  1. 每周五收盘后计算信号（用周五收盘价）
  2. 根据信号确定目标持仓
  3. 下周一开盘执行交易（用周一开盘价+价差）
  4. 记录净值，循环往复

关键设计：
  - 避免数据穿越：周五算信号，周一执行
  - 部分调仓：只操作差异部分，减少换手
  - 信号触发式调仓：信号没变不调仓
  - 三层止损：个股止损+组合止损+单日暴跌止损
"""

import pandas as pd
from .config import INITIAL_CAPITAL, DEFAULT_MA_SHORT, DEFAULT_MA_LONG
from .signals import calc_signals
from .risk_control import (
    check_stop_loss, check_portfolio_stop_loss,
    calc_dynamic_positions
)
from .utils import (
    get_price_on_date, get_next_trading_day,
    calc_buy_price, calc_sell_price, calc_commission,
    filter_by_correlation
)


def calc_portfolio_value(holdings: dict, capital: float,
                         all_data: dict, date) -> float:
    """
    计算当前组合总市值（现金+持仓）。
    
    参数:
        holdings: 持仓字典
        capital: 现金
        all_data: 所有ETF数据
        date: 计算日期
    
    返回:
        float，组合总市值
    """
    value = capital
    for code, holding in holdings.items():
        price = get_price_on_date(all_data, code, date, "close")
        if price:
            value += holding["shares"] * price
    return value


def execute_trades(all_data: dict, holdings: dict, capital: float,
                   sig_date, target_weights: dict,
                   stop_codes: list = None,
                   silent: bool = False) -> tuple:
    """
    执行交易（买卖操作）。
    
    交易逻辑：
      1. 卖出不在目标中的持仓 + 止损的持仓
      2. 买入新进入目标的ETF
      3. 计算佣金和价差成本
    
    参数:
        all_data: 所有ETF数据
        holdings: 当前持仓（会被修改）
        capital: 当前现金
        sig_date: 信号日期（周五）
        target_weights: 目标持仓权重
        stop_codes: 需要止损的代码列表
        silent: 是否静默（不记录交易日志）
    
    返回:
        (updated_holdings, updated_capital, trade_log_list)
    """
    if stop_codes is None:
        stop_codes = []
    
    trade_log = []
    
    # 确定执行日期（下一个交易日，即下周一）
    exec_date = None
    for code in all_data:
        _, next_date = get_next_trading_day(all_data, code, sig_date)
        if next_date is not None:
            exec_date = next_date
            break
    
    if exec_date is None:
        return holdings, capital, trade_log
    
    current_codes = set(holdings.keys())
    target_codes = set(target_weights.keys())
    
    # === 卖出 ===
    # 需要卖出的：不在目标中 + 止损的
    codes_to_sell = (current_codes - target_codes) | set(stop_codes)
    for code in codes_to_sell:
        if code not in holdings:
            continue
        open_price, _ = get_next_trading_day(all_data, code, sig_date)
        if open_price and holdings[code]["shares"] > 0:
            sell_price = calc_sell_price(open_price, code)  # 扣除价差
            sell_amount = holdings[code]["shares"] * sell_price
            commission = calc_commission(sell_amount)
            capital += sell_amount - commission
            
            if not silent:
                trade_log.append({
                    "date": sig_date.strftime("%Y-%m-%d"),
                    "action": "卖出",
                    "code": code,
                    "exec_price": round(sell_price, 3),
                    "shares": holdings[code]["shares"],
                    "commission": round(commission, 2),
                })
            del holdings[code]
    
    # === 买入 ===
    codes_to_buy = target_codes - set(holdings.keys())
    for code in codes_to_buy:
        open_price, _ = get_next_trading_day(all_data, code, sig_date)
        if open_price:
            buy_price = calc_buy_price(open_price, code)  # 加上价差
            portfolio_value = calc_portfolio_value(holdings, capital, all_data, sig_date)
            target_value = portfolio_value * target_weights[code]
            buy_shares = int(target_value / buy_price / 100) * 100  # 整手
            
            if buy_shares > 0:
                buy_amount = buy_shares * buy_price
                commission = calc_commission(buy_amount)
                if capital >= buy_amount + commission:
                    capital -= buy_amount + commission
                    holdings[code] = {"shares": buy_shares, "cost": buy_price}
                    if not silent:
                        trade_log.append({
                            "date": sig_date.strftime("%Y-%m-%d"),
                            "action": "买入",
                            "code": code,
                            "exec_price": round(buy_price, 3),
                            "shares": buy_shares,
                            "commission": round(commission, 2),
                        })
    
    return holdings, capital, trade_log


def run_backtest(all_data: dict, start_date: str, end_date: str,
                 ma_short: int = DEFAULT_MA_SHORT,
                 ma_long: int = DEFAULT_MA_LONG,
                 silent: bool = False) -> tuple:
    """
    运行ETF轮动策略回测。
    
    主回测循环，每周执行一次：
      1. 计算当前持仓市值
      2. 检查止损条件
      3. 计算信号（动量+趋势+波动率）
      4. 确定目标持仓（动态数量+相关性过滤）
      5. 执行交易（部分调仓）
      6. 记录净值
    
    参数:
        all_data: 所有ETF数据字典
        start_date: 回测开始日期
        end_date: 回测结束日期
        ma_short: 短期均线周期
        ma_long: 长期均线周期
        silent: 是否静默模式（敏感性分析时用）
    
    返回:
        (nav_df, trade_log_df)
        - nav_df: 净值曲线DataFrame，列：date, nav
        - trade_log_df: 交易记录DataFrame
    """
    # 生成调仓日期（每周五）
    signal_dates = pd.date_range(start_date, end_date, freq="W-FRI")
    
    # 初始化状态
    capital = INITIAL_CAPITAL
    holdings = {}  # {code: {"shares": N, "cost": price}}
    nav_history = []
    trade_log = []
    last_target_weights = {}  # 记录上周目标，用于信号触发式调仓
    high_water_mark = INITIAL_CAPITAL  # 组合净值高水位
    
    for i, sig_date in enumerate(signal_dates):
        sig_str = sig_date.strftime("%Y-%m-%d")
        
        # === 1. 计算当前持仓市值 ===
        portfolio_value = calc_portfolio_value(holdings, capital, all_data, sig_date)
        
        # === 2. 止损检查 ===
        # 第一层：个股成本价止损
        stop_codes = check_stop_loss(holdings, all_data, sig_date)
        # 第二层：组合高点回撤止损
        portfolio_reduce, high_water_mark, current_dd = check_portfolio_stop_loss(
            capital, holdings, all_data, sig_date, high_water_mark
        )
        
        # === 3. 计算信号 ===
        signals = calc_signals(all_data, sig_date, ma_short, ma_long)
        if signals.empty:
            nav_history.append({"date": sig_date, "nav": portfolio_value})
            continue
        
        # === 4. 确定目标持仓 ===
        # 筛选趋势向上的ETF，按风险调整动量排序
        qualified = signals[signals["trend_up"]].copy()
        qualified = qualified.sort_values("risk_adj_mom", ascending=False)
        vol_pct = signals.iloc[0]["vol_percentile"]
        
        # 动态决定持仓数量
        top_n = calc_dynamic_positions(qualified, vol_pct)
        selected_codes = qualified.head(top_n)["code"].tolist()
        
        # 相关性过滤
        selected_codes = filter_by_correlation(selected_codes, all_data, sig_date)
        
        # 构建目标权重
        target_weights = {}
        if selected_codes:
            weight_per = 1.0 / len(selected_codes)
            for code in selected_codes:
                target_weights[code] = weight_per
        
        # 止损移除
        for code in stop_codes:
            if code in target_weights:
                del target_weights[code]
                if target_weights:
                    w = 1.0 / len(target_weights)
                    target_weights = {c: w for c in target_weights}
        
        # 组合止损减半
        if portfolio_reduce:
            target_weights = {c: w * 0.5 for c, w in target_weights.items()}
        
        # === 5. 信号触发式调仓 ===
        if target_weights == last_target_weights:
            # 信号没变，不调仓，只记录净值
            final_value = calc_portfolio_value(holdings, capital, all_data, sig_date)
            nav_history.append({"date": sig_date, "nav": final_value})
            if not silent and ((i + 1) % 20 == 0 or i == len(signal_dates) - 1):
                ret = (final_value / INITIAL_CAPITAL - 1) * 100
                print(f"  [{sig_str}] 净值={final_value:,.0f} 收益={ret:+.1f}% (信号未变)")
            continue
        
        # === 6. 执行交易 ===
        holdings, capital, new_trades = execute_trades(
            all_data, holdings, capital, sig_date,
            target_weights, stop_codes, silent
        )
        trade_log.extend(new_trades)
        last_target_weights = target_weights.copy()
        
        # === 7. 记录净值 ===
        final_value = calc_portfolio_value(holdings, capital, all_data, sig_date)
        position_desc = ", ".join([
            f"{signals[signals['code']==c].iloc[0]['name']}({w*100:.0f}%)"
            for c, w in target_weights.items()
            if not signals[signals['code']==c].empty
        ]) or "现金"
        
        nav_history.append({"date": sig_date, "nav": final_value, "position": position_desc})
        
        if not silent and ((i + 1) % 20 == 0 or i == len(signal_dates) - 1):
            ret = (final_value / INITIAL_CAPITAL - 1) * 100
            print(f"  [{sig_str}] 净值={final_value:,.0f} 收益={ret:+.1f}% 持仓={position_desc}")
    
    return pd.DataFrame(nav_history), pd.DataFrame(trade_log)
