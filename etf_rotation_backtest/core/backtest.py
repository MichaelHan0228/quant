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
  - 三层止损：个股止损+组合止损+暴跌反弹止损
"""

import pandas as pd
from .config import (
    INITIAL_CAPITAL, DEFAULT_MA_SHORT, DEFAULT_MA_LONG,
    CRASH_THRESHOLD, RECOVERY_THRESHOLD
)
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
    """
    value = capital
    for code, holding in holdings.items():
        price = get_price_on_date(all_data, code, date, "close")
        if price:
            value += holding["shares"] * price
    return value


def check_weekly_crash_recovery(all_data: dict, code: str, sig_date,
                                 crash_threshold: float = CRASH_THRESHOLD,
                                 recovery_threshold: float = RECOVERY_THRESHOLD) -> tuple:
    """
    第三层止损：暴跌后反弹检查（周一视角）。
    
    逻辑说明：
      - 检查时间：周五收盘后
      - 检查范围：本周一~周五的数据
      - 找出本周内最大单日跌幅那天（暴跌日）
      - 用暴跌日开盘价作为基准
      - 用周五收盘价作为当前价（周一开盘前能看到的最新价）
      - 计算反弹程度 = (当前价 - 暴跌日最低) / (暴跌日开盘 - 暴跌日最低)
      - 如果反弹程度 >= 50%，不止损（反弹足够）
      - 如果反弹程度 < 50%，止损（反弹不足）
    
    参数:
        all_data: 所有ETF数据
        code: ETF代码
        sig_date: 信号日期（周五）
        crash_threshold: 暴跌阈值（默认-5%）
        recovery_threshold: 反弹阈值（默认50%）
    
    返回:
        (need_stop, crash_info)
        - need_stop: bool，是否需要止损
        - crash_info: dict，暴跌详情
    """
    df = all_data[code]
    mask = df["date"] <= pd.Timestamp(sig_date)
    week_data = df[mask].tail(5)  # 最近5个交易日（周一~周五）
    
    if len(week_data) < 5:
        return False, {}  # 数据不足，不触发
    
    # 计算每天的涨跌幅
    daily_returns = week_data["close"].pct_change().dropna()
    
    # 找出最大跌幅
    min_return = daily_returns.min()
    if min_return >= crash_threshold:
        return False, {}  # 本周没有暴跌
    
    # 找出暴跌日（跌幅最大的那天）
    crash_idx = daily_returns.idxmin()
    crash_day = week_data.loc[crash_idx]
    
    # 暴跌日参数
    crash_open = crash_day["open"]   # 用开盘价作为基准
    crash_low = crash_day["low"]     # 暴跌日最低价
    crash_close = crash_day["close"] # 暴跌日收盘价
    
    # 当前价格 = 周五收盘（周一开盘前能看到的最新价）
    current_price = week_data.iloc[-1]["close"]
    
    # 计算反弹程度
    # 反弹程度 = (当前价 - 暴跌日最低) / (暴跌日开盘 - 暴跌日最低)
    if crash_open <= crash_low:
        recovery = 1.0  # 开盘就是最低，没有下跌空间
    else:
        recovery = (current_price - crash_low) / (crash_open - crash_low)
    
    # 判断是否止损
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
                    "date": exec_date.strftime("%Y-%m-%d"),
                    "action": "卖出",
                    "code": code,
                    "exec_price": round(sell_price, 3),
                    "shares": holdings[code]["shares"],
                    "commission": round(commission, 2),
                })
            del holdings[code]
    
    # === 买入 ===
    # 用执行日（周一开盘）的价格重新计算组合市值，确保仓位准确
    portfolio_value_at_exec = capital
    for code, holding in holdings.items():
        open_price, _ = get_next_trading_day(all_data, code, sig_date)
        if open_price:
            portfolio_value_at_exec += holding["shares"] * open_price
    
    codes_to_buy = target_codes - set(holdings.keys())
    for code in codes_to_buy:
        open_price, _ = get_next_trading_day(all_data, code, sig_date)
        if open_price:
            buy_price = calc_buy_price(open_price, code)  # 加上价差
            target_value = portfolio_value_at_exec * target_weights[code]
            buy_shares = int(target_value / buy_price / 100) * 100  # 整手
            
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
                 ma_short: int = DEFAULT_MA_SHORT,
                 ma_long: int = DEFAULT_MA_LONG,
                 silent: bool = False) -> tuple:
    """
    运行ETF轮动策略回测。
    
    主回测循环，每周执行一次：
      1. 计算当前持仓市值
      2. 检查止损条件（三层止损）
      3. 计算信号（动量+趋势+波动率）
      4. 确定目标持仓（动态数量+相关性过滤）
      5. 执行交易（部分调仓）
      6. 记录净值
    
    时序说明：
      - 每周五收盘后：计算信号、检查止损
      - 下周一开盘：执行交易（用周一开盘价）
    
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
    crash_events = []  # 记录暴跌事件
    
    for i, sig_date in enumerate(signal_dates):
        sig_str = sig_date.strftime("%Y-%m-%d")
        
        # === 1. 计算当前持仓市值（用周五收盘价） ===
        portfolio_value = calc_portfolio_value(holdings, capital, all_data, sig_date)
        
        # === 2. 止损检查 ===
        # 第一层：个股成本价止损
        stop_codes = check_stop_loss(holdings, all_data, sig_date)
        
        # 第二层：组合高点回撤止损
        portfolio_reduce, high_water_mark, current_dd = check_portfolio_stop_loss(
            capital, holdings, all_data, sig_date, high_water_mark
        )
        
        # 第三层：暴跌后反弹检查（本周内是否发生过单日跌幅>5%）
        for code in list(holdings.keys()):
            if code not in stop_codes:
                need_stop, crash_info = check_weekly_crash_recovery(
                    all_data, code, sig_date
                )
                if need_stop:
                    stop_codes.append(code)
                    crash_events.append({
                        "date": sig_str,
                        "code": code,
                        **crash_info
                    })
                    if not silent:
                        print(f"  [{sig_str}] ⚠️ 暴跌止损 {code}: "
                              f"本周跌{crash_info['crash_return']:.1f}%, "
                              f"反弹{crash_info['recovery']:.0f}%")
        
        # === 3. 计算信号（用周五收盘价） ===
        signals = calc_signals(all_data, sig_date, ma_short, ma_long)
        if signals.empty:
            nav_history.append({"date": sig_date, "nav": portfolio_value})
            continue
        
        # === 4. 确定目标持仓 ===
        # 筛选趋势向上的ETF，按风险调整动量排序
        qualified = signals[signals["trend_up"]].copy()
        qualified = qualified.sort_values("risk_adj_mom", ascending=False)
        vol_pct = signals.iloc[0]["vol_percentile"]

        # 先做相关性过滤（从动量最高的开始，剔除高相关的）
        all_trending_codes = qualified["code"].tolist()
        filtered_codes = filter_by_correlation(all_trending_codes, all_data, sig_date)

        # 用过滤后的列表重建qualified，再决定持仓数
        qualified_filtered = qualified[qualified["code"].isin(filtered_codes)]
        top_n = calc_dynamic_positions(qualified_filtered, vol_pct)
        selected_codes = qualified_filtered.head(top_n)["code"].tolist()
        
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
        if set(target_weights.keys()) == set(last_target_weights.keys()):
            # 持仓代码没变，不调仓，只记录净值
            final_value = calc_portfolio_value(holdings, capital, all_data, sig_date)
            nav_history.append({"date": sig_date, "nav": final_value})
            if not silent and ((i + 1) % 20 == 0 or i == len(signal_dates) - 1):
                ret = (final_value / INITIAL_CAPITAL - 1) * 100
                print(f"  [{sig_str}] 净值={final_value:,.0f} 收益={ret:+.1f}% (信号未变)")
            continue
        
        # === 6. 执行交易（下周一开盘） ===
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
    
    # 打印暴跌事件统计
    if not silent and crash_events:
        print(f"\n暴跌事件统计: 共{len(crash_events)}次")
        for event in crash_events:
            print(f"  {event['date']} {event['code']}: "
                  f"跌{event['crash_return']:.1f}%, 反弹{event['recovery']:.0f}%, "
                  f"{'止损' if event['need_stop'] else '持有'}")
    
    return pd.DataFrame(nav_history), pd.DataFrame(trade_log)
