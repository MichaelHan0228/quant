"""
信号计算模块
==========
负责计算ETF轮动策略的核心信号，包括：
1. 多周期动量（5日+20日+60日加权）
2. 风险调整动量（动量/波动率）
3. 双均线趋势确认（MA25 > MA60）
4. QDII偏离度检查
5. 波动率择时

信号计算流程：
  1. 对每个ETF计算多周期动量
  2. 计算波动率，得到风险调整动量
  3. 检查双均线趋势是否向上
  4. 检查QDII偏离度（如过高则降权）
  5. 返回所有ETF的信号DataFrame
"""

import math
import pandas as pd
import numpy as np
from . import config as cfg
from .config import (
    DEFAULT_MOM_WEIGHTS, DEFAULT_MA_SHORT, DEFAULT_MA_LONG,
    QDII_CODES, QDII_DEVIATION_THRESHOLD
)


def calc_multi_period_momentum(df: pd.DataFrame, current_idx: int,
                                mom_short: int = 5, mom_mid: int = 20,
                                mom_long: int = 60) -> float:
    """
    计算多周期加权动量（4个周期）。

    两种模式（由 config.USE_DECAY_MOMENTUM 运行时控制）：
      1. 固定权重：{5:0.15, 10:0.25, 20:0.35, 60:0.25}
      2. 指数衰减（新方案）：w(t) = e^(-λt)，λ=0.693/half_life，1个自由度

    参数:
        df: 包含close列的DataFrame
        current_idx: 当前行索引
        mom_short: 短期动量周期（默认5日）
        mom_mid: 中期动量周期（默认20日）
        mom_long: 长期动量周期（默认60日）

    返回:
        加权动量值（百分比），如2.5表示2.5%

    示例:
        >>> mom = calc_multi_period_momentum(df, current_idx=100)
        >>> print(f"动量: {mom:+.2f}%")
    """
    if current_idx < mom_long:
        return 0

    close = df.iloc[current_idx]["close"]
    periods = [mom_short, 10, mom_mid, mom_long]

    if cfg.USE_DECAY_MOMENTUM:
        # 指数衰减：λ = ln(2) / half_life，w(t) = e^(-λt)
        decay = 0.693 / cfg.HALF_LIFE
        raw_weights = [math.exp(-decay * p) for p in periods]
        total = sum(raw_weights)
        weights = [w / total for w in raw_weights]
    else:
        # 固定权重（4个周期）
        weights = [0.15, 0.25, 0.35, 0.25]

    combined = 0
    total_weight = 0
    for period, weight in zip(periods, weights):
        if current_idx >= period:
            old_close = df.iloc[current_idx - period]["close"]
            mom = (close / old_close - 1) * 100  # 收益率百分比
            combined += weight * mom
            total_weight += weight

    return round(combined / total_weight, 2) if total_weight > 0 else 0


def calc_risk_adj_momentum(df: pd.DataFrame, current_idx: int,
                           mom_short: int = 5, mom_mid: int = 20,
                           mom_long: int = 60, vol_window: int = 20) -> float:
    """
    计算风险调整动量（Risk-Adjusted Momentum）。
    
    公式：风险调整动量 = 多周期动量 / 年化波动率
    
    设计思路：
      - 纯动量会偏向高波动资产（如恒生科技）
      - 除以波动率后，选出的是"每单位风险获得最高动量"的ETF
      - 类似夏普比率的思路
    
    参数:
        df: 包含close列的DataFrame
        current_idx: 当前行索引
        mom_short/mid/long: 动量周期
        vol_window: 波动率计算窗口（默认20日）
    
    返回:
        风险调整动量值（无量纲）
    """
    if current_idx < mom_long + vol_window:
        return 0
    
    # 计算多周期动量
    mom = calc_multi_period_momentum(df, current_idx, mom_short, mom_mid, mom_long)
    
    # 计算波动率（年化）
    returns = df["close"].iloc[current_idx-vol_window:current_idx+1].pct_change().dropna()
    vol = returns.std() * np.sqrt(252) * 100  # 年化波动率%
    
    if vol < 0.1:  # 避免除以零
        return 0
    
    return round(mom / vol, 4)


