"""
全天候策略归因分析
==================
基于 backtest.py 的引擎与价格面板，输出：
  1. 两个版本的逐年收益（复核口径，与 backtest.py 一致）
  2. 各品种自身逐年收益（前复权价格口径，含分红再投）
  3. 各品种对组合的逐年贡献（精确流水法：腿盈亏 = 期末市值-期初市值-净买入流水，
     含交易费用；Σ各腿盈亏 = 组合总盈亏，可自验）
运行: python analyze.py  （无需 miniQMT，用 data/ 已有数据；pandas 即可）
"""
import os
import pandas as pd
from backtest import (load_panel, VARIANTS, LEGS, buy_shares, sell_shares,
                      CASH_LEG, INITIAL_CAPITAL, START_DATE, REBAL_BAND)

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

LEG_ORDER = ["hlb", "sp500", "bond10", "gold", "soybean", "cash"]
LEG_CN = {leg: LEGS[leg]["name"] for leg in LEG_ORDER}


def run_with_ledger(panel: pd.DataFrame, weights: dict):
    """backtest.run_backtest 的流水版：额外记录每笔交易流水与每日各腿市值"""
    dates = panel.index[panel.index >= pd.Timestamp(START_DATE)]
    holdings = {leg: 0 for leg in weights}
    cash = INITIAL_CAPITAL
    flows = []   # (date, leg, flow)  flow>0=买入耗资(含费), <0=卖出净得

    def assets_on(date):
        return cash + sum(holdings[leg] * panel.loc[date, leg] for leg in holdings)

    def rebalance(date):
        nonlocal cash
        total = assets_on(date)
        prices = panel.loc[date]
        for leg, w in weights.items():          # 先卖
            if leg == CASH_LEG:
                continue
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * w
            if cur_val > tgt_val:
                sh = int((cur_val - tgt_val) / prices[leg] / 100) * 100
                if 0 < sh <= holdings[leg]:
                    proceeds, _ = sell_shares(leg, prices[leg], sh)
                    holdings[leg] -= sh
                    cash += proceeds
                    flows.append((date, leg, -proceeds))
        if CASH_LEG in weights and holdings[CASH_LEG] > 0:   # 现金腿超配减仓，回笼资金
            tgt_cash = total * weights[CASH_LEG]
            etf_val = holdings[CASH_LEG] * prices[CASH_LEG]
            if etf_val > tgt_cash:
                sh = min(holdings[CASH_LEG],
                         int((etf_val - tgt_cash) / prices[CASH_LEG] / 100) * 100)
                if sh > 0:
                    proceeds, _ = sell_shares(CASH_LEG, prices[CASH_LEG], sh)
                    holdings[CASH_LEG] -= sh
                    cash += proceeds
                    flows.append((date, CASH_LEG, -proceeds))
        for leg, w in weights.items():          # 后买
            if leg == CASH_LEG:
                continue
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * w
            if tgt_val > cur_val:
                sh, spent = buy_shares(leg, prices[leg], tgt_val - cur_val, cash)
                if sh:
                    holdings[leg] += sh
                    cash -= spent
                    flows.append((date, leg, spent))
        if CASH_LEG in weights:                 # 现金腿低配补买（超配已在卖出阶段处理）
            tgt_cash = total * weights[CASH_LEG]
            etf_val = holdings[CASH_LEG] * prices[CASH_LEG]
            if etf_val < tgt_cash and cash > 0:
                sh, spent = buy_shares(CASH_LEG, prices[CASH_LEG], tgt_cash - etf_val, cash)
                if sh:
                    holdings[CASH_LEG] += sh
                    cash -= spent
                    flows.append((date, CASH_LEG, spent))

    rebalance(dates[0])
    check_dates = []
    for y in range(dates[0].year, dates[-1].year + 1):
        for m in (3, 6, 9, 12):
            md = dates[(dates.year == y) & (dates.month == m)]
            if len(md):
                check_dates.append(md[-1])

    rows, leg_rows = [], []
    for date in dates:
        if date in check_dates and date != dates[0]:
            total = assets_on(date)
            prices = panel.loc[date]
            dev = max(
                abs((cash + holdings[l] * prices[l] if l == CASH_LEG else holdings[l] * prices[l])
                    / total - w)
                for l, w in weights.items())
            if dev >= REBAL_BAND:
                rebalance(date)
        rows.append({"date": date, "assets": assets_on(date)})
        for leg in holdings:
            leg_rows.append({"date": date, "leg": leg,
                             "value": holdings[leg] * panel.loc[date, leg]})
    eq = pd.DataFrame(rows)
    leg_val = pd.DataFrame(leg_rows).pivot(index="date", columns="leg", values="value")
    flow_df = pd.DataFrame(flows, columns=["date", "leg", "flow"])
    return eq, leg_val, flow_df


