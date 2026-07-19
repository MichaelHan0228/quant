"""
风险控制模块
==========
负责策略的止损体系和动态持仓管理。

止损/止盈体系（四层）：
  第一层：个股成本价止损（按ETF波动率分档，日频检查，次日执行）
  第二层：组合高点回撤止损（-10%减半仓，日频检查）
  第三层：暴跌后反弹止损（-5%触发，反弹<50%止损，周频，仅高波动ETF）
  第四层：个股移动止盈（按ETF波动率分档，日频检查，次日执行）

注：第一、四层在 backtest.py 的主循环中实现，本模块提供第一、二层的独立检查函数。

动态持仓管理：
  根据市场波动率和趋势数量动态调整持仓数
"""

import pandas as pd
from . import config as cfg
from .utils import get_price_on_date


def check_stop_loss(holdings: dict, all_data: dict, date) -> list:
    """
    第一层止损：个股成本价止损（按ETF分档）。
    
    逻辑：
      - 计算每只持仓ETF的浮动盈亏
      - 如果从买入价下跌超过阈值，触发止损
      - 返回需要止损的ETF代码列表
    
    触发时机：每天检查
    
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
            stop_loss_threshold = cfg.STOP_LOSS_BY_ETF.get(code, cfg.STOP_LOSS_DEFAULT)
            if pnl_pct < stop_loss_threshold:  # 按ETF分档止损
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
    need_reduce = drawdown < cfg.STOP_LOSS_PORTFOLIO  # 回撤超过10%
    
    return need_reduce, high_water_mark, round(drawdown * 100, 2)


def calc_dynamic_positions(qualified_df: pd.DataFrame,
                           vol_percentile: float) -> int:
    """
    动态计算持仓数量。
    
    根据市场波动率和通过过滤的ETF数量决定持仓数。
    
    参数:
        qualified_df: 符合条件的ETF信号DataFrame（已按动量排序）
        vol_percentile: 市场波动率百分位
    
    返回:
        int，建议持仓数量
    """
    n_trending = len(qualified_df)
    
    # 波动率极端时最多持1个
    if vol_percentile > cfg.VOL_HIGH_THRESHOLD:
        return min(1, n_trending)
    
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
            return min(cfg.MAX_POSITIONS, n_trending)
        else:
            return 2
