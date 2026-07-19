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
from .signals import calc_signals, calc_adx
from .risk_control import (
    check_stop_loss, check_portfolio_stop_loss,
    calc_dynamic_positions
)
from .utils import (
    get_price_on_date, get_next_trading_day,
    calc_buy_price, calc_sell_price, calc_commission,
    calc_etf_volatility, calc_atr, precompute_atr
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
        open_price = get_price_on_date(all_data, code, exec_date, "open")
        if open_price and holdings[code]["shares"] > 0:
            if cfg.USE_ATR_STOP:
                # ATR动态止损：取 min(开盘价, 买入价 - N×ATR)
                buy_atr = holdings[code].get("buy_atr", 0)
                if buy_atr > 0:
                    stop_price = holdings[code]["cost"] - cfg.ATR_COST_MULTIPLIER * buy_atr
                    exec_price = min(open_price, stop_price)
                else:
                    exec_price = open_price
            elif cfg.USE_STOP_PRICE_OPTIMIZATION:
                # 取 min(开盘价, 止损价)，更贴近跳空低开的实际场景
                stop_threshold = cfg.STOP_LOSS_BY_ETF.get(code, cfg.STOP_LOSS_DEFAULT)
                stop_price = holdings[code]["cost"] * (1 + stop_threshold)
                exec_price = min(open_price, stop_price)
            else:
                exec_price = open_price
            sell_price = calc_sell_price(exec_price, code)
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
                    buy_atr = calc_atr(all_data, code, exec_date, cfg.ATR_PERIOD) if cfg.USE_ATR_STOP else 0
                    holdings[code] = {"shares": buy_shares, "cost": buy_price, "buy_date": exec_date, "buy_atr": buy_atr}
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
    # 预计算ATR（批量，存入DataFrame的atr列）
    if cfg.USE_ATR_STOP:
        precompute_atr(all_data, cfg.ATR_PERIOD)
    
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
    
    # 止损冷却期：{code: cooldown_end_date}，冷却期内不允许重新买入
    stop_cooldown = {}
    
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
                    print(f"  [{date_str}] [STOP] 止损/止盈执行 {t['code']}: 卖出{t['shares']}股@{t['exec_price']}")
            # 清理已止损ETF的最高价记录
            for code in codes_to_sell:
                if code in etf_high_watermarks:
                    del etf_high_watermarks[code]
                # 自适应冷却期：根据当前市场ADX决定冷却天数
                if "510300" in all_data:
                    hs300_mask = all_data["510300"]["date"] <= pd.Timestamp(current_date)
                    hs300_recent = all_data["510300"][hs300_mask]
                    if len(hs300_recent) > cfg.ADX_PERIOD * 2 + 10:
                        cur_adx = calc_adx(hs300_recent, cfg.ADX_PERIOD)
                    else:
                        cur_adx = 25.0
                else:
                    cur_adx = 25.0
                if cur_adx >= cfg.COOLDOWN_STRONG_ADX:
                    cd_days = cfg.COOLDOWN_DAYS_STRONG
                elif cur_adx >= cfg.COOLDOWN_MILD_ADX:
                    cd_days = cfg.COOLDOWN_DAYS_MILD
                else:
                    cd_days = cfg.COOLDOWN_DAYS_WEAK
                stop_cooldown[code] = current_date + pd.Timedelta(days=cd_days)
            pending_stops = {}

        # === 每日：持仓超时退出（持有超时天数且收益低于阈值，换入更强ETF） ===
        if cfg.USE_TIMEOUT_EXIT:
            for code, holding in list(holdings.items()):
                if "buy_date" in holding:
                    days_held = (current_date - holding["buy_date"]).days
                    if days_held > cfg.TIMEOUT_DAYS:
                        price = get_price_on_date(all_data, code, current_date, "close")
                        if price:
                            pnl = (price / holding["cost"] - 1)
                            if pnl < cfg.TIMEOUT_PNL_THRESHOLD and code not in pending_stops:
                                pending_stops[code] = current_date
                                if not silent:
                                    print(f"  [{date_str}] [TIMEOUT] {code}: held {days_held}d, pnl {pnl*100:.1f}%")

        # === 每日：更新每只ETF的最高价 ===
        for code in holdings:
            price = get_price_on_date(all_data, code, current_date, "high")
            if price:
                if code not in etf_high_watermarks:
                    etf_high_watermarks[code] = price
                else:
                    etf_high_watermarks[code] = max(etf_high_watermarks[code], price)
        
        # === 每日：检查个股止损和移动止盈（ATR动态 or 固定百分比） ===
        daily_stop_codes = []
        for code, holding in holdings.items():
            price = get_price_on_date(all_data, code, current_date, "close")
            if price:
                if cfg.USE_ATR_STOP:
                    current_atr = calc_atr(all_data, code, current_date, cfg.ATR_PERIOD)
                    if current_atr > 0:
                        # 第一层：成本价止损 = 买入价 - N×ATR(买入时)
                        buy_atr = holding.get("buy_atr", current_atr)
                        stop_price = holding["cost"] - cfg.ATR_COST_MULTIPLIER * buy_atr
                        if price < stop_price:
                            daily_stop_codes.append(code)
                            if not silent:
                                print(f"  [{date_str}] [ATR-SL] {code}: 价格{price:.3f} < 止损线{stop_price:.3f}")
                            continue
                        # 第四层：移动止盈 = 最高价 - N×ATR(当前)
                        if code in etf_high_watermarks and etf_high_watermarks[code] > holding["cost"]:
                            trail_price = etf_high_watermarks[code] - cfg.ATR_TRAILING_MULTIPLIER * current_atr
                            if price < trail_price:
                                daily_stop_codes.append(code)
                                if not silent:
                                    high = etf_high_watermarks[code]
                                    print(f"  [{date_str}] [ATR-TR] {code}: 价格{price:.3f} < 止盈线{trail_price:.3f} (最高{high:.3f})")
                else:
                    # 固定百分比止损（原方案）
                    pnl_pct = (price / holding["cost"] - 1)
                    stop_loss_threshold = cfg.STOP_LOSS_BY_ETF.get(code, cfg.STOP_LOSS_DEFAULT)
                    if pnl_pct < stop_loss_threshold:
                        daily_stop_codes.append(code)
                        if not silent:
                            print(f"  [{date_str}] [WARN] stop-loss {code}: 浮亏{pnl_pct*100:.1f}%")
                        continue
                    # 移动止盈（从最高点回撤，按ETF分档）
                    if code in etf_high_watermarks and etf_high_watermarks[code] > holding["cost"]:
                        drawdown_from_high = (price / etf_high_watermarks[code] - 1)
                        trailing_stop = cfg.TRAILING_STOP_BY_ETF.get(code, cfg.TRAILING_STOP_DEFAULT)
                        if drawdown_from_high < trailing_stop:
                            daily_stop_codes.append(code)
                            if not silent:
                                high = etf_high_watermarks[code]
                                print(f"  [{date_str}] [TRAIL] trailing-stop {code}: 从最高{high:.3f}回撤{drawdown_from_high*100:.1f}%")
        
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
            # 第三层止损：暴跌反弹检查（仅高波动ETF，已默认禁用）
            if cfg.USE_CRASH_STOP:
                for code in list(holdings.keys()):
                    if code not in pending_stops:
                        need_stop, crash_info = check_weekly_crash_recovery(
                            all_data, code, current_date
                        )
                        if need_stop:
                            pending_stops[code] = current_date
                            crash_events.append({"date": date_str, "code": code, **crash_info})
                            if not silent:
                                print(f"  [{date_str}] [CRASH] crash-stop {code}: "
                                      f"本周跌{crash_info['crash_return']:.1f}%, "
                                      f"反弹{crash_info['recovery']:.0f}%")
            
            # 计算信号
            signals = calc_signals(all_data, current_date, ma_short, ma_long)
            if signals.empty:
                nav_history.append({"date": current_date, "nav": portfolio_value})
                continue

            # 清理过期的冷却期
            expired = [c for c, d in stop_cooldown.items() if d <= current_date]
            for c in expired:
                del stop_cooldown[c]

            # 确定目标持仓
            qualified = signals[signals["trend_up"]].copy()
            # 排除冷却期内的ETF
            if stop_cooldown:
                qualified = qualified[~qualified["code"].isin(stop_cooldown.keys())]
            qualified = qualified.sort_values("risk_adj_mom", ascending=False)
            market_adx = signals.iloc[0]["market_adx"]

            # ADX趋势强度过滤：每只ETF用自己的ADX判断
            all_trending_codes = qualified["code"].tolist()

            # 只保留ADX>=阈值的ETF；如果全部低于阈值，选ADX最高的1只
            adx_qualified = qualified[qualified["etf_adx"] >= cfg.ADX_WEAK_THRESHOLD]
            if adx_qualified.empty:
                adx_qualified = qualified.nlargest(1, "etf_adx")
            qualified_filtered = adx_qualified

            # 每ETF方向过滤：下跌趋势中的ETF失去选资格（ADX够强且-DI>+DI）
            if cfg.USE_DI_DIRECTION_FILTER:
                downtrend_mask = (
                    (qualified_filtered["etf_downtrend"]) &
                    (qualified_filtered["etf_adx"] >= cfg.DI_DOWN_MIN_ADX)
                )
                qualified_filtered = qualified_filtered[~downtrend_mask]

            # 国债ETF仅在弱市中考虑（市场ADX低于阈值时才纳入候选）
            if cfg.BOND_ONLY_IN_WEAK_MARKET and market_adx >= cfg.ADX_WEAK_THRESHOLD:
                qualified_filtered = qualified_filtered[qualified_filtered["code"] != cfg.BOND_CODE]

            # 每只ETF趋势过滤：市场指数低于长期均线的不开新仓
            if cfg.USE_MARKET_TREND_FILTER:
                below_ma_mask = qualified_filtered["below_ma"]
                qualified_filtered = qualified_filtered[~below_ma_mask]

            # 根据通过过滤的ETF数量决定持仓数
            vol_pct = signals.iloc[0]["vol_percentile"]
            if market_adx < cfg.ADX_WEAK_THRESHOLD:
                top_n = min(1, len(qualified_filtered))
            else:
                top_n = calc_dynamic_positions(qualified_filtered, vol_pct)
            # 波动率极端时空仓
            if vol_pct > cfg.VOL_EXTREME_THRESHOLD:
                top_n = 0

            # 回撤渐进式减仓：净值回撤越深，最大持仓数越少（不限制已有持仓）
            if cfg.USE_DRAWDOWN_SCALING and high_water_mark > 0:
                current_dd = portfolio_value / high_water_mark - 1
                if current_dd < cfg.DRAWDOWN_LEVEL_2:
                    top_n = min(1, top_n)
                elif current_dd < cfg.DRAWDOWN_LEVEL_1:
                    top_n = min(2, top_n)

            selected_codes = qualified_filtered.head(top_n)["code"].tolist()
            
            # 构建目标权重
            target_weights = {}
            if selected_codes:
                if cfg.USE_INVERSE_VOL_WEIGHT:
                    # 波动率倒数加权：低波ETF拿更多仓位
                    inv_vols = {}
                    for code in selected_codes:
                        vol = calc_etf_volatility(all_data, code, current_date, lookback=20)
                        inv_vols[code] = 1.0 / vol
                    inv_vol_sum = sum(inv_vols.values())
                    for code in selected_codes:
                        target_weights[code] = inv_vols[code] / inv_vol_sum
                else:
                    # 等权重分配
                    weight_per = 1.0 / len(selected_codes)
                    for code in selected_codes:
                        target_weights[code] = weight_per

                # 集中度限制：单只ETF最大仓位不超过MAX_SINGLE_WEIGHT
                if cfg.USE_MAX_CONCENTRATION:
                    for code in target_weights:
                        if target_weights[code] > cfg.MAX_SINGLE_WEIGHT:
                            target_weights[code] = cfg.MAX_SINGLE_WEIGHT
                    # 注意：剩余资金自然留为现金，不需要额外处理
            
            # 从目标中移除待止损的
            for code in pending_stops:
                if code in target_weights:
                    del target_weights[code]
                    if target_weights:
                        w = 1.0 / len(target_weights)
                        target_weights = {c: w for c in target_weights}

            # 组合止损已禁用：个股止损+移动止盈已足够控制风险
            # 组合止损在回测中证明是负面的（底部割肉、错过反弹）
            
            # 信号触发式调仓
            need_rebalance = set(target_weights.keys()) != set(last_target_weights.keys())

            # 调仓缓冲区：新旧标的动量差距不大时不调仓
            if need_rebalance and cfg.USE_REBALANCE_BUFFER and last_target_weights:
                mom_map = dict(zip(qualified_filtered["code"], qualified_filtered["risk_adj_mom"]))
                old_codes = set(last_target_weights.keys()) - set(pending_stops.keys())
                new_codes = set(target_weights.keys()) - set(pending_stops.keys())
                entering = new_codes - old_codes
                exiting = old_codes - new_codes
                if entering and exiting:
                    avg_new = sum(mom_map.get(c, 0) for c in entering) / max(len(entering), 1)
                    avg_old = sum(mom_map.get(c, 0) for c in exiting) / max(len(exiting), 1)
                    if avg_old > 0 and (avg_new - avg_old) / abs(avg_old) < cfg.REBALANCE_BUFFER_PCT:
                        need_rebalance = False

            if need_rebalance:
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
        
        # 记录净值（每日记录）
        nav_history.append({"date": current_date, "nav": portfolio_value})
    
    # 打印暴跌事件统计
    if not silent and crash_events:
        print(f"\n暴跌事件统计: 共{len(crash_events)}次")
        for event in crash_events:
            print(f"  {event['date']} {event['code']}: "
                  f"跌{event['crash_return']:.1f}%, 反弹{event['recovery']:.0f}%, "
                  f"{'止损' if event['need_stop'] else '持有'}")
    
    return pd.DataFrame(nav_history), pd.DataFrame(trade_log)
