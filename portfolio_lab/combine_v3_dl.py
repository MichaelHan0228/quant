"""
组合层回测：全天候 v3 + 可转债双低
======================================
两条腿分别取各自回测的周频净值（本脚本不重算策略本身）：
  - v3：all-weather-etf/output/combo_nav_combo20_erp_长样本.csv（v3 正式版）
  - 双低：cb_double_low/output/nav_double_low.csv（推荐参数：评级开/上限130/持仓15）
合并方式：周收益序列 → 按季度末再平衡到目标权重（组合层调仓成本 0.05% 计入）
区间：两腿净值的交集（双低 2018-01 起）
权重网格：双低占比 0% / 20% / 30% / 40% / 50%
"""
import os
import math

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
Q = os.path.dirname(BASE)  # quant 根目录
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

V3_NAV = os.path.join(Q, "all-weather-etf", "output", "combo_nav_combo20_erp_长样本.csv")
DL_NAV = os.path.join(Q, "cb_double_low", "output", "nav_double_low.csv")
REBAL_COST = 0.0005   # 组合层季度再平衡的摩擦成本（按调换部分计）


def load_weekly_nav(path: str, col: str) -> pd.Series:
    df = pd.read_csv(path, parse_dates=["date"])
    s = df.set_index("date")[col].sort_index()
    s = s / s.iloc[0]
    return s.resample("W-FRI").last().dropna()


def combine(r_v3: pd.Series, r_dl: pd.Series, w_dl: float) -> pd.Series:
    """季度再平衡的两资产组合周收益序列"""
    weeks = r_v3.index
    v = 1.0
    a = 1 - w_dl        # v3 份额（组合内净值比例）
    b = w_dl            # 双低份额
    nav = []
    quarter_ends = set()
    for (y, m), g in pd.Series(weeks).groupby([pd.Series(weeks).dt.year, pd.Series(weeks).dt.month]):
        if m in (3, 6, 9, 12):
            quarter_ends.add(g.max())
    for w in weeks:
        rv, rd = r_v3.loc[w], r_dl.loc[w]
        a *= (1 + rv)
        b *= (1 + rd)
        v = a + b
        if w in quarter_ends:   # 季末调回目标权重
            trade = abs(v * (1 - w_dl) - a)
            v -= trade * REBAL_COST
            a = v * (1 - w_dl)
            b = v * w_dl
        nav.append(v)
    return pd.Series(nav, index=weeks)


def metrics(nav: pd.Series, freq: int = 52) -> dict:
    dd = nav / nav.cummax() - 1
    ret = nav.pct_change().dropna()
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    total = nav.iloc[-1] - 1
    ann = (1 + total) ** (1 / years) - 1
    sharpe = ret.mean() / ret.std() * math.sqrt(freq) if ret.std() > 0 else 0
    return {"total": total * 100, "ann": ann * 100, "mdd": dd.min() * 100,
            "sharpe": sharpe, "calmar": ann / abs(dd.min()) if dd.min() else 0}


def main():
    v3 = load_weekly_nav(V3_NAV, "nav")
    dl = load_weekly_nav(DL_NAV, "nav")
    idx = v3.index.intersection(dl.index)
    v3, dl = v3.loc[idx], dl.loc[idx]
    v3, dl = v3 / v3.iloc[0], dl / dl.iloc[0]
    r_v3, r_dl = v3.pct_change().fillna(0), dl.pct_change().fillna(0)
    print(f"组合区间: {idx[0].date()} ~ {idx[-1].date()}, {len(idx)} 周")
    print(f"周收益相关系数: {r_v3.corr(r_dl):.3f}")

    rows = []
    navs = {}
    for w_dl in [0.0, 0.2, 0.3, 0.4, 0.5]:
        nav = combine(r_v3, r_dl, w_dl)
        navs[w_dl] = nav
        m = metrics(nav)
        rows.append({"双低占比": f"{w_dl:.0%}", "总收益%": round(m["total"], 1),
                     "年化%": round(m["ann"], 2), "回撤%": round(m["mdd"], 1),
                     "夏普": round(m["sharpe"], 2), "Calmar": round(m["calmar"], 2)})
        nav.to_frame("nav").reset_index().to_csv(
            os.path.join(OUT, f"nav_combo_dl{int(w_dl*100)}.csv"), index=False, encoding="utf-8-sig")

    print("\n全天候v3 + 可转债双低（季度再平衡）:")
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n年度收益:")
    years = sorted({d.year for d in idx})
    print(f"{'年份':<6}" + "".join(f"{'双低'+f'{w:.0%}':>12}" for w in navs))
    for y in years:
        line = f"{y:<6}"
        for w, nav in navs.items():
            g = nav[nav.index.year == y]
            pre = nav[nav.index < g.index[0]]
            base = pre.iloc[-1] if len(pre) else g.iloc[0] / (1 + r_v3.iloc[0] * 0)
            r = g.iloc[-1] / base - 1
            line += f"{f'{r*100:+.1f}%':>12}"
        print(line)


if __name__ == "__main__":
    main()
