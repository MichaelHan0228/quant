"""
全天候 + 中证A500低估加仓层回测
================================
在 backtest.py 的 v2 基线（阈值±5pp + 季度检查）上叠加一条 A500 估值加仓层：

信号（PE_TTM 分位）：
  - 2024-09-03 前：A500 PE 不存在，用沪深300 PE 5年滚动分位代理
                    （同为大盘宽基，重叠期相关性见运行输出）
  - 2024-09-03 起：中证A500(000510) PE 扩展窗口分位（满60个观测后接管）
触发（滞回，防边界横跳）：
  - 季度检查日评估：分位 ≤20% → A500 目标仓位 15%
                    分位 ≥30% → A500 目标仓位 0%
                    20%~30% 之间维持原状态
出资：bond10 −10pp + cash −5pp（稳健 35→25/10→5；进取 25→15/5→0），退出时回补
标的衔接：2024-10-15（首批A500ETF 563360 上市）前用 510300 沪深300ETF，
          上市日起按收盘价拼接切换为 563360（拼接法同 hlb 重建；
          换股未建模交易费用，约 0.03%×15%≈0.005% 组合净值，可忽略）
纪律：沿用 v2——季度检查日 + 任何资产偏离目标 ≥±5pp 才调仓

对照：steady / aggressive 基线（固定权重，来自 backtest.py）
输出：output/nav_*_a500.csv、rebalance_*_a500.csv、trades_*_a500.csv、a500_decisions_*.csv
"""
import os

import pandas as pd

import backtest
from backtest import (LEGS, VARIANTS, INITIAL_CAPITAL, START_DATE, REBAL_BAND,
                      CASH_LEG, load_panel, buy_shares, sell_shares,
                      metrics, yearly, run_backtest)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

A500_LEG = "a500"
A500_W = 0.15          # 触发后的目标仓位
FUND_BOND = 0.10       # 十年国债出资 10pp
FUND_CASH = 0.05       # 货币ETF出资 5pp（进取版货币仅5pp，全部出完归零）
ENTER_PCT = 0.20       # 进场：分位 ≤20%
EXIT_PCT = 0.30        # 出场：分位 ≥30%（滞回带 20~30%）
ETF_LISTING = pd.Timestamp("2024-10-15")   # 首批A500ETF 563360 上市日
ROLL_WINDOW = 1250     # 沪深300代理：5年≈1250个交易日
MIN_OBS_300 = 750      # 代理窗口不足3年不出信号
MIN_OBS_500 = 60       # A500扩展窗口满60个观测（≈3个月）才接管

# 注册第7条腿（普通ETF 2tick 价差），buy/sell_shares 通过模块全局 LEGS 读取
LEGS[A500_LEG] = {"name": "A500/沪深300", "spread": 0.002}


def load_a500_series(panel_index: pd.DatetimeIndex, hs300: pd.Series = None) -> pd.Series:
    """A500腿价格序列：2024-10-15 前 510300，上市日起按收盘价拼接 563360。
    hs300 可传入外部沪深300序列（如长面板），缺省读 data/510300.csv。"""
    if hs300 is None:
        hs300 = pd.read_csv(os.path.join(DATA, "510300.csv"),
                            parse_dates=["date"]).set_index("date")["close"].sort_index()
    etf = pd.read_csv(os.path.join(DATA, "563360.csv"),
                      parse_dates=["date"]).set_index("date")["close"].sort_index()
    scale = hs300[ETF_LISTING] / etf[ETF_LISTING]
    spliced = pd.concat([hs300[hs300.index < ETF_LISTING],
                         (etf * scale)[etf.index >= ETF_LISTING]])
    spliced = spliced[~spliced.index.duplicated(keep="last")].sort_index()
    print(f"  A500腿拼接: 510300({ETF_LISTING.date()})={hs300[ETF_LISTING]:.3f}"
          f" ← 563360={etf[ETF_LISTING]:.3f} ×{scale:.4f}")
    return spliced.reindex(panel_index).ffill()


def load_a500_signal():
    """PE分位信号：A500扩展窗口分位优先，此前沪深300五年滚动分位代理"""
    pe300 = pd.read_csv(os.path.join(DATA, "csi300_pe.csv"),
                        parse_dates=["date"]).set_index("date")["pe"].sort_index()
    pe300 = pe300[~pe300.index.duplicated(keep="last")]
    pct300 = pe300.rolling(ROLL_WINDOW, min_periods=MIN_OBS_300).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)

    pe500 = pd.read_csv(os.path.join(DATA, "a500_pe.csv"),
                        parse_dates=["date"]).set_index("date")["pe"].sort_index()
    pe500 = pe500[~pe500.index.duplicated(keep="last")]
    pct500 = pe500.expanding(MIN_OBS_500).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)

    # 重叠期（两者都有值）相关性——代理有效性的证据
    both = pd.DataFrame({"proxy300": pct300, "a500": pct500}).dropna()
    if len(both) > 20:
        print(f"  信号重叠期({both.index[0].date()}~{both.index[-1].date()}, "
              f"{len(both)}日) 分位相关性: {both['proxy300'].corr(both['a500']):.3f}")

    signal = pct500.combine_first(pct300).dropna()
    signal.name = "pct"
    return signal


