"""因子层：乖离动量 / 斜率动量 / 效率动量 + Z-Score 标准化 + 加权评分。

三因子逻辑还原自猫哥AI量化文章（sklearn LinearRegression 与原文实现一致）：
  1. 乖离动量：bias = close/MA(BIAS_N)，最近 MOMENTUM_DAY 天 bias 归一化后回归斜率 ×10000
  2. 斜率动量：close 归一化后回归，10000 × slope × R²
  3. 效率动量：pivot=(O+H+L+C)/4，100×Δlog(pivot) × (净位移/总波动)
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression


@dataclass
class Params:
    bias_n: int = 25          # 乖离动量均线窗口
    slope_n: int = 25         # 斜率动量回归窗口
    momentum_day: int = 25    # 乖离序列回归天数
    eff_n: int = 25           # 效率动量窗口
    weights: tuple = (0.3, 0.3, 0.4)   # (乖离, 斜率, 效率)
    threshold: float = 1.5    # 调仓阈值
    commission: float = 0.0001  # ETF 佣金万1，双边
    top_k: int = 1            # 持仓评分前 k 名（等权），>1 即分散持仓
    cash_rule: str = "none"   # 'none'/'score'(总分>0才持仓)/'ma'(收盘>MA才持仓)
                              # /'ma_strict'(仅当评分第一过MA才持仓，否则空仓)
    ma_n: int = 25            # cash_rule 含 'ma' 时的均线窗口
    stop_pct: float = 0.0     # 移动止损：从持仓期最高收盘回落该比例则清仓(0=关闭)
    stop_cd: int = 5          # 止损后该标的冷却天数（防立刻买回）
    stop_sell_all: bool = True  # True=任一破位则清空整个组合(原版)；False=只卖破位标的，其余保留
    atr_mult: float = 0.0     # ATR止损：收盘 < 持仓期最高收盘 - mult×ATR 则清仓(0=关闭)
    atr_n: int = 14           # ATR 窗口（Wilder 平滑）
    atr_mult_map: dict = None # 按标的定制ATR倍数 {code: mult}，优先于 atr_mult
    threshold_exit: float = None  # 换出阈值(挑战者超过在持者+该边际即踢出，宜低)。
                                  # None=与 threshold 相同，走原版单一阈值逻辑
    vol_n: int = 0            # 逆波动率加权窗口(日收益率std)，0=关闭(等权)
    weight_mode: str = "equal"  # 'equal' 等权(每日再平衡) / 'score' 换仓时按
                                # 正分数比例定权重，成分不变则随价格漂移不再平衡
    vol_smooth: int = 0       # 波动率 EMA 平滑半衰期(交易日)，0=不平滑
    reb_band: float = 0.0     # 再平衡带：成分不变且各标的权重偏离目标 ≤ 该值
                              # 则不交易(0=关闭，每日精确跟踪目标权重)
    vol_short: int = 5        # 量能因子短期均量窗口
    vol_long: int = 25        # 量能因子长期均量窗口（weights 传 4 个值时启用）

    @classmethod
    def with_window(cls, window: int, **kw):
        return cls(bias_n=window, slope_n=window, momentum_day=window,
                   eff_n=window, **kw)


def bias_momentum(close: pd.Series, bias_n: int, momentum_day: int) -> float:
    bias = close / close.rolling(window=bias_n, min_periods=1).mean()
    recent = bias.iloc[-momentum_day:]
    y = (recent / recent.iloc[0]).values
    x = np.arange(len(y)).reshape(-1, 1)
    lr = LinearRegression().fit(x, y)
    return float(lr.coef_[0]) * 10000


def slope_momentum(close: pd.Series, slope_n: int) -> float:
    seg = close.iloc[-slope_n:]
    y = (seg / seg.iloc[0]).values
    x = np.arange(1, len(y) + 1).reshape(-1, 1)
    lr = LinearRegression().fit(x, y)
    return 10000 * float(lr.coef_[0]) * float(lr.score(x, y))


def efficiency_momentum(df: pd.DataFrame, eff_n: int) -> float:
    seg = df.iloc[-eff_n:]
    pivot = (seg["open"] + seg["high"] + seg["low"] + seg["close"]) / 4.0
    logp = np.log(pivot.values)
    momentum = 100 * (logp[-1] - logp[0])
    direction = abs(logp[-1] - logp[0])
    volatility = np.abs(np.diff(logp)).sum()
    er = direction / volatility if volatility > 0 else 0.0
    return momentum * er


def volume_momentum(volume: pd.Series, vol_short: int, vol_long: int) -> float:
    """量能因子：近 vol_short 日均量 / 近 vol_long 日均量 - 1（0=无量能变化，正=放量）。"""
    seg = volume.iloc[-vol_long:]
    long_mean = seg.mean()
    if not long_mean or long_mean <= 0:
        return 0.0
    return float(seg.iloc[-vol_short:].mean() / long_mean - 1.0)


def factor_matrices(panel: dict, dates: pd.Index, params: Params) -> pd.DataFrame:
    """逐日逐标的计算三因子原始分，返回 MultiIndex 列 (factor, code) 的 DataFrame。

    支持动态标的池：dates 为并集日历，标的未上市（或自身历史不足 warmup 根）
    的日期该标的因子为 NaN，横截面标准化时自动剔除（z-score 按 skipna 计算）。
    因子值只依赖窗口参数，不依赖权重/阈值，参数遍历时可复用。
    """
    warmup = max(params.bias_n, params.slope_n, params.momentum_day,
                 params.eff_n, params.vol_long)
    with_vol = len(params.weights) >= 4  # weights 传 4 个值时启用量能因子
    cols = {}
    for code, df in panel.items():
        df = df.reindex(dates)
        close = df["close"]
        valid_cnt = close.notna().cumsum()  # 截至当日的自身有效K线数
        b, s, e, v = [], [], [], []
        for i in range(len(dates)):
            if valid_cnt.iloc[i] < warmup:
                b.append(np.nan); s.append(np.nan); e.append(np.nan); v.append(np.nan)
                continue
            win_close = close.iloc[:i + 1].dropna()
            win_df = df.iloc[:i + 1].dropna(subset=["close"])
            b.append(bias_momentum(win_close, params.bias_n, params.momentum_day))
            s.append(slope_momentum(win_close, params.slope_n))
            e.append(efficiency_momentum(win_df, params.eff_n))
            if with_vol:
                v.append(volume_momentum(win_df["volume"],
                                         params.vol_short, params.vol_long))
            else:
                v.append(np.nan)
        cols[("bias", code)] = b
        cols[("slope", code)] = s
        cols[("eff", code)] = e
        if with_vol:
            cols[("volr", code)] = v
    out = pd.DataFrame(cols, index=dates)
    out.columns = pd.MultiIndex.from_tuples(out.columns)
    return out


def composite_scores(factors: pd.DataFrame, weights: tuple) -> pd.DataFrame:
    """横截面 Z-Score 标准化后加权，返回 index=date, columns=code 的总分矩阵。

    未上市标的的 NaN 不参与标准化（mean/std 均 skipna），得分保留 NaN，
    选标的时视为不在池内。std=0（当日可选标的同分）时该因子得分置 0。
    """
    names = ["bias", "slope", "eff", "volr"][:len(weights)]
    z = {}
    for fac, w in zip(names, weights):
        f = factors[fac]
        std = f.std(axis=1).replace(0, np.nan)  # 标的同分时防除零
        zfac = f.sub(f.mean(axis=1), axis=0).div(std, axis=0)
        # std==0（或该因子当日无法标准化）的已有值置 0；未上市的 NaN 保留 NaN
        zfac = zfac.fillna(0.0).where(f.notna(), other=np.nan)
        z[fac] = zfac * w
    return sum(z[fac] for fac in names)
