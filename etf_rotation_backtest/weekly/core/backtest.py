"""
回测引擎模块
==========
实现ETF轮动策略的核心回测逻辑。

回测流程：
  1. 每个交易日：检查个股止损（-8%）和移动止盈（-12%从最高点）
  2. 每周五收盘后：计算信号、确定目标持仓
  3. 下周一开盘：执行调仓交易
  4. 止损/止盈触发后：下一个交易日立即执行卖出

四层止损/止盈体系：
  第一层：个股-8%成本价止损（日频）
  第四层：个股-12%移动止盈（日频，从最高点回撤）
  第二层：组合-10%回撤减半仓（日频）
  第三层：暴跌反弹止损（周频，仅恒科）
"""

import pandas as pd
from . import config as cfg
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
    """计算当前组合总市值（现金+持仓）。"""
    value = capital
    for code, holding in holdings.items():
        price = get_price_on_date(all_data, code, date, "close")
        if price:
            value += holding["shares"] * price
    return value


def get_all_trading_days(all_data: dict, start_date: str, end_date: str) -> list:
    """
    获取回测区间内的所有交易日。
    用沪深300的交易日历（流动性最好，数据最全）。
    """
    code = "510300"
    if code not in all_data:
        code = list(all_data.keys())[0]
    df = all_data[code]
    mask = (df["date"] >= pd.Timestamp(start_date)) & (df["date"] <= pd.Timestamp(end_date))
    return df[mask]["date"].tolist()


def execute_stop_loss(all_data: dict, holdings: dict, capital: float,
                      exec_date, stop_codes: list, silent: bool = False) -> tuple:
    """
    执行止损卖出。
    
    参数:
        all_data: 所有ETF数据
        holdings: 当前持仓（会被修改）
        capital: 当前现金
        exec_date: 执行日期（已确定的下一个交易日）
        stop_codes: 需要止损的代码列表
        silent: 是否静默
    
    返回:
        (updated_holdings, updated_capital, trade_log_list)
    """
    trade_log = []
    
    if exec_date is None:
        return holdings, capital, trade_log
    
    for code in stop_codes:
        if code not in holdings:
            continue
        price = get_price_on_date(all_data, code, exec_date, "open")
        if price and holdings[code]["shares"] > 0:
            sell_price = calc_sell_price(price, code)
            sell_amount = holdings[code]["shares"] * sell_price
            commission = calc_commission(sell_amount)
            capital += sell_amount - commission
            
            if not silent:
                trade_log.append({
                    "date": exec_date.strftime("%Y-%m-%d"),
                    "action": "止损卖出",
                    "code": code,
                    "exec_price": round(sell_price, 3),
                    "shares": holdings[code]["shares"],
                    "commission": round(commission, 2),
                })
            del holdings[code]
    
    return holdings, capital, trade_log


def check_weekly_crash_recovery(all_data: dict, code: str, sig_date,
                                 crash_threshold: float = cfg.CRASH_THRESHOLD,
                                 recovery_threshold: float = cfg.RECOVERY_THRESHOLD) -> tuple:
    """
    第三层止损：暴跌后反弹检查（周频，每周五检查）。
    
    仅应用于高波动ETF（如恒生科技）。
    """
    if code not in cfg.HIGH_VOLATILITY_ETF:
        return False, {}
    
    df = all_data[code]
    mask = df["date"] <= pd.Timestamp(sig_date)
    week_data = df[mask].tail(5)  # 最近5个交易日
    
    if len(week_data) < 5:
        return False, {}
    
    daily_returns = week_data["close"].pct_change().dropna()
    min_return = daily_returns.min()
    if min_return >= crash_threshold:
        return False, {}
    
    crash_idx = daily_returns.idxmin()
    crash_day = week_data.loc[crash_idx]
    crash_open = crash_day["open"]
    crash_low = crash_day["low"]
    current_price = week_data.iloc[-1]["close"]
    
    if crash_open <= crash_low:
        recovery = 1.0
    else:
        recovery = (current_price - crash_low) / (crash_open - crash_low)
    
    need_stop = recovery < recovery_threshold
    
    crash_info = {
        "crash_date": week_data.loc[crash_idx, "date"].strftime("%Y-%m-%d"),
        "crash_return": round(min_return * 100, 2),
        "crash_open": round(crash_open, 3),
        "crash_low": round(crash_low, 3),
        "current_price": round(current_price, 3),
        "recovery": round(recovery * 100, 1),
        "need_stop": need_stop,
    }
    
    return need_stop, crash_info


