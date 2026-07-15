"""
风险控制模块
==========
负责策略的三层止损体系和动态持仓管理。

三层止损体系：
  第一层：个股成本价止损（-8%）
  第二层：组合高点回撤止损（-10%减半仓）
  第三层：暴跌后反弹止损（-5%触发，反弹<50%止损）

动态持仓管理：
  根据市场波动率和趋势数量动态调整持仓数
"""

import pandas as pd
from .config import (
    STOP_LOSS_SINGLE, STOP_LOSS_PORTFOLIO,
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
    
    参数:
        capital: 现金
        holdings: 持仓字典
        all_data: 所有ETF数据
        date: 检查日期
        high_water_mark: 历史最高净值
    
    返回:
        (need_reduce, high_water_mark, drawdown_pct)
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


def calc_dynamic_positions(qualified_df: pd.DataFrame,
                           vol_percentile: float) -> int:
    """
    动态计算持仓数量。
    
    根据两个维度决定持仓数：
      1. 市场波动率百分位（择时）
      2. 趋势向上的ETF数量（市场强度）
    
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