def run_backtest_a500(panel: pd.DataFrame, base_weights: dict,
                      signal: pd.Series, label: str):
    """A500低估加仓层回测。返回(净值, 再平衡日志, 决策日志, 交易明细, 总费用)"""
    dates = panel.index[panel.index >= pd.Timestamp(START_DATE)]
    weights = dict(base_weights)
    weights[A500_LEG] = 0.0
    holdings = {leg: 0 for leg in weights}
    cash = INITIAL_CAPITAL
    total_fees = 0.0
    rebal_log, decision_log, trade_log = [], [], []
    invested = False   # A500仓位状态（滞回状态机）

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
            # 1) A500信号：滞回状态机更新目标权重
            sig = signal[signal.index <= date]
            pct = sig.iloc[-1] if not sig.empty else float("nan")
            if not pd.isna(pct):
                new_state = invested
                if not invested and pct <= ENTER_PCT:
                    new_state = True
                elif invested and pct >= EXIT_PCT:
                    new_state = False
                if new_state != invested:
                    invested = new_state
                    if invested:
                        weights[A500_LEG] = A500_W
                        weights["bond10"] = round(base_weights["bond10"] - FUND_BOND, 4)
                        weights[CASH_LEG] = round(base_weights[CASH_LEG] - FUND_CASH, 4)
                    else:
                        weights[A500_LEG] = 0.0
                        weights["bond10"] = base_weights["bond10"]
                        weights[CASH_LEG] = base_weights[CASH_LEG]
                    decision_log.append({
                        "date": date, "pe_pct": round(pct, 4),
                        "state": "进场15%" if invested else "退出归零",
                        "a500": weights[A500_LEG], "bond10": weights["bond10"],
                        "cash": weights[CASH_LEG]})
            # 2) 阈值纪律：偏离 ≥5pp 才调仓
            total = assets_on(date)
            prices = panel.loc[date]

            def _leg_val(leg):
                if leg == CASH_LEG:
                    return cash + holdings[leg] * prices[leg]
                return holdings[leg] * prices[leg]

            dev = max(abs(_leg_val(leg) / total - w) for leg, w in weights.items())
            if dev >= REBAL_BAND:
                rebalance(date, f"偏离{dev*100:.1f}pp(PE分位{pct:.0%})"
                                if not pd.isna(pct) else f"偏离{dev*100:.1f}pp")
        rows.append({"date": date, "assets": assets_on(date)})
    eq = pd.DataFrame(rows)
    eq["nav"] = eq["assets"] / INITIAL_CAPITAL
    return eq, pd.DataFrame(rebal_log), pd.DataFrame(decision_log), pd.DataFrame(trade_log), total_fees


def main():
    print("加载价格面板与A500信号 ...")
    panel = load_panel()
    panel[A500_LEG] = load_a500_series(panel.index)
    panel = panel[panel.index >= pd.Timestamp(START_DATE)]
    signal = load_a500_signal()
    print(f"  行情 {panel.index[0].date()} ~ {panel.index[-1].date()}, "
          f"信号 {signal.index[0].date()} ~ {signal.index[-1].date()}")
    s = signal.iloc[-1]
    print(f"  最新PE分位({signal.index[-1].date()}): {s:.1%}"
          f"（≤{ENTER_PCT:.0%}进场 / ≥{EXIT_PCT:.0%}出场）")

    results = {}
    for label, w in VARIANTS.items():
        eq_b, log_b, fees_b = run_backtest(panel, w, label)                       # 基线
        eq_a, log_a, dec_a, trades_a, fees_a = run_backtest_a500(panel, w, signal, label)
        results[label] = (eq_b, log_b, fees_b, eq_a, log_a, dec_a, trades_a, fees_a)
        eq_a.to_csv(os.path.join(OUT, f"nav_{label}_a500.csv"), index=False, encoding="utf-8-sig")
        log_a.to_csv(os.path.join(OUT, f"rebalance_{label}_a500.csv"), index=False, encoding="utf-8-sig")
        trades_a.to_csv(os.path.join(OUT, f"trades_{label}_a500.csv"), index=False, encoding="utf-8-sig")
        if not dec_a.empty:
            dec_a.to_csv(os.path.join(OUT, f"a500_decisions_{label}.csv"),
                         index=False, encoding="utf-8-sig")

    print("\n" + "=" * 86)
    print("基线 vs +A500低估加仓层（2020-01 ~ 2026-07，100万，阈值±5pp）")
    print("=" * 86)
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
        print(f"\n再平衡记录（{cn}·+A500层）:")
        print(log_a.to_string(index=False))

    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