def execute_trades(all_data: dict, holdings: dict, capital: float,
                   sig_date, target_weights: dict,
                   stop_codes: list = None,
                   silent: bool = False) -> tuple:
    """
    执行调仓交易（下周一开盘）。
    """
    if stop_codes is None:
        stop_codes = []
    
    trade_log = []
    
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
    
    # 卖出
    codes_to_sell = (current_codes - target_codes) | set(stop_codes)
    for code in codes_to_sell:
        if code not in holdings:
            continue
        open_price, _ = get_next_trading_day(all_data, code, sig_date)
        if open_price and holdings[code]["shares"] > 0:
            sell_price = calc_sell_price(open_price, code)
            sell_amount = holdings[code]["shares"] * sell_price
            commission = calc_commission(sell_amount)
            capital += sell_amount - commission
            
            if not silent:
                trade_log.append({
                    "date": exec_date.strftime("%Y-%m-%d"),
                    "action": "卖出",
                    "code": code,
                    "exec_price": round(sell_price, 3),
                    "shares": holdings[code]["shares"],
                    "commission": round(commission, 2),
                })
            del holdings[code]
    
    # 买入
    portfolio_value_at_exec = capital
    for code, holding in holdings.items():
        open_price, _ = get_next_trading_day(all_data, code, sig_date)
        if open_price:
            portfolio_value_at_exec += holding["shares"] * open_price
    
    codes_to_buy = target_codes - set(holdings.keys())
    for code in codes_to_buy:
        open_price, _ = get_next_trading_day(all_data, code, sig_date)
        if open_price:
            buy_price = calc_buy_price(open_price, code)
            target_value = portfolio_value_at_exec * target_weights[code]
            buy_shares = int(target_value / buy_price / 100) * 100
            
            if buy_shares > 0:
                buy_amount = buy_shares * buy_price
                commission = calc_commission(buy_amount)
                if capital >= buy_amount + commission:
                    capital -= buy_amount + commission
                    holdings[code] = {"shares": buy_shares, "cost": buy_price}
                    if not silent:
                        trade_log.append({
                            "date": exec_date.strftime("%Y-%m-%d"),
                            "action": "买入",
                            "code": code,
                            "exec_price": round(buy_price, 3),
                            "shares": buy_shares,
                            "commission": round(commission, 2),
                        })
    
    return holdings, capital, trade_log


