"""
工具函数模块
==========
提供通用的辅助函数，包括：
  - 价格查询
  - 交易日计算
  - 交易成本计算
  - 相关性计算
"""

import pandas as pd
import numpy as np
from .config import (
    QDII_CODES, SPREAD_TICKS, QDII_SPREAD_TICKS,
    ETF_TICK_SIZE, COMMISSION_RATE, MIN_COMMISSION, CORR_THRESHOLD, CORR_WINDOW
)


def get_price_on_date(all_data: dict, code: str, date,
                      price_type: str = "close") -> float:
    """
    获取指定ETF在指定日期的价格。
    
    如果指定日期是非交易日，返回最近一个交易日的价格。
    
    参数:
        all_data: 所有ETF数据字典
        code: ETF代码
        date: 目标日期
        price_type: 价格类型，"close"/"open"/"high"/"low"
    
    返回:
        float，价格值，如果没有数据返回None
    """
    df = all_data[code]
    mask = df["date"] <= pd.Timestamp(date)
    if mask.sum() == 0:
        return None
    return df[mask].iloc[-1][price_type]


def get_next_trading_day(all_data: dict, code: str, after_date) -> tuple:
    """
    获取指定日期之后的第一个交易日。
    
    用途：信号在周五收盘后计算，实际交易在下周一开盘执行。
    
    参数:
        all_data: 所有ETF数据字典
        code: ETF代码
        after_date: 参考日期
    
    返回:
        (open_price, trading_date)
        - open_price: 下一个交易日的开盘价
        - trading_date: 下一个交易日的日期
        如果没有更多数据，返回(None, None)
    """
    df = all_data[code]
    future = df[df["date"] > pd.Timestamp(after_date)]
    if future.empty:
        return None, None
    return future.iloc[0]["open"], future.iloc[0]["date"]


def calc_buy_price(raw_price: float, code: str) -> float:
    """
    计算买入实际成本（加价差）。
    
    买入时需要支付的价格 = 中间价 + 半边价差
    价差大小取决于ETF类型：
      - 普通ETF：2个最小变动单位(0.002元)
      - QDII ETF：3个最小变动单位(0.003元)
    
    参数:
        raw_price: 原始价格（如开盘价）
        code: ETF代码
    
    返回:
        float，实际买入价格
    """
    ticks = QDII_SPREAD_TICKS if code in QDII_CODES else SPREAD_TICKS
    return raw_price + ticks * ETF_TICK_SIZE


def calc_sell_price(raw_price: float, code: str) -> float:
    """
    计算卖出实际到手（减价差）。
    
    卖出时实际收到的价格 = 中间价 - 半边价差
    
    参数:
        raw_price: 原始价格
        code: ETF代码
    
    返回:
        float，实际卖出价格
    """
    ticks = QDII_SPREAD_TICKS if code in QDII_CODES else SPREAD_TICKS
    return raw_price - ticks * ETF_TICK_SIZE


def calc_commission(amount: float) -> float:
    """
    计算交易佣金。
    
    ETF佣金规则：
      - 费率：万1.5（0.015%）
      - 最低：5元/笔
      - 无印花税（ETF免征）
    
    参数:
        amount: 交易金额
    
    返回:
        float，佣金金额（最低5元）
    """
    fee = amount * COMMISSION_RATE
    return max(fee, MIN_COMMISSION)


def calc_rolling_correlation(all_data: dict, code1: str, code2: str,
                              date, window: int = CORR_WINDOW) -> float:
    """
    计算两个ETF的滚动相关性。
    
    用途：相关性过滤，避免同时持有高相关的ETF。
    
    参数:
        all_data: 所有ETF数据字典
        code1/code2: ETF代码
        date: 计算日期
        window: 计算窗口（默认60日）
    
    返回:
        float，相关系数（-1到1）
    """
    if code1 not in all_data or code2 not in all_data:
        return 0
    
    df1 = all_data[code1]
    df2 = all_data[code2]
    
    mask1 = df1["date"] <= pd.Timestamp(date)
    mask2 = df2["date"] <= pd.Timestamp(date)
    
    ret1 = df1[mask1]["close"].tail(window).pct_change().dropna()
    ret2 = df2[mask2]["close"].tail(window).pct_change().dropna()
    
    min_len = min(len(ret1), len(ret2))
    if min_len < 20:
        return 0
    
    corr = ret1.tail(min_len).values
    corr2 = ret2.tail(min_len).values
    
    return round(np.corrcoef(corr, corr2)[0, 1], 3)


def filter_by_correlation(selected_codes: list, all_data: dict, date,
                          threshold: float = CORR_THRESHOLD) -> list:
    """
    相关性过滤：避免同时持有高相关的ETF。
    
    逻辑：
      - 从动量排名最高的ETF开始
      - 依次检查与已选中ETF的相关性
      - 如果相关性>0.7，跳过该ETF，选下一个
    
    参数:
        selected_codes: 按动量排序的ETF代码列表
        all_data: 所有ETF数据
        date: 计算日期
        threshold: 相关性阈值（默认0.7）
    
    返回:
        list，过滤后的ETF代码列表
    """
    if len(selected_codes) <= 1:
        return selected_codes
    
    filtered = [selected_codes[0]]
    
    for code in selected_codes[1:]:
        # 检查与已选中ETF的相关性
        is_correlated = False
        for existing_code in filtered:
            corr = calc_rolling_correlation(all_data, existing_code, code, date)
            if abs(corr) > threshold:
                is_correlated = True
                break
        
        if not is_correlated:
            filtered.append(code)
    
    return filtered
