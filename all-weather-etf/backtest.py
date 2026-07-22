"""
全天候ETF再平衡策略回测
========================
规则：
  - 期初按目标权重一次性建仓（100万，收盘价+费用）
  - 每年3/6/9/12月最后一个交易日检查权重
  - 任何资产偏离目标权重 ≥±5个百分点 → 全部调回目标权重（阈值触发，不触发不动）
  - 费用：佣金万1.5最低5元/笔；价差 普通ETF 2tick、QDII 3tick、511880 零成本
  - 分红处理：统一按前复权价格口径（同资产再投资），不单独建模现金流

两个版本：
  steady     稳健版: 红利低波20% + 标普500 10% + 十年国债35% + 黄金20% + 豆粕5% + 货币10%
  aggressive 进取版: 红利低波30% + 标普500 15% + 十年国债25% + 黄金20% + 豆粕5% + 货币5%

红利低波数据：2023-12-14前用全收益指数重建（H20269缩放接到563020前复权价）
"""
import os
import math
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

INITIAL_CAPITAL = 1_000_000
START_DATE = "2020-01-01"
LISTING_DATE = pd.Timestamp("2023-12-14")
COMMISSION_RATE = 0.00015
MIN_COMMISSION = 5.0
REBAL_BAND = 0.05          # 偏离±5个百分点触发
CASH_LEG = "cash"

# (代码, 市场类型)  QDII 3tick，其余2tick，511880零成本
LEGS = {
    "hlb":     {"name": "红利低波", "spread": 0.002},
    "sp500":   {"name": "标普500",  "spread": 0.003},
    "bond10":  {"name": "十年国债", "spread": 0.002},
    "gold":    {"name": "黄金",     "spread": 0.002},
    "soybean": {"name": "豆粕",     "spread": 0.002},
    "cash":    {"name": "货币ETF",  "spread": 0.0},
}

VARIANTS = {
    "steady":     {"hlb": 0.20, "sp500": 0.10, "bond10": 0.35, "gold": 0.20, "soybean": 0.05, "cash": 0.10},
    "aggressive": {"hlb": 0.30, "sp500": 0.15, "bond10": 0.25, "gold": 0.20, "soybean": 0.05, "cash": 0.05},
}


def load_panel() -> pd.DataFrame:
    """构建统一价格面板（前复权口径），索引=交易日"""
    # 红利低波：重建段(全收益指数) + 真实段(563020 qfq)
    tr = pd.read_csv(os.path.join(DATA, "h20269.csv"), parse_dates=["date"])
    qfq = pd.read_csv(os.path.join(DATA, "563020_qfq.csv"), parse_dates=["date"])
    tr = tr.set_index("date")["close"]
    qfq_s = qfq.set_index("date")["close"]
    tr_listing = tr[LISTING_DATE]
    qfq_listing = qfq_s[LISTING_DATE]
    hlb_recon = qfq_listing * tr / tr_listing
    hlb = pd.concat([hlb_recon[hlb_recon.index < LISTING_DATE], qfq_s])
    hlb = hlb[~hlb.index.duplicated(keep="last")].sort_index()

    panel = {"hlb": hlb}
    for leg, code in [("sp500", "513500"), ("bond10", "511260"), ("gold", "518880"),
                      ("soybean", "159985"), ("cash", "511880"), ("hs300", "510300")]:
        df = pd.read_csv(os.path.join(DATA, f"{code}.csv"), parse_dates=["date"])
        panel[leg] = df.set_index("date")["close"]

    idx = hlb.index  # 用红利腿的交易日历（指数交易日）
    out = pd.DataFrame(index=idx)
    for leg, s in panel.items():
        out[leg] = s.reindex(idx).ffill()
    return out.dropna(subset=["hlb", "sp500", "bond10", "gold", "soybean", "cash"])


def commission(amount: float) -> float:
    return max(amount * COMMISSION_RATE, MIN_COMMISSION)


def buy_shares(leg: str, price: float, budget: float, cash_avail: float):
    """按100份整数买入，返回(份额, 含费总支出)。预算或现金不足返回None"""
    if LEGS[leg]["spread"] == 0:
        px = price
    else:
        px = price + LEGS[leg]["spread"]
    lots = int(min(budget, cash_avail) / (px * 100))
    while lots > 0:
        sh = lots * 100
        cost = sh * px
        fee = 0 if leg == CASH_LEG else commission(cost)
        if cost + fee <= cash_avail:
            return sh, cost + fee
        lots -= 1
    return None, 0


def sell_shares(leg: str, price: float, shares: int):
    """卖出，返回(净收入, 费用)"""
    if LEGS[leg]["spread"] == 0:
        px = price
    else:
        px = price - LEGS[leg]["spread"]
    amount = shares * px
    fee = 0 if leg == CASH_LEG else commission(amount)
    return amount - fee, fee


