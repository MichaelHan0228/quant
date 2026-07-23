# -*- coding: utf-8 -*-
"""定位进取版·或规则真实的最大回撤区间（峰值→谷底→修复），并对照现配置（或规则镜像）"""
import pandas as pd

from backtest import VARIANTS
from backtest_timing import run_backtest_timed
from timing_signal import build_signal
from panel_long import build_panel_long
from backtest_real_dy import build_signal_real_dy, _sub, _or_signal, ADJ_MIN, START


def dd_window(eq):
    eq = eq.copy()
    eq["date"] = pd.to_datetime(eq["date"])
    nav = eq.set_index("date")["nav"]
    dd = nav / nav.cummax() - 1
    trough = dd.idxmin()
    peak = nav[:trough].idxmax()
    after = nav[trough:]
    rec = after[after >= nav[peak]]
    rec_date = rec.index[0] if len(rec) else None
    return nav, dd, peak, trough, rec_date


def report(name, eq, dec):
    nav, dd, peak, trough, rec_date = dd_window(eq)
    print(f"\n== {name} ==")
    print(f"  峰值 {peak.date()}  NAV={nav[peak]:.4f}")
    print(f"  谷底 {trough.date()}  NAV={nav[trough]:.4f}  回撤 {dd[trough]:.2%}")
    if rec_date is not None:
        print(f"  修复 {rec_date.date()}（谷底后 {(rec_date - trough).days} 天，全程 {(rec_date - peak).days} 天）")
    else:
        print(f"  至今未修复（谷底已过 {(nav.index[-1] - trough).days} 天）")
    print(f"  窗口内择时决策（前后各延一个季度）:")
    d = dec.copy()
    d["date"] = pd.to_datetime(d["date"])
    lo, hi = peak - pd.Timedelta(days=100), trough + pd.Timedelta(days=100)
    sub = d[(d["date"] >= lo) & (d["date"] <= hi)]
    if sub.empty:
        print("    （无决策）")
    for _, r in sub.iterrows():
        print(f"    {r['date'].date()}  分位={r['erp_pct']:.1%}  档={r['band_adj']:+.0%}  "
              f"hlb→{r['hlb_target']:.0%}  bond10→{r['bond10_target']:.0%}")
    return nav, dd


panel = build_panel_long()
panel = panel[panel.index >= pd.Timestamp(START)]
erp = build_signal(hist_start="2009-01-01")[["pct", "adjust"]]
erp["adjust"] = erp["adjust"].clip(lower=ADJ_MIN)
dy_r = build_signal_real_dy()
from timing_signal_dy import build_signal_dy
dy_m = build_signal_dy(hist_start="2009-01-01")

real_full = _sub(dy_r, "dy_pct_full", "adjust_full")
mir_full = _sub(dy_m, "dy_pct_full", "adjust_full")
orr = _or_signal(erp, real_full)
orm = _or_signal(erp, mir_full)

w = VARIANTS["aggressive"]
eq_r, log_r, dec_r, _, _ = run_backtest_timed(panel, w, orr, "aggressive")
eq_m, log_m, dec_m, _, _ = run_backtest_timed(panel, w, orm, "aggressive")

nav_r, dd_r = report("进取版·或规则·真实DY", eq_r, dec_r)
nav_m, dd_m = report("进取版·或规则·镜像（现配置，对照）", eq_m, dec_m)

# 真实版谷底窗口 vs 镜像版同期净值
_, _, peak, trough, _ = dd_window(eq_r)
span = (nav_r.index >= peak) & (nav_r.index <= trough)
print(f"\n窗口 {peak.date()} ~ {trough.date()} 内逐季净值对比:")
q = nav_r[span].resample("QE").last().index
for dt in q:
    print(f"  {dt.date()}  真实={nav_r.asof(dt):.4f}  镜像={nav_m.asof(dt):.4f}")
