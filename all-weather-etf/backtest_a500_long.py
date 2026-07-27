"""
全天候 + A500低估加仓层：2015 起长历史回测
==============================================
与 backtest_a500.py 同一套规则（PE分位滞回 ±20/30%、15%仓位、债10pp+货5pp出资、
510300→563360 衔接），但起点从 2020-01 提前到 2015-01：
  - 价格面板：panel_long.build_panel_long()（2015 起，bond10/soybean 含拼接重建段，
    口径声明见 timing-plan.md 附录B）
  - A500腿：长面板自带的 hs300(510300) 直接覆盖 2015 起，无需重建，
    2024-10-15 起按收盘价拼接 563360
  - 信号：backtest_a500.load_a500_signal()——沪深300 PE 五年滚动分位
    （csi300_pe.csv 自 2011-06 起，2014 年中即出信号，覆盖 2015 起点）；
    2024-09 起 A500 真实 PE 扩展窗口分位接管
  - 引擎与费用模型与 v2 完全一致（monkeypatch START_DATE，同 backtest_long.py）

输出: output/nav_*_a500_long.csv、rebalance_*_a500_long.csv、a500_decisions_*_long.csv
"""
import os

import pandas as pd

import backtest
import backtest_a500
from backtest import VARIANTS, metrics, yearly, run_backtest
from backtest_a500 import (A500_LEG, load_a500_series, load_a500_signal,
                           run_backtest_a500)
from panel_long import build_panel_long

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

START = "2015-01-01"
backtest.START_DATE = START
backtest_a500.START_DATE = START


def main():
    print("构建长历史价格面板与A500信号 ...")
    panel = build_panel_long()
    panel[A500_LEG] = load_a500_series(panel.index, hs300=panel["hs300"])
    panel = panel[panel.index >= pd.Timestamp(START)]
    signal = load_a500_signal()
    print(f"  行情 {panel.index[0].date()} ~ {panel.index[-1].date()}, "
          f"信号 {signal.index[0].date()} ~ {signal.index[-1].date()}")
    s = signal.iloc[-1]
    print(f"  最新PE分位({signal.index[-1].date()}): {s:.1%}")

    results = {}
    for label, w in VARIANTS.items():
        eq_b, log_b, fees_b = run_backtest(panel, w, label)                       # 基线
        eq_a, log_a, dec_a, trades_a, fees_a = run_backtest_a500(panel, w, signal, label)
        results[label] = (eq_b, log_b, fees_b, eq_a, log_a, dec_a, trades_a, fees_a)
        eq_a.to_csv(os.path.join(OUT, f"nav_{label}_a500_long.csv"), index=False,
                    encoding="utf-8-sig")
        log_a.to_csv(os.path.join(OUT, f"rebalance_{label}_a500_long.csv"), index=False,
                     encoding="utf-8-sig")
        trades_a.to_csv(os.path.join(OUT, f"trades_{label}_a500_long.csv"), index=False,
                        encoding="utf-8-sig")
        if not dec_a.empty:
            dec_a.to_csv(os.path.join(OUT, f"a500_decisions_{label}_long.csv"),
                         index=False, encoding="utf-8-sig")

    print("\n" + "=" * 88)
    print(f"基线 vs +A500低估加仓层（{START[:4]}-01 ~ 2026-07，100万，阈值±5pp）")
    print("=" * 88)
    hdr = f"{'版本':<18}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'调仓':>5}{'费用':>7}"
    print(hdr)
    for label, (eq_b, log_b, fees_b, eq_a, log_a, dec_a, trades_a, fees_a) in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        for tag, eq, log, fees in [("基线", eq_b, log_b, fees_b), ("+A500层", eq_a, log_a, fees_a)]:
            m = metrics(eq)
            print(f"{cn+'·'+tag:<18}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%"
                  f"{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{len(log):>5}{fees:>7,.0f}")

    print("\n年度收益（基线 → +A500层）:")
    for label, (eq_b, log_b, fees_b, eq_a, log_a, dec_a, trades_a, fees_a) in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        yb, ya = yearly(eq_b), yearly(eq_a)
        print(f"  {cn}: " + "  ".join(
            f"{y}: {yb.get(y, 0):+.1f}→{ya.get(y, 0):+.1f}" for y in sorted(set(yb) | set(ya))))

    for label, (eq_b, log_b, fees_b, eq_a, log_a, dec_a, trades_a, fees_a) in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        if not dec_a.empty:
            print(f"\nA500决策记录（{cn}）:")
            print(dec_a.to_string(index=False))

    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