def _calc_ema(series: pd.Series, period: int) -> pd.Series:
    """
    计算EMA（指数移动平均）。

    公式：EMA_today = α × Price + (1-α) × EMA_yesterday
    其中 α = 2 / (period + 1)

    EMA比SMA对近期价格更敏感：
      - SMA：过去N天价格的算术平均，所有天等权重
      - EMA：近期价格权重更高，远期价格指数衰减
      - 同样是25日均线，EMA对3天前的价格变化反应比SMA快约2倍
    """
    return series.ewm(span=period, adjust=False).mean()


def calc_adx(df: pd.DataFrame, period: int = 14) -> float:
    """
    计算ADX（Average Directional Index）趋势强度指标。

    ADX衡量趋势强度，不区分方向：
      - ADX > 25：趋势明确（无论涨跌）
      - ADX < 20：震荡市，无明显趋势
      - 20 <= ADX <= 25：过渡区

    计算步骤：
      1. 计算+DM/-DM（方向运动）
      2. 计算TR（真实波幅）
      3. 平滑得到+DI/-DI
      4. 计算DX = |+DI - -DI| / (+DI + -DI)
      5. ADX = DX的N日平均

    参数:
        df: 包含high/low/close列的DataFrame
        period: ADX计算周期（默认14日）

    返回:
        float，ADX值（0-100），数据不足返回25（中性）
    """
    if len(df) < period * 2 + 10:
        return 25.0  # 数据不足，返回中性值

    high = df["high"]
    low = df["low"]
    close = df["close"]

    # 计算TR（真实波幅）
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # 计算+DM/-DM
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

    # 平滑TR/+DM/-DM（Wilder平滑）
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)

    # 计算DX和ADX
    di_sum = plus_di + minus_di
    di_sum = di_sum.replace(0, 0.001)  # 避免除以零
    dx = 100 * abs(plus_di - minus_di) / di_sum
    adx = dx.ewm(alpha=1/period, adjust=False).mean()

    return round(adx.iloc[-1], 2)


def calc_market_direction(df: pd.DataFrame, period: int = 14) -> tuple:
    """
    计算市场趋势方向（+DI/-DI/ADX）。

    返回:
        (adx, plus_di, minus_di, is_downtrend)
        - is_downtrend: -DI > +DI 时为True（下跌趋势）
    """
    if len(df) < period * 2 + 10:
        return 25.0, 0.0, 0.0, False

    high = df["high"]
    low = df["low"]
    close = df["close"]

    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr)

    di_sum = plus_di + minus_di
    di_sum = di_sum.replace(0, 0.001)
    dx = 100 * abs(plus_di - minus_di) / di_sum
    adx = dx.ewm(alpha=1/period, adjust=False).mean()

    adx_val = round(adx.iloc[-1], 2)
    pdi = round(plus_di.iloc[-1], 2)
    mdi = round(minus_di.iloc[-1], 2)

    return adx_val, pdi, mdi, mdi > pdi


