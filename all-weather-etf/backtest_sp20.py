# -*- coding: utf-8 -*-
"""底仓调整回测：红利低波降5pp固定加到标普500（sp500 15%→20%）
==================================================================
新底仓（进取版·sp20）：红利低波25 / 标普500 20 / 十年国债25 / 黄金20 / 豆粕5 / 货币5
  择时层档位同步平移：红利低波浮动范围 20~40% → 15~35%，国债仍反向。
对比：现底仓（红利30/标普15）的 基线 / 真实DY择时 / v3正式版（ERP锚）
输出: output/sp20_*.csv
"""
import os

import pandas as pd

import backtest
import backtest_timing
import backtest_combo as bc
from backtest import VARIANTS, metrics, yearly, run_backtest
from backtest_timing import run_backtest_timed

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")

W_SP20 = {"hlb": 0.25, "sp500": 0.20, "bond10": 0.25,
          "gold": 0.20, "soybean": 0.05, "cash": 0.05}
ERP_CFG = bc.VARIANTS_A500["combo20_erp"]


def run_set(panel, dy_sig, signals, w, start, tag_w):
    """单底仓三版本：基线 / 真实DY择时 / v3正式版(ERP锚)"""
    backtest.START_DATE = start
    backtest_timing.START_DATE = start
    res = {}
    eq, log, fees = run_backtest(panel, w, "x")
    res[f"{tag_w}·基线"] = (eq, log, fees)
    eq, log, dec, tr, fees = run_backtest_timed(panel, w, dy_sig, "x")
    res[f"{tag_w}·真实DY择时"] = (eq, log, fees)
    eq, log, alog, dlog, trades, fees, _splog = bc.run_backtest_combo(
        panel, w, dy_sig, signals, ERP_CFG, start, f"sp20_{tag_w}")
    res[f"{tag_w}·v3正式版"] = (eq, log, fees)
    return res


def run_sample(panel, dy_sig, signals, start, tag):
    w_cur = VARIANTS["aggressive"]
    results = {}
    results.update(run_set(panel, dy_sig, signals, w_cur, start, "现底仓"))
    results.update(run_set(panel, dy_sig, signals, W_SP20, start, "sp20新底仓"))

    print("\n" + "=" * 100)
    print(f"底仓对比（进取版，{start[:4]}-01 ~ {panel.index[-1].date()}，100万，{tag}）")
    print("现底仓=红利30/标普500 15 ；sp20新底仓=红利25/标普500 20")
    print("=" * 100)
    print(f"{'版本':<22}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'调仓':>5}{'费用':>7}")
    for name, (eq, log, fees) in results.items():
        m = metrics(eq)
        print(f"{name:<22}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%"
              f"{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{len(log):>5}{fees:>7,.0f}")
        if "sp20" in name:
            safe = name.replace("·", "_")
            eq.to_csv(os.path.join(OUT, f"sp20_nav_{safe}_{tag}.csv"),
                      index=False, encoding="utf-8-sig")

    years = sorted({y for eq, _, _ in results.values() for y in yearly(eq)})
    ymap = {name: yearly(eq) for name, (eq, _, _) in results.items()}
    yrows = []
    print(f"\n年度收益（{tag}）:")
    print(f"{'年份':<6}" + "".join(f"{c:>20}" for c in results))
    for y in years:
        row = {"年份": y}
        line = f"{y:<6}"
        for name in results:
            v = ymap[name].get(y)
            row[name] = round(v, 1) if v is not None else None
            line += f"{(f'{v:+.1f}%' if v is not None else '-'):>20}"
        yrows.append(row)
        print(line)
    pd.DataFrame(yrows).to_csv(os.path.join(OUT, f"sp20_yearly_{tag}.csv"),
                               index=False, encoding="utf-8-sig")


def main():
    print("构建信号与面板 ...")
    dy_sig = bc.build_dy_signal()
    signals = {"erp_a500": bc.build_a500_erp_series()}

    panel_s = bc.load_panel()
    panel_s[bc.A500_LEG] = bc.load_a500_series(panel_s.index)
    panel_s = panel_s[panel_s.index >= pd.Timestamp("2020-01-01")]

    panel_l = bc.build_panel_long()
    panel_l[bc.A500_LEG] = bc.load_a500_series(panel_l.index, hs300=panel_l["hs300"])
    panel_l = panel_l[panel_l.index >= pd.Timestamp("2015-01-01")]

    run_sample(panel_s, dy_sig, signals, "2020-01-01", "短样本")
    run_sample(panel_l, dy_sig, signals, "2015-01-01", "长样本")
    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