def run_backtest(panel: pd.DataFrame, weights: dict, label: str):
    dates = panel.index[panel.index >= pd.Timestamp(START_DATE)]
    holdings = {leg: 0 for leg in weights}
    cash = INITIAL_CAPITAL
    total_fees = 0.0
    rebal_log = []

    def assets_on(date):
        return cash + sum(holdings[leg] * panel.loc[date, leg] for leg in holdings)

    def rebalance(date, reason):
        nonlocal cash, total_fees
        total = assets_on(date)
        prices = panel.loc[date]
        fees = 0.0
        # 先卖：所有资产减到目标值以下
        for leg, w in weights.items():
            if leg == CASH_LEG:
                continue
            cur_val = holdings[leg] * prices[leg]
            tgt_val = total * w
            if cur_val > tgt_val:
                sell_val = cur_val - tgt_val
                sh = int(sell_val / prices[leg] / 100) * 100
                if sh > 0 and sh <= holdings[leg]:
                    proceeds, fee = sell_shares(leg, prices[leg], sh)
                    holdings[leg] -= sh
                    cash += proceeds
                    fees += fee
        # 现金腿超配（511880市值高于目标）同样在卖出阶段减仓，回笼资金供后买
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
        # 后买：按目标补齐（现金腿最后兜底）
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
        # 现金腿：用闲置现金把511880补到目标市值（低配补买；超配已在卖出阶段减仓）
        if CASH_LEG in weights:
            tgt_cash = total * weights[CASH_LEG]
            etf_val = holdings[CASH_LEG] * prices[CASH_LEG]
            if etf_val < tgt_cash and cash > 0:
                sh, spent = buy_shares(CASH_LEG, prices[CASH_LEG], tgt_cash - etf_val, cash)
                if sh:
                    holdings[CASH_LEG] += sh
                    cash -= spent
        total_fees += fees
        rebal_log.append({"date": date, "reason": reason, "fees": round(fees, 2)})

    # 期初建仓（视作一次再平衡）
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
            total = assets_on(date)
            prices = panel.loc[date]
            def _leg_val(leg):
                # 现金腿权重 = 闲置现金 + 511880持仓市值
                if leg == CASH_LEG:
                    return cash + holdings[leg] * prices[leg]
                return holdings[leg] * prices[leg]
            dev = max(abs(_leg_val(leg) / total - w)
                      for leg, w in weights.items())
            if dev >= REBAL_BAND:
                rebalance(date, f"偏离{dev*100:.1f}pp")
        rows.append({"date": date, "assets": assets_on(date)})
    eq = pd.DataFrame(rows)
    eq["nav"] = eq["assets"] / INITIAL_CAPITAL
    return eq, pd.DataFrame(rebal_log), total_fees


def metrics(eq: pd.DataFrame) -> dict:
    nav = eq["nav"]
    dd = nav / nav.cummax() - 1
    i = dd.idxmin()
    ret = nav.pct_change().dropna()
    years = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    total_ret = nav.iloc[-1] / nav.iloc[0] - 1
    ann = (1 + total_ret) ** (1 / years) - 1
    sharpe = ret.mean() / ret.std() * math.sqrt(252) if ret.std() > 0 else 0
    return {"total": total_ret * 100, "ann": ann * 100,
            "mdd": dd.min() * 100, "mdd_date": eq.loc[i, "date"],
            "sharpe": sharpe, "calmar": ann / abs(dd.min()) if dd.min() != 0 else 0,
            "vol": ret.std() * math.sqrt(252) * 100}


def yearly(eq: pd.DataFrame) -> dict:
    eq = eq.copy()
    eq["year"] = eq["date"].dt.year
    out = {}
    prev = 1.0
    for y, g in eq.groupby("year"):
        r = (g["nav"].iloc[-1] / g["nav"].iloc[0] - 1) * 100 if y == eq["year"].min() else (g["nav"].iloc[-1] / prev - 1) * 100
        out[y] = r
        prev = g["nav"].iloc[-1]
    return out


def bench_6040(panel: pd.DataFrame) -> pd.DataFrame:
    """60%沪深300+40%五年国债，季度再平衡"""
    w = {"hs300": 0.60, "bond5": 0.40}
    legs = {"hs300": {"name": "沪深300", "spread": 0.002},
            "bond5": {"name": "五年国债", "spread": 0.002}}
    global LEGS
    old = LEGS
    LEGS = legs
    eq, _, _ = run_backtest(panel.rename(columns={"511010": "bond5"}).assign(
        bond5=pd.read_csv(os.path.join(DATA, "511010.csv"), parse_dates=["date"]).set_index("date")["close"]),
        w, "6040")
    LEGS = old
    return eq


def main():
    print("加载价格面板 ...")
    panel = load_panel()
    panel = panel[panel.index >= pd.Timestamp(START_DATE)]
    print(f"  {panel.index[0].date()} ~ {panel.index[-1].date()}, {len(panel)} 个交易日")

    results = {}
    for label, w in VARIANTS.items():
        eq, log, fees = run_backtest(panel, w, label)
        m = metrics(eq)
        y = yearly(eq)
        results[label] = (eq, m, y, log, fees)
        eq.to_csv(os.path.join(OUT, f"nav_{label}.csv"), index=False, encoding="utf-8-sig")
        log.to_csv(os.path.join(OUT, f"rebalance_{label}.csv"), index=False, encoding="utf-8-sig")

    # 基准
    hs300_eq = panel[["hs300"]].dropna().reset_index()
    hs300_eq["assets"] = hs300_eq["hs300"] / hs300_eq["hs300"].iloc[0] * INITIAL_CAPITAL
    hs300_eq["nav"] = hs300_eq["assets"] / INITIAL_CAPITAL
    m300 = metrics(hs300_eq[["date", "assets", "nav"]])

    print("\n" + "=" * 74)
    print("全天候再平衡策略回测（2020-01 ~ 2026-07，100万，阈值±5pp）")
    print("=" * 74)
    hdr = f"{'版本':<12}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'再平衡':>6}{'费用':>8}"
    print(hdr)
    for label, (eq, m, y, log, fees) in results.items():
        cn = "稳健版" if label == "steady" else "进取版"
        print(f"{cn:<12}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%{m['sharpe']:>7.2f}"
              f"{m['calmar']:>8.2f}{len(log):>6}{fees:>8,.0f}")
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
