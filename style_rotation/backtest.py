# -*- coding: utf-8 -*-
"""
四象限风格轮动策略回测（v1 最简版）
==========================================
标的：国证风格指数 399372 大盘成长 / 399373 大盘价值 / 399376 小盘成长 / 399377 小盘价值
规则：每周最后一个交易日收盘，按风险调整动量（加权收益率/波动率）选第1名，
     带前2名滞回；若选中指数收盘价 < MA60 则持现金；换仓双边成本 0.002。
数据：mootdx（通达信 TCP 行情），本地 CSV 缓存。

注意（实测结论）：
- mootdx 0.11.x 的 `bars()` 对 399xxx 指数返回错乱数据（指数必须走
  get_index_bars 协议，mootdx 对应接口是 `index_bars()`），故本脚本拉取
  指数日K使用 client.index_bars()。
- 任务给定的 10 台服务器中大部分对该协议返回空响应，实测
  60.191.117.167 / 218.75.126.9 可用，已追加到服务器列表尾部兜底。
"""

import os
import socket
import sys

import numpy as np
import pandas as pd

from mootdx.quotes import Quotes

# ======================== 可调参数（集中在此） ========================
MOM_WINDOWS = (20, 60, 120)          # 动量收益率窗口（交易日）
MOM_WEIGHTS = (0.4, 0.4, 0.2)        # 各窗口权重
VOL_WINDOW = 120                     # 波动率窗口（日收益率标准差）
MA_WINDOW = 60                       # 风控均线周期
HYSTERESIS_TOP_N = 2                 # 滞回：当前持仓仍在前 N 名则不换仓
COST_SWITCH = 0.002                  # 换仓成本（指数->指数，双边合计）
CASH_ANNUAL_RET = 0.02               # 现金年化收益
RISK_FREE = 0.02                     # 夏普计算用无风险利率
START_DATE = '2019-01-01'            # 回测起点
INIT_NAV = 1.0

SYMBOLS = {
    '399372': '大盘成长',
    '399373': '大盘价值',
    '399376': '小盘成长',
    '399377': '小盘价值',
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'data_cache')

# ======================== mootdx 客户端 helper（任务给定，原样使用） ========================
_TDX_SERVERS = [
    ('119.97.185.59', 7709), ('124.70.133.119', 7709), ('116.205.183.150', 7709),
    ('123.60.73.44', 7709),  ('116.205.163.254', 7709), ('121.36.225.169', 7709),
    ('123.60.70.228', 7709), ('124.71.9.153', 7709),    ('110.41.147.114', 7709),
    ('124.71.187.122', 7709),
]

# 实测对指数日K协议返回有效数据的服务器（作为兜底追加）
_EXTRA_SERVERS = [
    ('60.191.117.167', 7709), ('218.75.126.9', 7709),
]


def _probe(ip, port, timeout=2.0):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def tdx_client(market='std'):
    for ip, port in _TDX_SERVERS:
        if _probe(ip, port):
            return Quotes.factory(market=market, server=(ip, port))
    try:
        return Quotes.factory(market=market, bestip=True)
    except Exception:
        pass
    try:
        return Quotes.factory(market=market)
    except Exception as e:
        raise RuntimeError("所有 mootdx 服务器均不可达: %s" % e)


# ======================== 数据获取 ========================
def _validate(df):
    """校验K线数据有效性：非空、日期可解析且在合理区间。"""
    if df is None or len(df) == 0 or 'datetime' not in df.columns:
        return False
    dt = pd.to_datetime(df['datetime'], errors='coerce')
    if dt.isna().any():
        return False
    return dt.dt.year.between(1990, 2031).all()


def fetch_index_daily(symbol, pages=((0, 800), (800, 800), (1600, 800))):
    """分页拉取指数日K并合并。指数必须走 index_bars 协议（bars() 会返回错乱数据）。"""
    servers = _TDX_SERVERS + _EXTRA_SERVERS
    last_err = None
    for ip, port in servers:
        if not _probe(ip, port):
            continue
        try:
            client = Quotes.factory(market='std', server=(ip, port))
            parts = []
            for start, offset in pages:
                df = client.index_bars(symbol=symbol, frequency=9, start=start, offset=offset)
                if df is not None and len(df) > 0:
                    parts.append(df)
            client.close()
            if not parts:
                continue
            df = pd.concat(parts, ignore_index=True)
            if not _validate(df):
                print('  [%s] %s:%s 数据校验失败，换服务器' % (symbol, ip, port))
                continue
            df['datetime'] = pd.to_datetime(df['datetime'])
            df = df.drop_duplicates('datetime').sort_values('datetime').reset_index(drop=True)
            print('  [%s] %s:%s 拉取 %d 根日K  %s -> %s' % (
                symbol, ip, port, len(df),
                df['datetime'].iloc[0].date(), df['datetime'].iloc[-1].date()))
            return df[['datetime', 'open', 'close', 'high', 'low', 'vol', 'amount']]
        except Exception as e:
            last_err = e
            continue
    print('  [%s] 警告：所有服务器均拉取失败 (%s)' % (symbol, last_err))
    return None


