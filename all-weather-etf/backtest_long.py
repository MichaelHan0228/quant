"""
全天候 2015 起长历史回测：基线 / ERP择时 / ERP择时+换仓
=========================================================
与 backtest_swap.py 相同的三方对比，但起点从 2020-01 提前到 2015-01：
  - 价格面板：panel_long.build_panel_long()（2015 起，含拼接重建段）
  - ERP 信号：timing_signal.build_signal(hist_start="2009-01-01")，
    2015 年初即有完整 5 年滚动窗口
  - 引擎与费用模型与 v2/timing 完全一致（monkeypatch START_DATE）

输出: output/nav_*_long.csv、yearly_compare_long.csv
"""
import os

import pandas as pd

import backtest
import backtest_timing
import backtest_swap
from backtest import VARIANTS, metrics, yearly, run_backtest
from backtest_timing import run_backtest_timed
from backtest_swap import run_backtest_swap
from timing_signal import build_signal
from panel_long import build_panel_long

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

START = "2015-01-01"
# 三个引擎模块都 from backtest import START_DATE，需逐一改写其模块全局
backtest.START_DATE = START
backtest_timing.START_DATE = START
backtest_swap.START_DATE = START


def main():
    print("构建长历史价格面板与ERP信号(2009起) ...")
    panel = build_panel_long()
    panel = panel[panel.index >= pd.Timestamp(START)]
    signal = build_signal(hist_start="2009-01-01")
    signal = signal[signal.index >= pd.Timestamp("2009-01-01")]
    print(f"  行情 {panel.index[0].date()} ~ {panel.index[-1].date()}, "
          f"信号 {signal.index[0].date()} ~ {signal.index[-1].date()}")

    results = {}
    for label, w in VARIANTS.items():
        eq_b, log_b, fees_b = run_backtest(panel, w, label)
        eq_t, log_t, dec_t, trades_t, fees_t = run_backtest_timed(panel, w, signal, label)
        eq_s, log_s, dec_s, swap_s, trades_s, fees_s = run_backtest_swap(panel, w, signal, label)
        results[label] = {
            "base":  (eq_b, log_b, fees_b),
            "timed": (eq_t, log_t, dec_t, trades_t, fees_t),
            "swap":  (eq_s, log_s, dec_s, swap_s, trades_s, fees_s),
        }
        for key, eq in [("base", eq_b), ("timed", eq_t), ("swap", eq_s)]:
            eq.to_csv(os.path.join(OUT, f"nav_{label}_{key}_long.csv"),
                      index=False, encoding="utf-8-sig")
        log_t.to_csv(os.path.join(OUT, f"rebalance_{label}_timed_long.csv"),
                     index=False, encoding="utf-8-sig")
        if not swap_s.empty:
            swap_s.to_csv(os.path.join(OUT, f"swap_decisions_{label}_long.csv"),
                          index=False, encoding="utf-8-sig")

    # ── 总表 ────────────────────────────────────────────────
    print("\n" + "=" * 88)
    print(f"基线 / ERP择时 / ERP择时+换仓（{START[:4]}-01 起，100万）")
    print("=" * 88)
    print(f"{'版本':<16}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'调仓':>5}{'费用':>7}")
    for label, res in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        for tag, key in [("基线", "base"), ("择时", "timed"), ("择时+换仓", "swap")]:
            eq, log, fees = res[key][0], res[key][1], res[key][-1]
            m = metrics(eq)
            print(f"{cn+'·'+tag:<16}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%"
                  f"{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{len(log):>5}{fees:>7,.0f}")

    # ── 年度收益对比（重点）───────────────────────────────────
    print("\n年度收益对比:")
    years = sorted({y for res in results.values() for v in res.values() for y in yearly(v[0])})
    print(f"{'年份':<6}" + "".join(f"{h:>11}" for h in
          ["稳健基线", "稳健择时", "稳健换仓", "进取基线", "进取择时", "进取换仓"]))
    yrows = []
    for y in years:
        row = {"年份": y}
        line = f"{y:<6}"
        for label, res in results.items():
            cn = "稳健" if label == "steady" else "进取"
            for tag, key in [("基线", "base"), ("择时", "timed"), ("换仓", "swap")]:
                r = yearly(res[key][0]).get(y)
                row[f"{cn}{tag}"] = round(r, 1) if r is not None else None
                line += f"{f'{r:+.1f}%' if r is not None else '-':>11}"
        yrows.append(row)
        print(line)
    pd.DataFrame(yrows).to_csv(os.path.join(OUT, "yearly_compare_long.csv"),
                               index=False, encoding="utf-8-sig")

    # ── 择时决策与换仓记录 ───────────────────────────────────
    for label, res in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        dec_t = res["timed"][2]
        if not dec_t.empty:
            print(f"\n择时决策记录（{cn}）:")
            print(dec_t.to_string(index=False))
        swap_s = res["swap"][3]
        if not swap_s.empty:
            print(f"\n换仓记录（{cn}）:")
            print(swap_s.to_string(index=False))

    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
