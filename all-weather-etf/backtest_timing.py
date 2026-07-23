"""
全天候 + 股债性价比(ERP)择时回测
==================================
在 backtest.py 的阈值再平衡引擎上加一层估值择时：
  - 季度检查日，先按 ERP 分位档位更新目标权重（信号见 timing_signal.py）
  - 只动股票腿 hlb 与债券腿 bond10：hlb 加/减，bond10 反向减/加，其余腿不动
  - 单次调整幅度限制 ±5pp（防分位在档位边界横跳时来回交易）
  - 之后沿用原纪律：任何资产偏离目标 ≥±5pp 才调仓，不触发不动

对照：steady / aggressive 基线（固定权重，来自 backtest.py）
输出：output/nav_*_timed.csv、rebalance_*_timed.csv、timing_decisions.csv
"""
import os

import pandas as pd

from backtest import (LEGS, VARIANTS, INITIAL_CAPITAL, START_DATE, REBAL_BAND,
                      CASH_LEG, load_panel, buy_shares, sell_shares,
                      metrics, yearly, run_backtest)
from timing_signal import build_signal

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

MAX_STEP = 0.05   # 单次目标权重调整上限
STOCK_LEG = "hlb"
HEDGE_LEG = "bond10"


def run_backtest_timed(panel: pd.DataFrame, base_weights: dict,
                       signal: pd.DataFrame, label: str):
    """目标权重随 ERP 档位漂移的再平衡回测。返回(净值, 再平衡日志, 择时决策, 交易明细, 总费用)"""
    dates = panel.index[panel.index >= pd.Timestamp(START_DATE)]
    weights = dict(base_weights)
    holdings = {leg: 0 for leg in weights}
    cash = INITIAL_CAPITAL
    total_fees = 0.0
    rebal_log = []
    decision_log = []
    trade_log = []
    cur_hlb = base_weights[STOCK_LEG]      # 当前股票腿目标（带步进限制）

    def assets_on(date):
        return cash + sum(holdings[leg] * panel.loc[date, leg] for leg in holdings)

    def rebalance(date, reason):
        nonlocal cash, total_fees
        total = assets_on(date)
        prices = panel.loc[date]
        fees = 0.0

        def _log(leg, side, shares, px):
            trade_log.append({"date": date, "leg": leg, "name": LEGS[leg]["name"],
                              "side": side, "shares": shares, "price": round(px, 4),
                              "amount": round(shares * px, 2)})

        for leg, w in weights.items():
            if leg == CASH_LEG:
                continue
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * w
            if cur_val > tgt_val:
                sh = int((cur_val - tgt_val) / prices[leg] / 100) * 100
                if 0 < sh <= holdings[leg]:
                    proceeds, fee = sell_shares(leg, prices[leg], sh)
                    holdings[leg] -= sh
                    cash += proceeds
                    fees += fee
                    _log(leg, "卖", sh, prices[leg])
        if CASH_LEG in weights and holdings[CASH_LEG] > 0:
            tgt_cash = total * weights[CASH_LEG]
            etf_val = holdings[CASH_LEG] * prices[CASH_LEG]
            if etf_val > tgt_cash:
                sh = min(holdings[CASH_LEG],
                         int((etf_val - tgt_cash) / prices[CASH_LEG] / 100) * 100)
                if sh > 0:
                    proceeds, _ = sell_shares(CASH_LEG, prices[CASH_LEG], sh)
                    holdings[CASH_LEG] -= sh
                    cash += proceeds
                    _log(CASH_LEG, "卖", sh, prices[CASH_LEG])
        for leg, w in weights.items():
            if leg == CASH_LEG:
                continue
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * w
            if tgt_val > cur_val:
                sh, spent = buy_shares(leg, prices[leg], tgt_val - cur_val, cash)
                if sh:
                    holdings[leg] += sh
                    cash -= spent
                    fees += spent - sh * (prices[leg] + LEGS[leg]["spread"])
                    _log(leg, "买", sh, prices[leg])
        if CASH_LEG in weights:
            tgt_cash = total * weights[CASH_LEG]
            etf_val = holdings[CASH_LEG] * prices[CASH_LEG]
            if etf_val < tgt_cash and cash > 0:
                sh, spent = buy_shares(CASH_LEG, prices[CASH_LEG], tgt_cash - etf_val, cash)
                if sh:
                    holdings[CASH_LEG] += sh
                    cash -= spent
                    _log(CASH_LEG, "买", sh, prices[CASH_LEG])
        total_fees += fees
        rebal_log.append({"date": date, "reason": reason, "fees": round(fees, 2)})

    rebalance(dates[0], "期初建仓")

    # 季度检查日：每年3/6/9/12月最后一个交易日
    check_dates = []
    for y in range(dates[0].year, dates[-1].year + 1):
        for m in (3, 6, 9, 12):
            md = dates[(dates.year == y) & (dates.month == m)]
            if len(md):
                check_dates.append(md[-1])

    rows = []
    for date in dates:
        if date in check_dates and date != dates[0]:
            # 1) ERP 择时：更新股票腿目标（步进限制 ±5pp），债券腿反向
            sig = signal[signal.index <= date]
            if not sig.empty:
                pct = sig["pct"].iloc[-1]
                adj = sig["adjust"].iloc[-1]
                desired = base_weights[STOCK_LEG] + adj
                new_hlb = cur_hlb + max(-MAX_STEP, min(MAX_STEP, desired - cur_hlb))
                new_hlb = round(new_hlb, 4)
                if new_hlb != cur_hlb:
                    delta = new_hlb - cur_hlb
                    weights[STOCK_LEG] = new_hlb
                    weights[HEDGE_LEG] = round(base_weights[HEDGE_LEG] - (new_hlb - base_weights[STOCK_LEG]), 4)
                    decision_log.append({"date": date, "erp_pct": round(pct, 4),
                                         "band_adj": adj, "hlb_target": new_hlb,
                                         "bond10_target": weights[HEDGE_LEG]})
                    cur_hlb = new_hlb
            # 2) 阈值纪律：偏离 ≥5pp 才调仓
            total = assets_on(date)
            prices = panel.loc[date]

            def _leg_val(leg):
                if leg == CASH_LEG:
                    return cash + holdings[leg] * prices[leg]
                return holdings[leg] * prices[leg]

            dev = max(abs(_leg_val(leg) / total - w) for leg, w in weights.items())
            if dev >= REBAL_BAND:
                rebalance(date, f"偏离{dev*100:.1f}pp(ERP分位{pct:.0%})" if not sig.empty else f"偏离{dev*100:.1f}pp")
        rows.append({"date": date, "assets": assets_on(date)})
    eq = pd.DataFrame(rows)
    eq["nav"] = eq["assets"] / INITIAL_CAPITAL
    return eq, pd.DataFrame(rebal_log), pd.DataFrame(decision_log), pd.DataFrame(trade_log), total_fees


