"""
风险控制模块
==========
负责策略的三层止损体系和动态持仓管理。

三层止损体系：
  第一层：个股成本价止损（-8%）
  第二层：组合高点回撤止损（-10%减半仓）
  第三层：单日暴跌紧急止损（-5%，方案A）

动态持仓管理：
  根据市场波动率和趋势数量动态调整持仓数
"""

import pandas as pd
from .config import (
    STOP_LOSS_SINGLE, STOP_LOSS_PORTFOLIO, DAILY_DROP_THRESHOLD,
    VOL_HIGH_THRESHOLD, VOL_EXTREME_THRESHOLD, MAX_POSITIONS
)
from .utils import get_price_on_date


def check_stop_loss(holdings: dict, all_data: dict, date) -> list:
    """
    第一层止损：个股成本价止损。
    
    逻辑：
      - 计算每只持仓ETF的浮动盈亏
      - 如果从买入价下跌超过8%，触发止损
      - 返回需要止损的ETF代码列表
    
    触发时机：每周五调仓时检查
    
    参数:
        holdings: 持仓字典，{code: {"shares": N, "cost": price}}
        all_data: 所有ETF数据
        date: 检查日期
    
    返回:
        list，需要止损的ETF代码列表
    
    示例:
        >>> stop_codes = check_stop_loss(holdings, all_data, "2024-01-05")
        >>> if stop_codes:
        ...     print(f"止损: {stop_codes}")
    """
    codes_to_stop = []
    for code, holding in holdings.items():
        price = get_price_on_date(all_data, code, date, "close")
        if price:
            # 计算浮动盈亏
            pnl_pct = (price / holding["cost"] - 1)
            if pnl_pct < STOP_LOSS_SINGLE:  # 低于-8%
                codes_to_stop.append(code)
    return codes_to_stop


def check_portfolio_stop_loss(capital: float, holdings: dict, all_data: dict,
                               date, high_water_mark: float) -> tuple:
    """
    第二层止损：组合高点回撤止损。
    
    逻辑：
      - 跟踪组合净值的历史最高点（高水位）
      - 计算当前净值相对于高点的回撤
      - 如果回撤超过10%，触发减半仓（不是清仓）
    
    设计思路：
      - 减半仓而非清仓：保留部分仓位，避免完全踏空反弹
      - 只在极端情况下触发：10%回撤对ETF组合来说已经很大
    
    参数:
        capital: 现金
        holdings: 持仓字典
        all_data: 所有ETF数据
        date: 检查日期
        high_water_mark: 历史最高净值
    
    返回:
        (need_reduce, high_water_mark, drawdown_pct)
        - need_reduce: bool，是否需要减仓
        - high_water_mark: 更新后的高水位
        - drawdown_pct: 当前回撤百分比（负数）
    """
    # 计算当前组合市值
    portfolio_value = capital
    for code, holding in holdings.items():
        price = get_price_on_date(all_data, code, date, "close")
        if price:
            portfolio_value += holding["shares"] * price
    
    # 更新高水位
    high_water_mark = max(high_water_mark, portfolio_value)
    
    # 计算回撤
    drawdown = (portfolio_value / high_water_mark - 1) if high_water_mark > 0 else 0
    
    # 判断是否触发止损
    need_reduce = drawdown < STOP_LOSS_PORTFOLIO  # 回撤超过10%
    
    return need_reduce, high_water_mark, round(drawdown * 100, 2)


def check_daily_drop(all_data: dict, code: str, date,
                     threshold: float = DAILY_DROP_THRESHOLD) -> bool:
    """
    第三层止损：单日暴跌紧急止损（方案A）。
    
    逻辑：
      - 每天监控持仓ETF的当日涨跌幅
      - 如果单日跌幅超过5%，触发紧急止损
      - 下一个交易日开盘执行卖出
    
    设计思路：
      - 应对突发黑天鹅事件（如政策突变、地缘冲突）
      - 不等到周五再处理，及时止损
      - 阈值-5%经过回测验证，只在极端情况触发（3年约3次）
    
    参数:
        all_data: 所有ETF数据
        code: ETF代码
        date: 检查日期
        threshold: 跌幅阈值（默认-5%）
    
    返回:
        bool，是否触发紧急止损
    
    示例:
        >>> for code in holdings:
        ...     if check_daily_drop(all_data, code, today):
        ...         print(f"紧急止损: {code}")
    """
    df = all_data[code]
    mask = df["date"] <= pd.Timestamp(date)
    recent = df[mask]
    
    if len(recent) < 2:
        return False
    
    # 计算当日收益率
    today_close = recent.iloc[-1]["close"]
    yesterday_close = recent.iloc[-2]["close"]
    daily_return = (today_close / yesterday_close - 1)
    
    return daily_return < threshold


def check_break_ma(all_data: dict, code: str, date,
                   ma_period: int = 10) -> bool:
    """
    跌破均线检查（用于方案C变体）。
    
    逻辑：
      - 检查ETF价格是否跌破N日均线
      - 用于周中止损判断
    
    参数:
        all_data: 所有ETF数据
        code: ETF代码
        date: 检查日期
        ma_period: 均线周期（默认10日）
    
    返回:
        bool，是否跌破均线
    """
    df = all_data[code]
    mask = df["date"] <= pd.Timestamp(date)
    recent = df[mask].tail(ma_period + 2)
    
    if len(recent) < ma_period:
        return False
    
    close = recent.iloc[-1]["close"]
    ma = recent["close"].tail(ma_period).mean()
    
    return close < ma


def calc_dynamic_positions(qualified_df: pd.DataFrame,
                           vol_percentile: float) -> int:
    """
    动态计算持仓数量。
    
    根据两个维度决定持仓数：
      1. 市场波动率百分位（择时）
      2. 趋势向上的ETF数量（市场强度）
    
    逻辑：
      - 波动率>90%分位 → 空仓（极端风险）
      - 波动率>80%分位 → 最多持1个（高风险）
      - 正常情况：
        - 0个趋势向上 → 空仓
        - 1个趋势向上 → 持1个
        - 2个趋势向上 → 持2个
        - 3个以上且动量强 → 持3个
    
    参数:
        qualified_df: 符合条件的ETF信号DataFrame（已按动量排序）
        vol_percentile: 市场波动率百分位
    
    返回:
        int，建议持仓数量
    """
    n_trending = len(qualified_df)
    
    # 波动率择时
    if vol_percentile > VOL_EXTREME_THRESHOLD:
        return 0  # 极端高波动，空仓
    if vol_percentile > VOL_HIGH_THRESHOLD:
        return min(1, n_trending)  # 高波动，最多持1个
    
    # 根据趋势数量决定
    if n_trending == 0:
        return 0
    elif n_trending == 1:
        return 1
    elif n_trending == 2:
        return 2
    else:
        # 3个以上趋势向上，检查动量强度
        top_mom = qualified_df.iloc[0]["risk_adj_mom"]
        if top_mom > 0.5:  # 动量很强
            return min(MAX_POSITIONS, n_trending)
        else:
            return 2
