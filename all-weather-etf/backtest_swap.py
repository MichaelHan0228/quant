"""
全天候 + ERP择时 + 极值区股票腿换仓（hlb ↔ hs300）
====================================================
在 backtest_timing.py 的 ERP 权重择时之上，加一条独立的"载体切换"状态机：
  - ERP 5年分位 ≥ 90%（股票投资价值极远高于债券）
      → 股票腿持仓整体从 红利低波(563020) 换成 沪深300ETF(510300)（拿高beta）
  - 分位回落 < 60%（回到合理区间）→ 换回 红利低波
  - 90% 进 / 60% 出，30pp 迟滞带防边界横跳
  - 换仓只改"用什么扛股票腿"，ERP 决定的权重档位不变（换仓期间权重漂移照常生效）
  - 判定时机与现有纪律一致：仅在季度检查日（3/6/9/12月末）

对照：基线（固定权重） / ERP择时 / ERP择时+换仓
输出：output/nav_*_swap.csv、rebalance_*_swap.csv、trades_*_swap.csv、
      swap_decisions_*.csv、yearly_compare.csv
"""
import os

import pandas as pd

import backtest
from backtest import (VARIANTS, INITIAL_CAPITAL, START_DATE, REBAL_BAND,
                      CASH_LEG, load_panel, buy_shares, sell_shares,
                      metrics, yearly, run_backtest)
from backtest_timing import run_backtest_timed
from timing_signal import build_signal

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

# 沪深300ETF 加入费用模型（2tick 价差，与普通ETF一致）
backtest.LEGS["hs300"] = {"name": "沪深300", "spread": 0.002}
LEGS = backtest.LEGS

MAX_STEP = 0.05
STOCK_LEG = "hlb"
HEDGE_LEG = "bond10"
SWAP_IN = 0.90    # 分位 ≥90% 换入沪深300
SWAP_OUT = 0.60   # 分位 <60% 换回红利低波


def run_backtest_swap(panel: pd.DataFrame, base_weights: dict, signal: pd.DataFrame,
                      label: str, swap_in: float = SWAP_IN, swap_out: float = SWAP_OUT):
    """ERP权重择时 + 极值区股票腿换仓。返回(净值, 再平衡日志, 择时决策, 换仓记录, 交易明细, 总费用)"""
    dates = panel.index[panel.index >= pd.Timestamp(START_DATE)]
    weights = dict(base_weights)
    holdings = {leg: 0 for leg in list(weights) + ["hs300"]}
    cash = INITIAL_CAPITAL
    total_fees = 0.0
    rebal_log, decision_log, trade_log, swap_log = [], [], [], []
    cur_stock = base_weights[STOCK_LEG]   # 股票腿目标权重（带步进限制）
    vehicle = STOCK_LEG                   # 当前股票腿载体: hlb / hs300

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
        # 非当前载体的股票腿残仓（换仓后旧载体目标为0）也要清掉
        other = "hs300" if vehicle == STOCK_LEG else STOCK_LEG
        if holdings.get(other, 0) > 0 and weights.get(other, 0) == 0:
            sh = holdings[other]
            proceeds, fee = sell_shares(other, prices[other], sh)
            holdings[other] = 0
            cash += proceeds
            fees += fee
            _log(other, "卖", sh, prices[other])
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

    check_dates = []
    for y in range(dates[0].year, dates[-1].year + 1):
        for m in (3, 6, 9, 12):
            md = dates[(dates.year == y) & (dates.month == m)]
            if len(md):
                check_dates.append(md[-1])

    rows = []
    for date in dates:
        if date in check_dates and date != dates[0]:
            sig = signal[signal.index <= date]
            pct = None
            if not sig.empty:
                pct = sig["pct"].iloc[-1]
                adj = sig["adjust"].iloc[-1]
                # 1) 换仓状态机（先于权重漂移）
                if vehicle == STOCK_LEG and pct >= swap_in:
                    vehicle = "hs300"
                    swap_log.append({"date": date, "action": "换入沪深300",
                                     "erp_pct": round(pct, 4)})
                elif vehicle == "hs300" and pct < swap_out:
                    vehicle = STOCK_LEG
                    swap_log.append({"date": date, "action": "换回红利低波",
                                     "erp_pct": round(pct, 4)})
                # 2) ERP 权重漂移（步进 ±5pp，换仓期间照常生效）
                desired = base_weights[STOCK_LEG] + adj
                new_stock = cur_stock + max(-MAX_STEP, min(MAX_STEP, desired - cur_stock))
                new_stock = round(new_stock, 4)
                if new_stock != cur_stock:
                    decision_log.append({"date": date, "erp_pct": round(pct, 4),
                                         "band_adj": adj, "stock_target": new_stock,
                                         "vehicle": vehicle,
                                         "bond10_target": round(
                                             base_weights[HEDGE_LEG] - (new_stock - base_weights[STOCK_LEG]), 4)})
                    cur_stock = new_stock
            # 3) 重建股票腿键（载体切换后旧载体目标为0）
            weights.pop(STOCK_LEG, None)
            weights.pop("hs300", None)
            weights[vehicle] = cur_stock
            weights[HEDGE_LEG] = round(base_weights[HEDGE_LEG] - (cur_stock - base_weights[STOCK_LEG]), 4)
            # 4) 阈值纪律：偏离 ≥5pp 才调仓（换仓必然触发）
            total = assets_on(date)
            prices = panel.loc[date]

            def _leg_val(leg):
                if leg == CASH_LEG:
                    return cash + holdings[leg] * prices[leg]
                return holdings[leg] * prices[leg]

            dev = max(abs(_leg_val(leg) / total - w) for leg, w in weights.items())
            # 非当前载体的残仓也算偏离
            other = "hs300" if vehicle == STOCK_LEG else STOCK_LEG
            if holdings.get(other, 0) > 0:
                dev = max(dev, holdings[other] * prices[other] / total)
            if dev >= REBAL_BAND:
                tag = f"ERP分位{pct:.0%}" if pct is not None else ""
                rebalance(date, f"偏离{dev*100:.1f}pp{tag}{'·'+vehicle if vehicle != STOCK_LEG else ''}")
        rows.append({"date": date, "assets": assets_on(date)})
    eq = pd.DataFrame(rows)
    eq["nav"] = eq["assets"] / INITIAL_CAPITAL
    return (eq, pd.DataFrame(rebal_log), pd.DataFrame(decision_log),
            pd.DataFrame(swap_log), pd.DataFrame(trade_log), total_fees)


