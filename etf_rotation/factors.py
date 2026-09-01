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
    mom_vol_pen: float = 0.0  # 低波惩罚：总分 - pen × z(波动率)，0=关闭
    mom_vol_n: int = 25       # 低波惩罚的波动率窗口（日收益率std）
    profit_trigger: float = 0.0  # 浮盈收紧止损：峰值收益 ≥ 该值后启用（0=关闭）
    profit_stop: float = 0.08    # 浮盈收紧后的止损线（替换 stop_pct）
    # ── 平台止损（箱体/前高/筹码分布）──
    # 触发规则：浮盈头寸 = 百分比线与平台线都破才止损（较低者生效，宽松）；
    #           亏损头寸 = 任一线破即止损（较高者生效，严格）。平台无效时回退百分比线。
    platform_mode: str = "none"  # none / swing(波段锚定箱体) / prevhigh(前高) / volprofile(筹码分布)
    platform_q: float = 0.5      # 箱体内止损位置：0=箱底 0.5=中部 1=箱顶
    platform_anchor_n: int = 60  # 波段起点搜索窗口（最近N天最低点=当前段起点）
    platform_box_n: int = 60     # 箱体窗口（起点之前N天）
    platform_max_h: float = 0.30  # 箱体高度上限，超过视为无平台（回退百分比止损）
    platform_ph_n: int = 120     # prevhigh：波段起点之前的回望窗口
    platform_vp_n: int = 120     # volprofile：筹码分布窗口
    platform_vp_bins: int = 30   # volprofile：价格分桶数
    # ── 组合波动率目标 ──
    vol_target: float = 0.0   # 组合年化目标波动率（如 0.20），超出则按比例降仓留现金；0=关闭
    vol_target_n: int = 20    # 实现波动率回望窗口（策略自身日收益 std）
    # ── QDII 溢价过滤 ──
    premium_limit: float = 0.0  # QDII 溢价率上限（如 0.03），超限禁止新买入（不强卖持仓）；0=关闭

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
                 params.eff_n, params.vol_long,
                 params.mom_vol_n if params.mom_vol_pen > 0 else 0)
    with_vol = len(params.weights) >= 4  # weights 传 4 个值时启用量能因子
    with_lv = params.mom_vol_pen > 0     # 低波惩罚：需波动率因子列
    cols = {}
    for code, df in panel.items():
        df = df.reindex(dates)
        close = df["close"]
        valid_cnt = close.notna().cumsum()  # 截至当日的自身有效K线数
        b, s, e, v, lv = [], [], [], [], []
        for i in range(len(dates)):
            if valid_cnt.iloc[i] < warmup:
                b.append(np.nan); s.append(np.nan); e.append(np.nan)
                v.append(np.nan); lv.append(np.nan)
                continue
            win_close = close.iloc[:i + 1].dropna()
            win_df = df.iloc[:i + 1].dropna(subset=["close"])
            b.append(bias_momentum(win_close, params.bias_n, params.momentum_day))
            s.append(slope_momentum(win_close, params.slope_n))
            e.append(efficiency_momentum(win_df, params.eff_n))
            if with_vol:
                v.append(volume_momentum(win_df["volume"],
                                         params.vol_short, params.vol_long))
            if with_lv:
                # 已实现波动率：最近 mom_vol_n 日收益率 std（z-score 时横截面标准化）
                lv.append(float(win_close.pct_change()
                                .iloc[-params.mom_vol_n:].std()))
        cols[("bias", code)] = b
        cols[("slope", code)] = s
        cols[("eff", code)] = e
        if with_vol:
            cols[("volr", code)] = v
        if with_lv:
            cols[("lowvol", code)] = lv
    out = pd.DataFrame(cols, index=dates)
    out.columns = pd.MultiIndex.from_tuples(out.columns)
    return out


def composite_scores(factors: pd.DataFrame, weights: tuple,
                     vol_pen: float = 0.0) -> pd.DataFrame:
    """横截面 Z-Score 标准化后加权，返回 index=date, columns=code 的总分矩阵。

    未上市标的的 NaN 不参与标准化（mean/std 均 skipna），得分保留 NaN，
    选标的时视为不在池内。std=0（当日可选标的同分）时该因子得分置 0。
    vol_pen > 0 时额外减去 z(波动率) × vol_pen（低波惩罚：同动量下偏好波动小的）。
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
    total = sum(z[fac] for fac in names)
    if vol_pen > 0:
        f = factors["lowvol"]
        std = f.std(axis=1).replace(0, np.nan)
        zfac = f.sub(f.mean(axis=1), axis=0).div(std, axis=0)
        zfac = zfac.fillna(0.0).where(f.notna(), other=np.nan)
        total = total - zfac * vol_pen
    return total