def yearly_table(series_by_year: dict, columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(series_by_year).reindex(columns=columns)
    df.index.name = "年份"
    return df


def main():
    panel = load_panel()
    panel_bt = panel[panel.index >= pd.Timestamp(START_DATE)]

    # ── 1. 各品种自身逐年收益（价格口径）──────────────────────────
    px = panel[LEG_ORDER + ["hs300"]]
    year_end = px.groupby(px.index.year).last()          # 每年最后交易日收盘
    prev = px[px.index < pd.Timestamp("2020-01-01")].iloc[-1]  # 2019 年末
    leg_ret = {}
    for y in year_end.index:
        if y < 2020:
            prev = year_end.loc[y]
            continue
        cur = year_end.loc[y]
        leg_ret[y] = (cur / prev - 1) * 100
        prev = cur
    leg_ret_df = pd.DataFrame(leg_ret).T
    leg_ret_df.index.name = "年份"
    cum = (year_end.iloc[-1] / px[px.index < pd.Timestamp("2020-01-01")].iloc[-1] - 1) * 100

    # ── 2. 两版本逐年收益 + 各腿贡献 ──────────────────────────────
    variant_year_ret, contrib = {}, {}
    for label, w in VARIANTS.items():
        eq, leg_val, flow_df = run_with_ledger(panel_bt, w)
        years = sorted(eq["date"].dt.year.unique())
        yr, ct = {}, {}
        assets_prev, val_prev = INITIAL_CAPITAL, leg_val.iloc[0] * 0
        for y in years:
            yd = eq[eq["date"].dt.year == y]
            assets_end = yd["assets"].iloc[-1]
            yr[y] = (assets_end / assets_prev - 1) * 100
            lv_end = leg_val[leg_val.index.year == y].iloc[-1]
            fl = flow_df[flow_df["date"].dt.year == y].groupby("leg")["flow"].sum()
            pnl = lv_end - val_prev - fl.reindex(lv_end.index).fillna(0)
            ct[y] = (pnl / assets_prev * 100).to_dict()
            assets_prev, val_prev = assets_end, lv_end
        variant_year_ret[label] = yr
        contrib[label] = pd.DataFrame(ct).T.fillna(0).reindex(columns=LEG_ORDER).fillna(0)
        # 自验：Σ腿盈亏 = 组合盈亏
        total_pnl = contrib[label].sum(axis=1)
        diff = (total_pnl - pd.Series(yr)).abs().max()
        assert diff < 0.01, f"{label} 归因不平，最大偏差 {diff}"

    # ── 输出 ──────────────────────────────────────────────────────
    pd.set_option("display.width", 160)
    pd.set_option("display.float_format", lambda x: f"{x:+.2f}")

    print("\n【各品种自身逐年收益 %】（前复权，含分红再投；2026 为年初至今）")
    show = leg_ret_df.rename(columns={**LEG_CN, "hs300": "沪深300"})
    print(show.to_string())
    print("\n【各品种全期累计收益 %】（2019 年末 ~ 2026-07-22）")
    print(cum.rename({**LEG_CN, "hs300": "沪深300"}).to_string())

    for label, cn in [("steady", "稳健版"), ("aggressive", "进取版")]:
        print(f"\n【{cn}】逐年收益 % 与各腿贡献（pp，合计=组合收益）")
        c = contrib[label].rename(columns=LEG_CN)
        c["组合收益"] = pd.Series(variant_year_ret[label])
        print(c.to_string())
        c.to_csv(os.path.join(OUT, f"contribution_{label}.csv"), encoding="utf-8-sig")
    print(f"\n贡献明细已存: {OUT}\\contribution_*.csv")


if __name__ == "__main__":
    main()