def run_backtest(all_data: dict, start_date: str, end_date: str,
                 ma_short: int = cfg.DEFAULT_MA_SHORT,
                 ma_long: int = cfg.DEFAULT_MA_LONG,
                 silent: bool = False) -> tuple:
    """
    运行ETF轮动策略回测（日频止损+周频调仓）。
    
    主回测循环，每个交易日执行：
      1. 每天：检查个股止损条件（-8%成本价止损）
      2. 每周五：计算信号、确定目标持仓、检查暴跌止损
      3. 下周一：执行调仓交易
      4. 止损触发后：下一个交易日立即执行卖出
    
    参数:
        all_data: 所有ETF数据字典
        start_date: 回测开始日期
        end_date: 回测结束日期
        ma_short: 短期均线周期
        ma_long: 长期均线周期
        silent: 是否静默模式
    
    返回:
        (nav_df, trade_log_df)
    """
    # 获取所有交易日
    all_trading_days = get_all_trading_days(all_data, start_date, end_date)
    
    # 生成周五信号日集合（用于快速查找）
    signal_dates_set = set(pd.date_range(start_date, end_date, freq="W-FRI"))
    
    # 初始化状态
    capital = cfg.INITIAL_CAPITAL
    holdings = {}
    nav_history = []
    trade_log = []
    last_target_weights = {}
    high_water_mark = cfg.INITIAL_CAPITAL
    crash_events = []
    
    # 每只ETF的历史最高价（用于移动止盈）
    etf_high_watermarks = {}  # {code: highest_price_since_buy}
    
    # 待止损队列：{code: check_date}，下一个交易日执行
    pending_stops = {}
    
    for i, current_date in enumerate(all_trading_days):
        date_str = current_date.strftime("%Y-%m-%d")
        is_friday = current_date in signal_dates_set
        
        # === 每日：执行待止损卖出 ===
        if pending_stops:
            codes_to_sell = list(pending_stops.keys())
            holdings, capital, stop_trades = execute_stop_loss(
                all_data, holdings, capital, 
                current_date,  # 当前日就是检查日的下一个交易日
                codes_to_sell, silent
            )
            trade_log.extend(stop_trades)
            if not silent and stop_trades:
                for t in stop_trades:
                    print(f"  [{date_str}] 🔴 止损/止盈执行 {t['code']}: 卖出{t['shares']}股@{t['exec_price']}")
            # 清理已止损ETF的最高价记录
            for code in codes_to_sell:
                if code in etf_high_watermarks:
                    del etf_high_watermarks[code]
            pending_stops = {}
        
        # === 每日：更新每只ETF的最高价 ===
        for code in holdings:
            price = get_price_on_date(all_data, code, current_date, "high")
            if price:
                if code not in etf_high_watermarks:
                    etf_high_watermarks[code] = price
                else:
                    etf_high_watermarks[code] = max(etf_high_watermarks[code], price)
        
        # === 每日：检查个股止损（-8%成本价）和移动止盈（-12%从最高点） ===
        daily_stop_codes = []
        for code, holding in holdings.items():
            price = get_price_on_date(all_data, code, current_date, "close")
            if price:
                # 第一层：成本价止损（按ETF分档）
                pnl_pct = (price / holding["cost"] - 1)
                stop_loss_threshold = cfg.STOP_LOSS_BY_ETF.get(code, cfg.STOP_LOSS_DEFAULT)
                if pnl_pct < stop_loss_threshold:
                    daily_stop_codes.append(code)
                    if not silent:
                        print(f"  [{date_str}] ⚠️ 触发止损 {code}: 浮亏{pnl_pct*100:.1f}%")
                    continue
                
                # 第四层：移动止盈（从最高点回撤，按ETF分档）
                if code in etf_high_watermarks and etf_high_watermarks[code] > holding["cost"]:
                    drawdown_from_high = (price / etf_high_watermarks[code] - 1)
                    trailing_stop = cfg.TRAILING_STOP_BY_ETF.get(code, cfg.TRAILING_STOP_DEFAULT)
                    if drawdown_from_high < trailing_stop:
                        daily_stop_codes.append(code)
                        if not silent:
                            high = etf_high_watermarks[code]
                            print(f"  [{date_str}] 🎯 触发止盈 {code}: 从最高{high:.3f}回撤{drawdown_from_high*100:.1f}%")
        
        if daily_stop_codes:
            # 记录到待止损队列，下一个交易日执行
            for code in daily_stop_codes:
                pending_stops[code] = current_date
        
        # === 每日：检查组合止损（-10%回撤） ===
        portfolio_value = calc_portfolio_value(holdings, capital, all_data, current_date)
        high_water_mark = max(high_water_mark, portfolio_value)
        current_dd = (portfolio_value / high_water_mark - 1) if high_water_mark > 0 else 0
        portfolio_reduce = current_dd < -0.10
        
        # === 每周五：计算信号并调仓 ===
        if is_friday:
            # 第三层止损：暴跌反弹检查（仅高波动ETF）
            for code in list(holdings.keys()):
                if code not in pending_stops:
                    need_stop, crash_info = check_weekly_crash_recovery(
                        all_data, code, current_date
                    )
                    if need_stop:
                        pending_stops[code] = current_date
                        crash_events.append({"date": date_str, "code": code, **crash_info})
                        if not silent:
                            print(f"  [{date_str}] ⚠️ 暴跌止损 {code}: "
                                  f"本周跌{crash_info['crash_return']:.1f}%, "
                                  f"反弹{crash_info['recovery']:.0f}%")
            
            # 计算信号
            signals = calc_signals(all_data, current_date, ma_short, ma_long)
            if signals.empty:
                nav_history.append({"date": current_date, "nav": portfolio_value})
                continue
            
            # 确定目标持仓
            qualified = signals[signals["trend_up"]].copy()
            qualified = qualified.sort_values("risk_adj_mom", ascending=False)
            vol_pct = signals.iloc[0]["vol_percentile"]
            
            all_trending_codes = qualified["code"].tolist()
            filtered_codes = filter_by_correlation(all_trending_codes, all_data, current_date)
            
            qualified_filtered = qualified[qualified["code"].isin(filtered_codes)]
            top_n = calc_dynamic_positions(qualified_filtered, vol_pct)
            selected_codes = qualified_filtered.head(top_n)["code"].tolist()
            
            # 构建目标权重（等权重分配）
            target_weights = {}
            if selected_codes:
                weight_per = 1.0 / len(selected_codes)
                for code in selected_codes:
                    target_weights[code] = weight_per
            
            # 从目标中移除待止损的
            for code in pending_stops:
                if code in target_weights:
                    del target_weights[code]
                    if target_weights:
                        w = 1.0 / len(target_weights)
                        target_weights = {c: w for c in target_weights}
            
            # 组合止损减半
            if portfolio_reduce:
                target_weights = {c: w * 0.5 for c, w in target_weights.items()}
            
            # 信号触发式调仓
            if set(target_weights.keys()) != set(last_target_weights.keys()):
                # 执行调仓（下周一开盘）
                holdings, capital, new_trades = execute_trades(
                    all_data, holdings, capital, current_date,
                    target_weights, list(pending_stops.keys()), silent
                )
                trade_log.extend(new_trades)
                last_target_weights = target_weights.copy()
                
                # 更新移动止盈水位：清理已卖出的，为新买入的初始化
                for code in list(etf_high_watermarks.keys()):
                    if code not in holdings:
                        del etf_high_watermarks[code]
                for code in holdings:
                    if code not in etf_high_watermarks:
                        price = get_price_on_date(all_data, code, current_date, "close")
                        if price:
                            etf_high_watermarks[code] = price
                
                # 更新净值（调仓后用周五收盘价计算）
                portfolio_value = calc_portfolio_value(holdings, capital, all_data, current_date)
                
                # 打印持仓信息
                if not silent:
                    position_desc = ", ".join([
                        f"{signals[signals['code']==c].iloc[0]['name']}({w*100:.0f}%)"
                        for c, w in target_weights.items()
                        if not signals[signals['code']==c].empty
                    ]) or "现金"
                    ret = (portfolio_value / cfg.INITIAL_CAPITAL - 1) * 100
                    if (i + 1) % 20 == 0 or i == len(all_trading_days) - 1:
                        print(f"  [{date_str}] 净值={portfolio_value:,.0f} 收益={ret:+.1f}% 持仓={position_desc}")
        
        # 记录净值（每周五记录一次）
        if is_friday:
            nav_history.append({"date": current_date, "nav": portfolio_value})
    
    # 打印暴跌事件统计
    if not silent and crash_events:
        print(f"\n暴跌事件统计: 共{len(crash_events)}次")
        for event in crash_events:
            print(f"  {event['date']} {event['code']}: "
                  f"跌{event['crash_return']:.1f}%, 反弹{event['recovery']:.0f}%, "
                  f"{'止损' if event['need_stop'] else '持有'}")
    
    return pd.DataFrame(nav_history), pd.DataFrame(trade_log)