def load_data():
    """加载四个指数日线 close，优先读本地缓存。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    closes = {}
    for symbol, name in SYMBOLS.items():
        cache = os.path.join(CACHE_DIR, '%s.csv' % symbol)
        if os.path.exists(cache):
            df = pd.read_csv(cache, parse_dates=['datetime'])
            print('[%s %s] 读缓存 %d 根  %s -> %s' % (
                symbol, name, len(df), df['datetime'].iloc[0].date(), df['datetime'].iloc[-1].date()))
        else:
            print('[%s %s] 缓存不存在，联网拉取...' % (symbol, name))
            df = fetch_index_daily(symbol)
            if df is not None:
                df.to_csv(cache, index=False)
        if df is None or len(df) == 0:
            print('警告：%s %s 数据缺失，该指数将被跳过' % (symbol, name))
            continue
        df = df.set_index('datetime')
        closes[symbol] = df['close'].rename(symbol)
    if len(closes) < 2:
        raise RuntimeError('可用指数不足 2 个，无法回测')
    prices = pd.concat(closes.values(), axis=1).sort_index()
    # 前向填充个别缺失日（指数停牌极少见），再丢弃仍全空的行
    prices = prices.ffill().dropna(how='all')
    return prices


# ======================== 信号计算 ========================
def compute_scores(prices):
    """风险调整动量得分 = 加权收益率 / 波动率。"""
    rets = prices.pct_change()
    weighted = sum(w * prices.pct_change(win) for w, win in zip(MOM_WEIGHTS, MOM_WINDOWS))
    vol = rets.rolling(VOL_WINDOW).std()
    score = weighted / vol.replace(0, np.nan)
    score[prices.isna()] = np.nan
    return score


# ======================== 回测主体 ========================
def run_backtest(prices):
    scores = compute_scores(prices)
    ma = prices.rolling(MA_WINDOW).mean()
    daily_ret = prices.pct_change()

    dates = prices.index
    # 调仓日：每周最后一个交易日
    week_key = dates.to_period('W')
    is_rebalance = pd.Series(dates, index=dates).groupby(week_key).transform('max') == pd.Series(dates, index=dates)
    is_rebalance = is_rebalance.values

    mask = dates >= pd.Timestamp(START_DATE)
    test_dates = dates[mask]

    cash_daily = (1 + CASH_ANNUAL_RET) ** (1 / 252) - 1

    nav = INIT_NAV
    holding = None            # 当前持仓 symbol，None 表示现金
    navs, holds = {}, {}
    n_switch = 0              # 换仓次数（指数->指数）
    weekly_marks = []         # (调仓日, 该日收盘净值)

    for i, dt in enumerate(dates):
        # 1) 当日收益（持仓在昨日收盘确定）；回测起点前不累计（此间必为现金）
        if i > 0 and mask[i]:
            if holding is None:
                r = cash_daily
            else:
                r = daily_ret[holding].iloc[i]
                if np.isnan(r):
                    r = 0.0
            nav *= (1 + r)

        # 2) 收盘后信号与调仓（次日生效），扣除换仓成本
        if mask[i] and is_rebalance[i]:
            sc = scores.iloc[i].dropna()
            target = None
            if len(sc) > 0:
                ranked = sc.sort_values(ascending=False)
                pick = ranked.index[0]
                # 滞回：当前持仓仍在前 N 名则继续持有
                if holding is not None and holding in ranked.index[:HYSTERESIS_TOP_N]:
                    pick = holding
                # MA60 风控：选中指数收盘 < MA60 则持现金
                if not np.isnan(ma[pick].iloc[i]) and prices[pick].iloc[i] >= ma[pick].iloc[i]:
                    target = pick
            if target != holding:
                if target is not None and holding is not None:
                    nav *= (1 - COST_SWITCH)   # 指数->指数，双边成本
                    n_switch += 1
                # 现金进出不扣费
                holding = target
            weekly_marks.append((dt, nav))

        if mask[i]:
            navs[dt] = nav
            holds[dt] = holding

    nav_s = pd.Series(navs)
    hold_s = pd.Series(holds)
    weekly_nav = pd.Series(dict(weekly_marks))
    return nav_s, hold_s, weekly_nav, n_switch


def equal_weight_benchmark(prices):
    """四指数等权基准：每周最后一个交易日收盘再平衡到等权，不计成本。
    从回测起点开始累计（起点净值 1.0）；每日权重漂移后归一化，周末再平衡重置为等权。"""
    bt = prices.loc[prices.index >= pd.Timestamp(START_DATE)]
    daily_ret = bt.pct_change().fillna(0.0)
    dates = bt.index
    week_key = dates.to_period('W')
    is_rebalance = (pd.Series(dates, index=dates).groupby(week_key).transform('max')
                    == pd.Series(dates, index=dates)).values
    n = bt.shape[1]
    weights = np.full(n, 1.0 / n)
    nav = INIT_NAV
    out = {dates[0]: nav}
    for i in range(1, len(dates)):
        r = daily_ret.iloc[i].values
        nav *= float(np.sum(weights * (1 + r)))   # 当日组合收益（按开盘权重）
        weights = weights * (1 + r)
        weights = weights / weights.sum()          # 漂移后归一化，权重和恒为 1
        if is_rebalance[i]:
            weights = np.full(n, 1.0 / n)          # 周频再平衡
        out[dates[i]] = nav
    return pd.Series(out)


# ======================== 绩效指标 ========================
def perf_stats(nav_s, weekly_nav=None, hold_s=None, n_switch=None):
    n_days = len(nav_s)
    years = n_days / 252
    total_ret = nav_s.iloc[-1] / nav_s.iloc[0] - 1
    ann_ret = (nav_s.iloc[-1] / nav_s.iloc[0]) ** (1 / years) - 1
    dr = nav_s.pct_change().dropna()
    ann_vol = dr.std() * np.sqrt(252)
    sharpe = (ann_ret - RISK_FREE) / ann_vol if ann_vol > 0 else np.nan
    dd = nav_s / nav_s.cummax() - 1
    mdd = dd.min()
    calmar = ann_ret / abs(mdd) if mdd < 0 else np.nan
    stats = {
        '年化收益': ann_ret, '年化波动': ann_vol, '最大回撤': mdd,
        '夏普': sharpe, '卡玛': calmar, '累计收益': total_ret,
    }
    if weekly_nav is not None and len(weekly_nav) > 1:
        wr = weekly_nav.pct_change().dropna()
        stats['胜率(周)'] = (wr > 0).mean()
    if hold_s is not None:
        stats['现金占比'] = (hold_s.isna()).mean()
    if n_switch is not None:
        stats['换仓次数'] = n_switch
        stats['年均换仓'] = n_switch / years
    return stats


def fmt_pct(x):
    return 'N/A' if x is None or (isinstance(x, float) and np.isnan(x)) else '%.2f%%' % (x * 100)


# ======================== 主流程 ========================
def main():
    print('=' * 60)
    print('四象限风格轮动策略回测')
    print('=' * 60)

    prices_all = load_data()
    print('\n数据区间: %s -> %s, 共 %d 个交易日, %d 个指数' % (
        prices_all.index[0].date(), prices_all.index[-1].date(),
        len(prices_all), prices_all.shape[1]))

    nav_s, hold_s, weekly_nav, n_switch = run_backtest(prices_all)
    bench_s = equal_weight_benchmark(prices_all)

    # 各指数买入持有（归一化到回测起点）
    bt_prices = prices_all.loc[prices_all.index >= pd.Timestamp(START_DATE)]
    idx_navs = bt_prices / bt_prices.iloc[0] * INIT_NAV

    stats = perf_stats(nav_s, weekly_nav, hold_s, n_switch)
    bstats = perf_stats(bench_s)

    lines = []
    lines.append('四象限风格轮动策略回测 — 绩效汇总')
    lines.append('回测区间: %s -> %s' % (nav_s.index[0].date(), nav_s.index[-1].date()))
    lines.append('标的: %s' % ', '.join('%s %s' % (s, n) for s, n in SYMBOLS.items() if s in prices_all.columns))
    lines.append('参数: 动量窗口%s 权重%s 波动率窗口%d MA%d 滞回前%d 换仓成本%.3f 现金年化%.1f%%'
                 % (MOM_WINDOWS, MOM_WEIGHTS, VOL_WINDOW, MA_WINDOW,
                    HYSTERESIS_TOP_N, COST_SWITCH, CASH_ANNUAL_RET * 100))
    lines.append('-' * 50)
    lines.append('[策略]')
    lines.append('  年化收益:   %s' % fmt_pct(stats['年化收益']))
    lines.append('  累计收益:   %s' % fmt_pct(stats['累计收益']))
    lines.append('  年化波动:   %s' % fmt_pct(stats['年化波动']))
    lines.append('  最大回撤:   %s' % fmt_pct(stats['最大回撤']))
    lines.append('  夏普比率:   %.2f' % stats['夏普'])
    lines.append('  卡玛比率:   %.2f' % stats['卡玛'])
    lines.append('  总换仓次数: %d (年均 %.1f 次)' % (stats['换仓次数'], stats['年均换仓']))
    lines.append('  持现金占比: %s' % fmt_pct(stats['现金占比']))
    lines.append('  周胜率:     %s' % fmt_pct(stats['胜率(周)']))
    lines.append('[等权基准(周再平衡, 不计成本)]')
    lines.append('  年化收益:   %s' % fmt_pct(bstats['年化收益']))
    lines.append('  最大回撤:   %s' % fmt_pct(bstats['最大回撤']))
    lines.append('  夏普比率:   %.2f' % bstats['夏普'])
    lines.append('[超额] 策略年化 - 基准年化 = %s' % fmt_pct(stats['年化收益'] - bstats['年化收益']))
    lines.append('[各指数买入持有]')
    for s in prices_all.columns:
        st = perf_stats(idx_navs[s].dropna())
        lines.append('  %s %s: 年化 %s, 最大回撤 %s' % (s, SYMBOLS[s], fmt_pct(st['年化收益']), fmt_pct(st['最大回撤'])))

    summary = '\n'.join(lines)
    print('\n' + summary)

    # 保存输出
    out_nav = pd.DataFrame({'strategy': nav_s, 'benchmark': bench_s})
    for s in prices_all.columns:
        out_nav[s] = idx_navs[s]
    out_nav['holding'] = hold_s.map(lambda h: 'cash' if h is None else h)
    out_nav.to_csv(os.path.join(BASE_DIR, 'nav.csv'), encoding='utf-8-sig')
    with open(os.path.join(BASE_DIR, 'summary.txt'), 'w', encoding='utf-8') as f:
        f.write(summary + '\n')

    # 绘图
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei']
    plt.rcParams['axes.unicode_minus'] = False

    fig, ax = plt.subplots(figsize=(12, 6))
    ax.plot(nav_s.index, nav_s.values, label='轮动策略', linewidth=1.8, color='red')
    ax.plot(bench_s.index, bench_s.values, label='等权基准', linewidth=1.2, color='black')
    colors = ['#1f77b4', '#2ca02c', '#ff7f0e', '#9467bd']
    for (s, name), c in zip(SYMBOLS.items(), colors):
        if s in idx_navs.columns:
            ax.plot(idx_navs.index, idx_navs[s].values, label=name, linewidth=0.9, alpha=0.75, color=c)
    # 标出现金区间
    cash_days = hold_s[hold_s.isna()].index
    if len(cash_days) > 0:
        ax.scatter(cash_days, nav_s.loc[cash_days], s=1, color='gray', alpha=0.3, label='持现金')
    ax.set_title('四象限风格轮动策略 vs 等权基准 vs 风格指数（%s 起）' % nav_s.index[0].date())
    ax.set_ylabel('净值')
    ax.legend(loc='upper left')
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE_DIR, 'nav.png'), dpi=120)
    print('\n输出文件: nav.csv / summary.txt / nav.png')

    # 年度收益对比（供结论分析）
    yearly = pd.DataFrame({'strategy': nav_s, 'benchmark': bench_s})
    yr = yearly.resample('YE').last()
    yr_ret = yr.pct_change()
    first_year = yearly.index[0].year
    yr_ret.iloc[0] = yr.iloc[0] / INIT_NAV - 1
    print('\n分年度收益(策略 vs 等权基准):')
    for dt, row in yr_ret.iterrows():
        print('  %d: 策略 %s, 基准 %s' % (dt.year, fmt_pct(row['strategy']), fmt_pct(row['benchmark'])))


if __name__ == '__main__':
    main()
