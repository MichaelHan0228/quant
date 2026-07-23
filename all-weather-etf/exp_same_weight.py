"""
同仓位对照实验：择时增益来自"择时能力"还是"平均仓位更高"？
==============================================================
质疑："或规则"取 max(ERP档, DY档)，合并档恒 ≥ 任一单信号档，
长期平均 hlb 仓位必然高于基线。在股票长牛的样本里，择时版的超额
收益可能只是"平均仓位更高"的仓位效应，而非择时 alpha。

方法：
  1. 跑 ERP / 或规则全史 择时版（2015 起，与 backtest_dy.py 同引擎同口径），
     记录每日 hlb 目标权重路径（期初基准 + 决策日前向填充）。
  2. 取路径时间平均 avg_hlb，构造固定权重基线：
       hlb = avg_hlb，bond10 反向（其余腿与基准相同），
     用同一引擎、同一 ±5pp 阈值纪律回测（run_backtest）。
  3. 判定：择时版明显跑赢同仓位固定基线 → 增益来自择时能力；
           持平或跑输 → 增益是仓位效应，"或规则"价值需重估。

口径说明：
  - avg_hlb 用"目标权重"路径的时间平均；实际持仓权重在两次调仓间
    随价格漂移，择时版与固定版漂移机制相同，对对比结果影响可忽略。
  - 调仓下限 ADJ_MIN = -10pp，与 backtest_dy.py 一致（2026-07-23 口径）。

输出: output/exp_same_weight.csv、output/exp_same_weight_yearly.csv
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

ADJ_MIN = -0.10
STOCK_LEG = "hlb"
HEDGE_LEG = "bond10"


def _sub(signal: pd.DataFrame, pct_col: str, adj_col: str) -> pd.DataFrame:
    return pd.DataFrame({"pct": signal[pct_col],
                         "adjust": signal[adj_col].clip(lower=ADJ_MIN)})


def _or_signal(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """'或'规则：分位取较大、档位取较乐观（与 backtest_dy.py 相同）"""
    idx = a.index.union(b.index)
    pct = pd.concat([a["pct"], b["pct"]], axis=1).reindex(idx).max(axis=1, skipna=True)
    adj = pd.concat([a["adjust"], b["adjust"]], axis=1).reindex(idx).max(axis=1, skipna=True)
    return pd.DataFrame({"pct": pct, "adjust": adj}).dropna()


def hlb_target_path(dates: pd.DatetimeIndex, base_hlb: float,
                    dec: pd.DataFrame) -> pd.Series:
    """由择时决策记录重建每日 hlb 目标权重路径"""
    path = pd.Series(base_hlb, index=dates)
    if dec is not None and not dec.empty:
        d = dec.copy()
        d["date"] = pd.to_datetime(d["date"])
        tgt = d.set_index("date")["hlb_target"]
        tgt = tgt.reindex(dates).ffill()
        path = tgt.fillna(pd.Series(base_hlb, index=dates))
    return path


def main():
    print("构建长历史面板与 ERP/DY 信号 ...")
    panel = build_panel_long()
    panel = panel[panel.index >= pd.Timestamp(START)]
    dates = panel.index
    erp = build_signal(hist_start="2009-01-01")[["pct", "adjust"]]
    erp["adjust"] = erp["adjust"].clip(lower=ADJ_MIN)
    dy = build_signal_dy(hist_start="2009-01-01")
    dyfull = _sub(dy, "dy_pct_full", "adjust_full")
    orfull = _or_signal(erp, dyfull)

    signals = {"erp": ("ERP择时", erp), "orfull": ("或规则全史", orfull)}
    rows, yrows = [], []

    for label, w in VARIANTS.items():
        cn = "稳健版" if label == "steady" else "进取版"
        base_hlb, base_bond = w[STOCK_LEG], w[HEDGE_LEG]

        # 基准（固定权重，文档口径）
        eq_b, log_b, fees_b = run_backtest(panel, w, label)
        m = metrics(eq_b)
        rows.append({"版本": cn, "信号": "基线(固定)", "avg_hlb": base_hlb,
                     **{k: m[k] for k in ("total", "ann", "mdd", "sharpe", "calmar")},
                     "调仓": len(log_b), "费用": fees_b})
        y_base = yearly(eq_b)

        for key, (name, sig) in signals.items():
            eq_t, log_t, dec, trades_t, fees_t = run_backtest_timed(panel, w, sig, label)
            m_t = metrics(eq_t)

            # 择时版的平均目标仓位
            path = hlb_target_path(dates, base_hlb, dec)
            avg_hlb = round(float(path.mean()), 4)

            # 同仓位固定基线：hlb=avg_hlb，bond10 反向，其余腿不变
            fixed_w = dict(w)
            fixed_w[STOCK_LEG] = avg_hlb
            fixed_w[HEDGE_LEG] = round(base_bond - (avg_hlb - base_hlb), 4)
            eq_f, log_f, fees_f = run_backtest(panel, fixed_w, label)
            m_f = metrics(eq_f)

            rows.append({"版本": cn, "信号": f"{name}·择时", "avg_hlb": avg_hlb,
                         **{k: m_t[k] for k in ("total", "ann", "mdd", "sharpe", "calmar")},
                         "调仓": len(log_t), "费用": fees_t})
            rows.append({"版本": cn, "信号": f"{name}·同仓位固定", "avg_hlb": avg_hlb,
                         **{k: m_f[k] for k in ("total", "ann", "mdd", "sharpe", "calmar")},
                         "调仓": len(log_f), "费用": fees_f})

            y_t, y_f = yearly(eq_t), yearly(eq_f)
            for y in sorted(set(y_t) | set(y_f)):
                yrows.append({"版本": cn, "信号": name, "年份": y,
                              "择时": round(y_t[y], 1) if y in y_t else None,
                              "同仓位固定": round(y_f[y], 1) if y in y_f else None,
                              "基线": round(y_base[y], 1) if y in y_base else None})

    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(OUT, "exp_same_weight.csv"), index=False, encoding="utf-8-sig")
    pd.DataFrame(yrows).to_csv(os.path.join(OUT, "exp_same_weight_yearly.csv"),
                               index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 200)
    print("\n" + "=" * 110)
    print(f"同仓位对照实验（{START[:4]}-01 ~ 2026-07，100万，长回测，减仓下限 {ADJ_MIN:+.0%}）")
    print("=" * 110)
    print(f"{'版本':<6}{'信号':<18}{'avg_hlb':>8}{'总收益':>9}{'年化':>8}{'最大回撤':>9}"
          f"{'夏普':>7}{'Calmar':>8}{'调仓':>5}{'费用':>8}")
    for r in rows:
        print(f"{r['版本']:<6}{r['信号']:<18}{r['avg_hlb']:>8.1%}{r['total']:>8.1f}%"
              f"{r['ann']:>7.2f}%{r['mdd']:>8.1f}%{r['sharpe']:>7.2f}{r['calmar']:>8.2f}"
              f"{r['调仓']:>5}{r['费用']:>8,.0f}")

    print("\n年度收益（择时 vs 同仓位固定 vs 基线）:")
    ydf = pd.DataFrame(yrows)
    for (cn, name), g in ydf.groupby(["版本", "信号"], sort=False):
        print(f"\n  {cn}·{name}:")
        for _, r in g.iterrows():
            print(f"    {int(r['年份'])}: 择时 {r['择时']:+.1f}% | 同仓位 {r['同仓位固定']:+.1f}% "
                  f"| 基线 {r['基线']:+.1f}%")

    print(f"\n输出已保存: {os.path.join(OUT, 'exp_same_weight.csv')}")


if __name__ == "__main__":
    main()