def main():
    print("加载价格面板与ERP信号 ...")
    panel = load_panel()
    panel = panel[panel.index >= pd.Timestamp(START_DATE)]
    signal = build_signal()
    print(f"  行情 {panel.index[0].date()} ~ {panel.index[-1].date()}, "
          f"信号 {signal.index[0].date()} ~ {signal.index[-1].date()}")

    # 最新信号
    s = signal.iloc[-1]
    print(f"\n最新ERP信号({signal.index[-1].date()}): 分位={s['pct']:.0%} "
          f"PE={s['pe']:.2f} 10Y={s['y10']:.3f}% ERP={s['erp']:.3f}% → hlb {s['adjust']:+.0%}")

    results = {}
    for label, w in VARIANTS.items():
        eq_b, log_b, fees_b = run_backtest(panel, w, label)                            # 基线
        eq_t, log_t, dec_t, trades_t, fees_t = run_backtest_timed(panel, w, signal, label)  # 择时
        results[label] = (eq_b, log_b, fees_b, eq_t, log_t, dec_t, trades_t, fees_t)
        eq_t.to_csv(os.path.join(OUT, f"nav_{label}_timed.csv"), index=False, encoding="utf-8-sig")
        log_t.to_csv(os.path.join(OUT, f"rebalance_{label}_timed.csv"), index=False, encoding="utf-8-sig")
        trades_t.to_csv(os.path.join(OUT, f"trades_{label}_timed.csv"), index=False, encoding="utf-8-sig")
        if not dec_t.empty:
            dec_t.to_csv(os.path.join(OUT, f"timing_decisions_{label}.csv"),
                         index=False, encoding="utf-8-sig")

    print("\n" + "=" * 86)
    print("基线 vs ERP择时（2020-01 ~ 2026-07，100万，阈值±5pp，单次步进±5pp）")
    print("=" * 86)
    hdr = f"{'版本':<16}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'调仓':>5}{'费用':>7}"
    print(hdr)
    for label, (eq_b, log_b, fees_b, eq_t, log_t, dec_t, trades_t, fees_t) in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        for tag, eq, log, fees in [("基线", eq_b, log_b, fees_b), ("择时", eq_t, log_t, fees_t)]:
            m = metrics(eq)
            print(f"{cn+'·'+tag:<16}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%"
                  f"{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{len(log):>5}{fees:>7,.0f}")

    print("\n年度收益（基线 → 择时）:")
    for label, (eq_b, log_b, fees_b, eq_t, log_t, dec_t, trades_t, fees_t) in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        yb, yt = yearly(eq_b), yearly(eq_t)
        print(f"  {cn}: " + "  ".join(
            f"{y}: {yb.get(y, 0):+.1f}→{yt.get(y, 0):+.1f}" for y in sorted(set(yb) | set(yt))))

    for label, (eq_b, log_b, fees_b, eq_t, log_t, dec_t, trades_t, fees_t) in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        if not dec_t.empty:
            print(f"\n择时决策记录（{cn}）:")
            print(dec_t.to_string(index=False))
        print(f"\n交易明细（{cn}·择时）:")
        t = trades_t.copy()
        t["date"] = pd.to_datetime(t["date"]).dt.date
        print(t.to_string(index=False))

    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
