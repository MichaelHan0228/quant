# -*- coding: utf-8 -*-
"""真实股息率(DY) vs PE镜像DY —— 择时信号换源对比回测（2015起）
==================================================================
背景：timing_signal_dy 用 "DY分位 ≈ 1−PE分位" 镜像（假设分红率稳定）。
real_dy.py 用 H20269全收益/H30269价格比值推出真实股息率（日频，2010起）。
本脚本把 DY 信号源替换为真实股息率，对比六版本：
  base         基线（固定权重）
  erp          ERP择时（不变，对照）
  dyM          镜像DY·全史分位（现文档口径）
  dyR          真实DY·全史分位
  orM          或规则：ERP ∨ 镜像DY（现推荐配置）
  orR          或规则：ERP ∨ 真实DY
判定：orR vs orM —— 若真实DY修正了镜像偏差（分红率上升期镜像偏保守），
orR 应在 2021 后的高配年份更准；若差异可忽略，镜像假设成立、维持现状。

输出: output/exp_real_dy.csv、output/exp_real_dy_yearly.csv
"""
import os

import pandas as pd

import backtest
import backtest_timing
from backtest import VARIANTS, metrics, yearly, run_backtest
from backtest_timing import run_backtest_timed
from timing_signal import build_signal, BANDS, ROLL_WINDOW, MIN_OBS, band_adjust
from timing_signal_dy import build_signal_dy
from panel_long import build_panel_long
from real_dy import build_real_dy

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

START = "2015-01-01"
backtest.START_DATE = START
backtest_timing.START_DATE = START

ADJ_MIN = -0.10


def _sub(signal: pd.DataFrame, pct_col: str, adj_col: str) -> pd.DataFrame:
    return pd.DataFrame({"pct": signal[pct_col],
                         "adjust": signal[adj_col].clip(lower=ADJ_MIN)})


def _or_signal(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    idx = a.index.union(b.index)
    pct = pd.concat([a["pct"], b["pct"]], axis=1).reindex(idx).max(axis=1, skipna=True)
    adj = pd.concat([a["adjust"], b["adjust"]], axis=1).reindex(idx).max(axis=1, skipna=True)
    return pd.DataFrame({"pct": pct, "adjust": adj}).dropna()


def build_signal_real_dy() -> pd.DataFrame:
    """真实DY信号：dy, dy_pct_5y, dy_pct_full, adjust_5y, adjust_full（档位同 BANDS）"""
    df = build_real_dy()[["dy"]].dropna()
    df["dy_pct_5y"] = df["dy"].rolling(ROLL_WINDOW, min_periods=MIN_OBS).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)
    df["dy_pct_full"] = df["dy"].expanding(min_periods=250).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)
    df["adjust_5y"] = df["dy_pct_5y"].map(band_adjust)
    df["adjust_full"] = df["dy_pct_full"].map(band_adjust)
    return df


def main():
    print("构建长历史面板 / ERP / 镜像DY / 真实DY 信号 ...")
    panel = build_panel_long()
    panel = panel[panel.index >= pd.Timestamp(START)]
    erp = build_signal(hist_start="2009-01-01")[["pct", "adjust"]]
    erp["adjust"] = erp["adjust"].clip(lower=ADJ_MIN)
    dy_m = build_signal_dy(hist_start="2009-01-01")
    dy_r = build_signal_real_dy()

    mir_full = _sub(dy_m, "dy_pct_full", "adjust_full")
    real_full = _sub(dy_r, "dy_pct_full", "adjust_full")

    signals = {
        "erp": ("ERP择时", erp),
        "dyM": ("镜像DY全史", mir_full),
        "dyR": ("真实DY全史", real_full),
        "orM": ("或规则·镜像", _or_signal(erp, mir_full)),
        "orR": ("或规则·真实", _or_signal(erp, real_full)),
    }

    rows, yrows = [], []
    for label, w in VARIANTS.items():
        cn = "稳健版" if label == "steady" else "进取版"
        eq_b, log_b, fees_b = run_backtest(panel, w, label)
        m = metrics(eq_b)
        rows.append({"版本": cn, "信号": "基线", **{k: m[k] for k in ("total", "ann", "mdd", "sharpe")},
                     "调仓": len(log_b), "费用": fees_b})
        y_base = yearly(eq_b)
        res = {}
        for key, (name, sig) in signals.items():
            eq, log, dec, trades, fees = run_backtest_timed(panel, w, sig, label)
            res[key] = eq
            m = metrics(eq)
            rows.append({"版本": cn, "信号": name, **{k: m[k] for k in ("total", "ann", "mdd", "sharpe")},
                         "调仓": len(log), "费用": fees})
        years = sorted({y for v in res.values() for y in yearly(v)} | set(y_base))
        for y in years:
            row = {"版本": cn, "年份": y, "基线": round(y_base[y], 1) if y in y_base else None}
            for key, (name, _) in signals.items():
                yy = yearly(res[key])
                row[name] = round(yy[y], 1) if y in yy else None
            yrows.append(row)

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT, "exp_real_dy.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(yrows).to_csv(os.path.join(OUT, "exp_real_dy_yearly.csv"),
                               index=False, encoding="utf-8-sig")

    print("\n" + "=" * 100)
    print(f"真实DY vs 镜像DY（{START[:4]}-01 ~ 2026-07，100万，长回测，减仓下限 {ADJ_MIN:+.0%}）")
    print("=" * 100)
    print(f"{'版本':<6}{'信号':<14}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'调仓':>5}{'费用':>8}")
    for r in rows:
        print(f"{r['版本']:<6}{r['信号']:<14}{r['total']:>8.1f}%{r['ann']:>7.2f}%"
              f"{r['mdd']:>8.1f}%{r['sharpe']:>7.2f}{r['调仓']:>5}{r['费用']:>8,.0f}")

    ydf = pd.DataFrame(yrows)
    for cn, g in ydf.groupby("版本", sort=False):
        print(f"\n年度收益（{cn}）:")
        cols = [c for c in g.columns if c not in ("版本", "年份")]
        print("    年份  " + "".join(f"{c:>10}" for c in cols))
        for _, r in g.iterrows():
            line = f"    {int(r['年份'])}  "
            for c in cols:
                line += f"{(f'{r[c]:+.1f}' if pd.notna(r[c]) else '-'):>10}"
            print(line)

    # 当前信号对比
    print("\n当前信号对比（最新交易日）:")
    for name, sig in [("ERP", erp), ("镜像DY全史", mir_full), ("真实DY全史", real_full)]:
        s = sig.dropna().iloc[-1]
        print(f"  {name:<10} {sig.dropna().index[-1].date()}: 分位 {s['pct']:.1%} → hlb {s['adjust']:+.0%}")

    print(f"\n输出已保存: {os.path.join(OUT, 'exp_real_dy.csv')}")


if __name__ == "__main__":
    main()
