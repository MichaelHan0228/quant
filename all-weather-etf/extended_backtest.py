"""
全天候策略延长回测（2015-01 ~ 今）
====================================
backtest.py 的窗口（2020 起）受标的上市时间限制，本脚本用代理把窗口延长到 2015，
覆盖 2015 股灾、2016-17 债熊、2018 熊市、2020 疫情等完整 regime。

数据代理（与生产口径的差异，阅读结果时注意）：
  hlb     红利低波：H20269 全收益指数（全程；2023-12-14 锚定到 563020 前复权，同生产口径）
                   ⚠ 指数未扣 ETF 管理费(~0.6%/年)与跟踪误差，全程偏乐观约 0.5pp/年
  sp500   标普500：513500 真实 ETF（2014-01 上市），QDII 溢价波动已含在价格中
  bond10  债券腿：2017-08-24 前 = 511010（5年国债，久期~4），之后 = 511260（十年国债，久期~8）
                   ⚠ 2016-17 债熊段用的是 5 年久期，该段债券腿回撤比真十年国债小约一半
  gold    黄金：518880 真实 ETF（2014-01 上市）
  soybean 豆粕：159985 于 2019-12-05 才上市，之前无标的 → 权重并入现金腿，
                2020 年首个交易日强制再平衡切换到生产权重（与生产窗口衔接）
  cash    货币：511880 真实 ETF（2014-01 上市）

运行: python extended_backtest.py（纯 pandas，无需 miniQMT；data/ 需为 2014 起的全量数据）
"""
import os
import pandas as pd
from backtest import (LEGS, VARIANTS, buy_shares, sell_shares, CASH_LEG,
                      REBAL_BAND, INITIAL_CAPITAL, LISTING_DATE, metrics, yearly)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

EXT_START = "2015-01-01"
SWITCH_DATE = "2020-01-02"     # 2020 年首个交易日：豆粕上线，切换到生产权重
BOND_SPLICE = None             # 由 511260 首个交易日决定

# 2020 前权重：豆粕 5% 并入现金
W_PRE = {
    "steady":     {"hlb": 0.20, "sp500": 0.10, "bond10": 0.35, "gold": 0.20, "soybean": 0.0, "cash": 0.15},
    "aggressive": {"hlb": 0.30, "sp500": 0.15, "bond10": 0.25, "gold": 0.20, "soybean": 0.0, "cash": 0.10},
}


def _close(code: str) -> pd.Series:
    return pd.read_csv(os.path.join(DATA, f"{code}.csv"), parse_dates=["date"]).set_index("date")["close"]


def load_panel_ext() -> pd.DataFrame:
    """延长版价格面板：2014 起，债券拼接、豆粕前段常数填充"""
    # 红利低波：H20269 全收益指数锚定 563020 前复权（同生产 load_panel）
    tr = pd.read_csv(os.path.join(DATA, "h20269.csv"), parse_dates=["date"]).set_index("date")["close"]
    qfq = pd.read_csv(os.path.join(DATA, "563020_qfq.csv"), parse_dates=["date"]).set_index("date")["close"]
    hlb_recon = qfq[LISTING_DATE] * tr / tr[LISTING_DATE]
    hlb = pd.concat([hlb_recon[hlb_recon.index < LISTING_DATE], qfq])
    hlb = hlb[~hlb.index.duplicated(keep="last")].sort_index()

    # 债券：511010(5年) → 511260(10年) 在 511260 上市日拼接（保持收益率连续）
    b5, b10 = _close("511010"), _close("511260")
    t0 = b10.index[0]                     # 2017-08-24
    b5_pre = b5[b5.index < t0]
    bond = pd.concat([b5_pre * (b10[t0] / b5_pre.iloc[-1]), b10])

    panel = {"hlb": hlb, "sp500": _close("513500"), "bond10": bond,
             "gold": _close("518880"), "soybean": _close("159985"),
             "cash": _close("511880"), "hs300": _close("510300")}
    idx = hlb.index                     # 用指数交易日历
    out = pd.DataFrame(index=idx)
    for leg, s in panel.items():
        out[leg] = s.reindex(idx).ffill()
    # 豆粕上市前常数回填（该段权重为 0，价格仅为占位，不影响结果）
    out["soybean"] = out["soybean"].bfill()
    return out.dropna(subset=["hlb", "sp500", "bond10", "gold", "cash", "hs300"])