def check_trend(df: pd.DataFrame, current_idx: int,
                ma_short: int = 25, ma_long: int = 60) -> tuple:
    """
    双均线趋势确认。

    趋势判断逻辑（三重确认）：
      1. 收盘价 > 短期均线  →  短期趋势向上
      2. 短期均线 > 长期均线  →  中期趋势向上
      3. 两者同时满足  →  确认趋势向上

    均线类型（由 config.USE_EMA 控制）：
      - SMA（原方案）：简单算术平均，平滑但滞后
      - EMA（新方案）：指数加权平均，对近期价格更敏感，更快捕捉趋势转折

    参数:
        df: 包含close列的DataFrame
        current_idx: 当前行索引
        ma_short: 短期均线周期（默认25日）
        ma_long: 长期均线周期（默认60日）

    返回:
        (trend_up, ma_short_value, ma_long_value)
    """
    if current_idx < ma_long + 5:
        return False, 0, 0

    close = df.iloc[current_idx]["close"]

    if cfg.USE_EMA:
        # EMA：对近期价格更敏感，趋势转折时反应更快
        ema_s = _calc_ema(df["close"].iloc[:current_idx+1], ma_short).iloc[-1]
        ema_l = _calc_ema(df["close"].iloc[:current_idx+1], ma_long).iloc[-1]
        ma_s, ma_l = ema_s, ema_l
    else:
        # SMA（原方案）
        ma_s = df["close"].iloc[current_idx-ma_short+1:current_idx+1].mean()
        ma_l = df["close"].iloc[current_idx-ma_long+1:current_idx+1].mean()

    # 三重确认：位置关系
    trend_up = (close > ma_s) and (ma_s > ma_l)

    # 可选第四重：斜率确认（MA25本身必须在上升）
    if trend_up and cfg.USE_TREND_SLOPE:
        slope_idx = current_idx - cfg.SLOPE_LOOKBACK
        if slope_idx >= ma_long:
            if cfg.USE_EMA:
                prev_ma_s = _calc_ema(df["close"].iloc[:slope_idx+1], ma_short).iloc[-1]
            else:
                prev_ma_s = df["close"].iloc[slope_idx-ma_short+1:slope_idx+1].mean()
            trend_up = ma_s > prev_ma_s  # 短期均线必须在上升

    return trend_up, round(ma_s, 4), round(ma_l, 4)


def calc_market_volatility(all_data: dict, date, lookback: int = 20) -> tuple:
    """
    计算市场整体波动率（用沪深300代表）。
    
    用途：
      - 波动率择时：高波动时减仓或空仓
      - 波动率百分位：当前波动率在历史中的位置
    
    参数:
        all_data: 所有ETF数据字典
        date: 计算日期
        lookback: 波动率计算窗口（默认20日）
    
    返回:
        (volatility, percentile)
        - volatility: 年化波动率（小数，如0.20表示20%）
        - percentile: 波动率在历史中的百分位（0-100）
    """
    code = "510300"  # 用沪深300代表市场
    if code not in all_data:
        return 0, 50
    
    df = all_data[code]
    mask = df["date"] <= pd.Timestamp(date)
    recent = df[mask].tail(lookback + 5)
    
    if len(recent) < lookback:
        return 0, 50
    
    # 计算当前波动率
    returns = recent["close"].tail(lookback).pct_change().dropna()
    vol = returns.std() * np.sqrt(252) * 100  # 年化波动率%
    
    # 计算历史百分位
    hist_mask = df["date"] <= pd.Timestamp(date)
    hist_data = df[hist_mask]
    if len(hist_data) > 252:
        hist_vol = []
        for i in range(252, len(hist_data)):
            r = hist_data["close"].iloc[i-20:i].pct_change().dropna()
            hist_vol.append(r.std() * np.sqrt(252) * 100)
        percentile = sum(1 for v in hist_vol if v < vol) / len(hist_vol) * 100
    else:
        percentile = 50
    
    return round(vol, 2), round(percentile, 1)


def check_qdii_deviation(all_data: dict, code: str, date,
                          ma_short: int = 20) -> tuple:
    """
    QDII ETF价格偏离度检查。
    
    背景：
      - QDII ETF（标普500/恒生科技）盘中价格可能偏离实际净值
      - 溢价过高时买入可能亏损（溢价回归）
      - 偏离度 = (价格 - MA20) / MA20
    
    参数:
        all_data: 所有ETF数据字典
        code: ETF代码
        date: 计算日期
        ma_short: 均线周期（默认20日）
    
    返回:
        (is_overdeviated, deviation_pct)
        - is_overdeviated: bool，是否偏离过大
        - deviation_pct: 偏离百分比（%）
    """
    if code not in QDII_CODES:
        return False, 0
    
    df = all_data[code]
    mask = df["date"] <= pd.Timestamp(date)
    recent = df[mask].tail(ma_short + 5)
    
    if len(recent) < ma_short:
        return False, 0
    
    close = recent.iloc[-1]["close"]
    ma = recent["close"].tail(ma_short).mean()
    deviation = (close - ma) / ma
    
    return deviation > QDII_DEVIATION_THRESHOLD, round(deviation * 100, 2)


