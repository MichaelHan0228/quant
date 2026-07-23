"""
DY股息率择时 + ERP/DY "或"规则合并 —— 2015 起长回测
====================================================
对比六个版本（稳健/进取各一套）：
  base   基线（固定权重）
  erp    ERP择时（股债性价比，跨资产信号）
  dy5    DY择时·5年滚动分位（单资产信号，PE镜像）
  dyfull DY择时·全历史分位（2009起扩展窗口）
  or5    "或"规则·5年分位：max(ERP档, DY档)——任一信号说便宜就调高
  orfull "或"规则·全历史分位

"或"规则语义：合并档 = max(erp_adjust, dy_adjust)。
  任一信号说便宜 → 按较乐观档调高；两个都说贵 → 才按较轻档调低。

仓位边界：减仓下限 ADJ_MIN = −10pp（稳健版 hlb 最低 10%、进取版最低 20%）。
  曾测试 −5pp 下限（最低 15%/25%）：2015 年保护失效（进取 +9.5%→+6.3%）、
  进取版最大回撤 −10.3%→−13.1%、年化全面下降，已回退并保留 −10pp（2026-07-23）。

输出: output/yearly_compare_dy.csv、timing_decisions_or_*.csv
"""
import os

import pandas as pd

import backtest
import backtest_timing
from backtest import VARIANTS, metrics, yearly, run_backtest
from backtest_timing import run_backtest_timed
from timing_signal import build_signal
from timing_signal_dy import build_signal_dy
from panel_long import build_panel_long

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

START = "2015-01-01"
backtest.START_DATE = START
backtest_timing.START_DATE = START

ADJ_MIN = -0.10   # 减仓下限：-10pp（稳健版 hlb 最低 10%、进取版最低 20%）；加仓上限 +10pp
                  # 注：曾测试 -5pp 下限（最低15%/25%），2015 类极端年保护失效、
                  # 进取版回撤恶化 -10.3%→-13.1%，年化全面下降，已回退（2026-07-23）


def _sub(signal: pd.DataFrame, pct_col: str, adj_col: str) -> pd.DataFrame:
    return pd.DataFrame({"pct": signal[pct_col],
                         "adjust": signal[adj_col].clip(lower=ADJ_MIN)})


def _or_signal(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """'或'规则：分位取较大、档位取较乐观"""
    idx = a.index.union(b.index)
    pct = pd.concat([a["pct"], b["pct"]], axis=1).reindex(idx).max(axis=1, skipna=True)
    adj = pd.concat([a["adjust"], b["adjust"]], axis=1).reindex(idx).max(axis=1, skipna=True)
    return pd.DataFrame({"pct": pct, "adjust": adj}).dropna()


def main():
    print("构建长历史面板与 ERP/DY 信号 ...")
    panel = build_panel_long()
    panel = panel[panel.index >= pd.Timestamp(START)]
    erp = build_signal(hist_start="2009-01-01")[["pct", "adjust"]]
    erp["adjust"] = erp["adjust"].clip(lower=ADJ_MIN)
    dy = build_signal_dy(hist_start="2009-01-01")

    signals = {
        "erp":    erp,
        "dy5":    _sub(dy, "dy_pct_5y", "adjust_5y"),
        "dyfull": _sub(dy, "dy_pct_full", "adjust_full"),
        "or5":    _or_signal(erp, _sub(dy, "dy_pct_5y", "adjust_5y")),
        "orfull": _or_signal(erp, _sub(dy, "dy_pct_full", "adjust_full")),
    }
    names = {"base": "基线", "erp": "ERP择时", "dy5": "DY择时5年",
             "dyfull": "DY择时全史", "or5": "或规则5年", "orfull": "或规则全史"}

    results = {}   # results[label][variant] = (eq, log, dec, fees)
    for label, w in VARIANTS.items():
        results[label] = {}
        eq_b, log_b, fees_b = run_backtest(panel, w, label)
        results[label]["base"] = (eq_b, log_b, None, fees_b)
        for key, sig in signals.items():
            eq, log, dec, trades, fees = run_backtest_timed(panel, w, sig, label)
            results[label][key] = (eq, log, dec, fees)
            eq.to_csv(os.path.join(OUT, f"nav_{label}_{key}_dy.csv"),
                      index=False, encoding="utf-8-sig")
        if key:
            pass

    # ── 总表 ────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print(f"基线 / ERP / DY / 或规则（{START[:4]}-01 ~ 2026-07，100万，长回测，减仓下限 {ADJ_MIN:+.0%}）")
    print("=" * 92)
    print(f"{'版本':<16}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'调仓':>5}{'费用':>7}")
    for label, res in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        for key in ["base", "erp", "dy5", "dyfull", "or5", "orfull"]:
            eq, log, dec, fees = res[key]
            m = metrics(eq)
            print(f"{cn+'·'+names[key]:<16}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%"
                  f"{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{len(log):>5}{fees:>7,.0f}")

    # ── 年度收益对比（重点）───────────────────────────────────
    variants = ["base", "erp", "dy5", "dyfull", "or5", "orfull"]
    yrows = []
    for label, res in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        print(f"\n年度收益（{cn}）:")
        years = sorted({y for v in res.values() for y in yearly(v[0])})
        print(f"{'年份':<6}" + "".join(f"{names[k]:>11}" for k in variants))
        for y in years:
            row = {"版本": cn, "年份": y}
            line = f"{y:<6}"
            for k in variants:
                r = yearly(res[k][0]).get(y)
                row[names[k]] = round(r, 1) if r is not None else None
                line += f"{f'{r:+.1f}%' if r is not None else '-':>11}"
            yrows.append(row)
            print(line)
    pd.DataFrame(yrows).to_csv(os.path.join(OUT, "yearly_compare_dy.csv"),
                               index=False, encoding="utf-8-sig")

    # ── 或规则决策记录 ───────────────────────────────────────
    for label, res in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        for key in ["or5", "orfull"]:
            dec = res[key][2]
            if dec is not None and not dec.empty:
                dec.to_csv(os.path.join(OUT, f"timing_decisions_{key}_{label}.csv"),
                           index=False, encoding="utf-8-sig")
                print(f"\n择时决策记录（{cn}·{names[key]}）:")
                print(dec.to_string(index=False))

    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
