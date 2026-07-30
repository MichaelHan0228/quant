"""
可转债双低 —— 参数变体扫描
==============================
网格：评级过滤 {≥AA−, 无} × 价格上限 {130, 140} × 持仓数 {10, 15, 20} = 12 组
滞回带 = 持仓数 + 5。其余规则同 backtest_double_low.py。
"""
import os

import pandas as pd

from backtest_double_low import (load_dataset, build_panel, run_backtest,
                                 metrics_df, fetch_csi_cb_index, DATA, OUT)


def yearly(curve):
    c = curve.copy()
    c["year"] = c["date"].dt.year
    out = {}
    prev = None
    for y, g in c.groupby("year"):
        base = g["nav"].iloc[0] if prev is None else prev
        out[y] = (g["nav"].iloc[-1] / base - 1) * 100
        prev = g["nav"].iloc[-1]
    return out


def main():
    print("构建数据集（一次性）...")
    bonds, stocks = load_dataset()
    panel, weeks = build_panel(bonds, stocks)
    print(f"周频面板: {len(weeks)} 周")

    rows = []
    yearly_map = {}
    for rating_on in [True, False]:
        for max_price in [130.0, 140.0]:
            for top_n in [10, 15, 20]:
                tag = f"评级{'开' if rating_on else '关'} 上限{max_price:.0f} 持仓{top_n}"
                curve, trades, fees = run_backtest(
                    bonds, panel, weeks,
                    {"top_n": top_n, "max_price": max_price, "rating_on": rating_on})
                m = metrics_df(curve)
                rows.append({"变体": tag, "年化%": round(m["ann"], 2),
                             "回撤%": round(m["mdd"], 1), "夏普": round(m["sharpe"], 2),
                             "Calmar": round(m["calmar"], 2), "交易": len(trades)})
                yearly_map[tag] = yearly(curve)
                curve.to_csv(os.path.join(OUT, f"nav_dl_{tag}.csv"), index=False,
                             encoding="utf-8-sig")
                print(f"{tag}: 年化{m['ann']:.2f}% 回撤{m['mdd']:.1f}% 夏普{m['sharpe']:.2f}",
                      flush=True)

    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(OUT, "scan_double_low.csv"), index=False, encoding="utf-8-sig")
    print("\n按 Calmar 排序:")
    print(res.sort_values("Calmar", ascending=False).to_string(index=False))

    print("\n年度收益（关键变体）:")
    years = sorted({y for m in yearly_map.values() for y in m})
    tags = list(yearly_map)
    print(f"{'年份':<6}" + "".join(f"{t:>22}" for t in tags))
    for y in years:
        line = f"{y:<6}"
        for t in tags:
            v = yearly_map[t].get(y)
            line += f"{(f'{v:+.1f}' if v is not None else '-'):>22}"
        print(line)


if __name__ == "__main__":
    main()