def calc_signals(all_data: dict, date,
                 ma_short: int = 25, ma_long: int = 60) -> pd.DataFrame:
    """
    计算所有ETF的综合信号。
    
    整合所有信号维度：
      1. 多周期动量
      2. 风险调整动量
      3. 双均线趋势
      4. QDII偏离度
      5. 市场波动率
    
    参数:
        all_data: 所有ETF数据字典
        date: 计算日期
        ma_short: 短期均线周期
        ma_long: 长期均线周期
    
    返回:
        DataFrame，每行一个ETF，列：
        - code, name, close
        - ma_short, ma_long: 均线值
        - raw_momentum: 原始动量
        - risk_adj_mom: 风险调整动量
        - trend_up: 趋势是否向上
        - qdii_overdev: QDII是否偏离过大
        - qdii_deviation: QDII偏离百分比
        - vol: 市场波动率
        - vol_percentile: 波动率百分位
    """
    signals = []
    date_ts = pd.Timestamp(date)

    # 计算市场波动率
    vol, vol_pct = calc_market_volatility(all_data, date)

    # 计算市场ADX（用沪深300代表整体市场环境）
    market_adx = 25.0  # 默认中性
    if "510300" in all_data:
        hs300_mask = all_data["510300"]["date"] <= date_ts
        hs300_recent = all_data["510300"][hs300_mask]
        if len(hs300_recent) > cfg.ADX_PERIOD * 2 + 10:
            market_adx, _, _, _ = calc_market_direction(hs300_recent, cfg.ADX_PERIOD)

    for code, df in all_data.items():
        mask = df["date"] <= date_ts
        if mask.sum() < ma_long + 10:
            continue

        recent = df[mask]
        current_idx = len(recent) - 1
        close = recent.iloc[current_idx]["close"]

        # 市场趋势过滤：看所属市场的代表指数是否在均线下方
        below_ma = False
        if cfg.USE_MARKET_TREND_FILTER and code not in cfg.TREND_FILTER_EXEMPT:
            idx_code = cfg.MARKET_INDEX_MAP.get(code, code)
            if idx_code in all_data:
                idx_df = all_data[idx_code]
                idx_mask = idx_df["date"] <= date_ts
                idx_recent = idx_df[idx_mask]
                if len(idx_recent) >= cfg.MARKET_TREND_MA:
                    idx_close = idx_recent.iloc[-1]["close"]
                    idx_ma = idx_recent["close"].tail(cfg.MARKET_TREND_MA).mean()
                    below_ma = idx_close < idx_ma

        # 计算各类信号
        risk_adj_mom = calc_risk_adj_momentum(recent, current_idx, 5, ma_short, ma_long)
        raw_mom = calc_multi_period_momentum(recent, current_idx, 5, ma_short, ma_long)
        trend_up, ma_s, ma_l = check_trend(recent, current_idx, ma_short, ma_long)
        qdii_overdev, qdii_dev = check_qdii_deviation(all_data, code, date, ma_short)
        etf_adx, etf_pdi, etf_mdi, etf_downtrend = calc_market_direction(recent, cfg.ADX_PERIOD)

        # QDII偏离度过高时，降低排名权重（惩罚50%）
        if qdii_overdev:
            risk_adj_mom *= 0.5

        signals.append({
            "code": code,
            "name": ETF_POOL[code],
            "close": close,
            "ma_short": ma_s,
            "ma_long": ma_l,
            "raw_momentum": raw_mom,
            "risk_adj_mom": risk_adj_mom,
            "trend_up": trend_up,
            "qdii_overdev": qdii_overdev,
            "qdii_deviation": qdii_dev,
            "vol": vol,
            "vol_percentile": vol_pct,
            "market_adx": market_adx,
            "below_ma": below_ma,
            "etf_adx": etf_adx,
            "etf_downtrend": etf_downtrend,
        })

    return pd.DataFrame(signals)


# 为了兼容性，从config导入ETF_POOL
from .config import ETF_POOL