def run_extended(panel: pd.DataFrame, w_pre: dict, w_post: dict, label: str):
    """权重表引擎：SWITCH_DATE 前用 w_pre（豆粕 0%、现金加重），之后用 w_post（生产权重）"""
    dates = panel.index[panel.index >= pd.Timestamp(EXT_START)]
    switch = dates[dates >= pd.Timestamp(SWITCH_DATE)][0]
    weights = dict(w_pre)
    holdings = {leg: 0 for leg in weights}
    cash = INITIAL_CAPITAL
    total_fees = 0.0
    rebal_log = []

    def assets_on(date):
        return cash + sum(holdings[leg] * panel.loc[date, leg] for leg in holdings)

    def rebalance(date, w, reason):
        nonlocal cash, total_fees
        total = assets_on(date)
        prices = panel.loc[date]
        fees = 0.0
        for leg, wt in w.items():          # 先卖（含现金腿超配减仓）
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * wt
            if leg == CASH_LEG:
                if cur_val > tgt_val:
                    sh = min(holdings[leg], int((cur_val - tgt_val) / prices[leg] / 100) * 100)
                    if sh > 0:
                        proceeds, _ = sell_shares(leg, prices[leg], sh)
                        holdings[leg] -= sh
                        cash += proceeds
                continue
            if cur_val > tgt_val:
                sh = int((cur_val - tgt_val) / prices[leg] / 100) * 100
                if 0 < sh <= holdings[leg]:
                    proceeds, fee = sell_shares(leg, prices[leg], sh)
                    holdings[leg] -= sh
                    cash += proceeds
                    fees += fee
        for leg, wt in w.items():          # 后买（现金腿最后兜底）
            if leg == CASH_LEG:
                continue
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * wt
            if tgt_val > cur_val:
                sh, spent = buy_shares(leg, prices[leg], tgt_val - cur_val, cash)
                if sh:
                    holdings[leg] += sh
                    cash -= spent
                    fees += spent - sh * (prices[leg] + LEGS[leg]["spread"])
        if CASH_LEG in w:                  # 现金腿低配补买
            tgt_cash = total * w[CASH_LEG]
            etf_val = holdings[CASH_LEG] * prices[CASH_LEG]
            if etf_val < tgt_cash and cash > 0:
                sh, spent = buy_shares(CASH_LEG, prices[CASH_LEG], tgt_cash - etf_val, cash)
                if sh:
                    holdings[CASH_LEG] += sh
                    cash -= spent
        total_fees += fees
        rebal_log.append({"date": date, "reason": reason, "fees": round(fees, 2)})

    rebalance(dates[0], weights, "期初建仓")

    check_dates = []
    for y in range(dates[0].year, dates[-1].year + 1):
        for m in (3, 6, 9, 12):
            md = dates[(dates.year == y) & (dates.month == m)]
            if len(md):
                check_dates.append(md[-1])

    rows = []
    for date in dates:
        if date == switch:
            weights = dict(w_post)
            rebalance(date, weights, "豆粕上线切换权重")
        elif date in check_dates and date != dates[0]:
            total = assets_on(date)
            prices = panel.loc[date]
            dev = max(
                abs((cash + holdings[l] * prices[l] if l == CASH_LEG else holdings[l] * prices[l])
                    / total - wt)
                for l, wt in weights.items())
            if dev >= REBAL_BAND:
                rebalance(date, weights, f"偏离{dev*100:.1f}pp")
        rows.append({"date": date, "assets": assets_on(date)})
    eq = pd.DataFrame(rows)
    eq["nav"] = eq["assets"] / INITIAL_CAPITAL
    return eq, pd.DataFrame(rebal_log), total_fees


def main():
    print("加载延长价格面板 ...")
    panel = load_panel_ext()
    panel = panel[panel.index >= pd.Timestamp(EXT_START)]
    print(f"  {panel.index[0].date()} ~ {panel.index[-1].date()}, {len(panel)} 个交易日")

    print("\n" + "=" * 74)
    print(f"全天候延长回测（{EXT_START[:4]}-01 ~ 2026-07，100万，阈值±5pp，2020前豆粕并入现金）")
    print("=" * 74)
    hdr = f"{'版本':<12}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'再平衡':>6}{'费用':>8}"
    print(hdr)
    results = {}
    for label in ("steady", "aggressive"):
        eq, log, fees = run_extended(panel, W_PRE[label], VARIANTS[label], label)
        m = metrics(eq)
        results[label] = (eq, m, yearly(eq), log, fees)
        eq.to_csv(os.path.join(OUT, f"nav_ext_{label}.csv"), index=False, encoding="utf-8-sig")
        log.to_csv(os.path.join(OUT, f"rebalance_ext_{label}.csv"), index=False, encoding="utf-8-sig")
        cn = "稳健版" if label == "steady" else "进取版"
        print(f"{cn:<12}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%{m['sharpe']:>7.2f}"
              f"{m['calmar']:>8.2f}{len(log):>6}{fees:>8,.0f}")

    hs300_eq = panel[["hs300"]].dropna().reset_index()
    hs300_eq["assets"] = hs300_eq["hs300"] / hs300_eq["hs300"].iloc[0] * INITIAL_CAPITAL
    hs300_eq["nav"] = hs300_eq["assets"] / INITIAL_CAPITAL
    m300 = metrics(hs300_eq[["date", "assets", "nav"]])
    print(f"{'沪深300基准':<12}{m300['total']:>8.1f}%{m300['ann']:>7.2f}%{m300['mdd']:>8.1f}%{m300['sharpe']:>7.2f}")

    print("\n年度收益对比:")
    y300 = yearly(hs300_eq[["date", "assets", "nav"]])
    years_all = sorted(set(list(results['steady'][2].keys()) + list(y300.keys())))
    print(f"{'年份':<8}{'稳健版':>9}{'进取版':>9}{'沪深300':>9}")
    for y in years_all:
        s = results['steady'][2].get(y)
        a = results['aggressive'][2].get(y)
        b = y300.get(y)
        print(f"{y:<8}{s if s is None else f'{s:+.1f}%':>9}{a if a is None else f'{a:+.1f}%':>9}{b if b is None else f'{b:+.1f}%':>9}")

    print("\n再平衡记录（稳健版）:")
    print(results['steady'][3].to_string(index=False))
    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