def main():
    print("加载价格面板与ERP信号 ...")
    panel = load_panel()
    panel = panel[panel.index >= pd.Timestamp(START_DATE)]
    signal = build_signal()
    print(f"  行情 {panel.index[0].date()} ~ {panel.index[-1].date()}, "
          f"信号 {signal.index[0].date()} ~ {signal.index[-1].date()}")

    results = {}
    for label, w in VARIANTS.items():
        eq_b, log_b, fees_b = run_backtest(panel, w, label)
        eq_t, log_t, dec_t, trades_t, fees_t = run_backtest_timed(panel, w, signal, label)
        eq_s, log_s, dec_s, swap_s, trades_s, fees_s = run_backtest_swap(panel, w, signal, label)
        results[label] = {
            "base":  (eq_b, log_b, fees_b),
            "timed": (eq_t, log_t, fees_t),
            "swap":  (eq_s, log_s, dec_s, swap_s, trades_s, fees_s),
        }
        eq_s.to_csv(os.path.join(OUT, f"nav_{label}_swap.csv"), index=False, encoding="utf-8-sig")
        log_s.to_csv(os.path.join(OUT, f"rebalance_{label}_swap.csv"), index=False, encoding="utf-8-sig")
        trades_s.to_csv(os.path.join(OUT, f"trades_{label}_swap.csv"), index=False, encoding="utf-8-sig")
        if not swap_s.empty:
            swap_s.to_csv(os.path.join(OUT, f"swap_decisions_{label}.csv"),
                          index=False, encoding="utf-8-sig")

    # ── 总表 ────────────────────────────────────────────────
    print("\n" + "=" * 88)
    print("基线 / ERP择时 / ERP择时+换仓（2020-01 ~ 2026-07，100万，换仓阈值 90%进 60%出）")
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
    header = f"{'年份':<6}" + "".join(f"{h:>12}" for h in
               ["稳健基线", "稳健择时", "稳健换仓", "进取基线", "进取择时", "进取换仓"])
    print(header)
    yrows = []
    for y in years:
        row = {"年份": y}
        line = f"{y:<6}"
        for label, res in results.items():
            cn = "稳健" if label == "steady" else "进取"
            for tag, key in [("基线", "base"), ("择时", "timed"), ("换仓", "swap")]:
                r = yearly(res[key][0]).get(y)
                row[f"{cn}{tag}"] = round(r, 1) if r is not None else None
                line += f"{f'{r:+.1f}%' if r is not None else '-':>12}"
        yrows.append(row)
        print(line)
    pd.DataFrame(yrows).to_csv(os.path.join(OUT, "yearly_compare.csv"),
                               index=False, encoding="utf-8-sig")

    # ── 换仓记录 ────────────────────────────────────────────
    for label, res in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        swap_s = res["swap"][3]
        if not swap_s.empty:
            print(f"\n换仓记录（{cn}）:")
            print(swap_s.to_string(index=False))
        print(f"\n调仓明细（{cn}·择时+换仓）:")
        t = res["swap"][4].copy()
        t["date"] = pd.to_datetime(t["date"]).dt.date
        print(t.to_string(index=False))

    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
