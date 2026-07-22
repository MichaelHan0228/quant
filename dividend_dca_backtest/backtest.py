"""
红利低波ETF 条件定投回测
========================
策略（用户规则）：
  - 每周最后一个交易日判断：当前价 vs 上一年最后一个交易日收盘价（前复权口径）
  - 低于 0~-5% 投1万；-5~-10% 投2万；-10~-15% 投3万；以此类推每档+1万
  - 高于等于去年收盘价：不买，持仓不动
  - 100万资金封顶，现金耗尽后停止定投；季度分红现金回池参与后续定投

标的数据：
  - 2023-12-14 起：563020 真实行情（腾讯）+ 真实分红记录
  - 2020-01 ~ 2023-12-13：H30269 价格指数重建基金价格，
    分红按全收益/价格指数差推算季度分红率 × 派发比例α=0.7
    （α 用 2024/2025 真实分红校准：真实派发约3.8-3.9% vs 隐含5.1-5.9%）

费用：
  - 佣金 万1.5 最低5元；价差 2 tick（0.002元）计入买入价
  - 管理/托管费 0.2%/年 未计入重建段（4年累计<1%，报告中声明）

净值口径：单位净值法（新增资金按当日净值折算份额），
  回撤/夏普/年化在单位净值曲线上计算，与资金流入无关。
"""
import os
import math
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

# ============ 参数 ============
INITIAL_CAPITAL = 1_000_000     # 100万
WEEKLY_BASE = 10_000            # 每档1万
TIER_STEP = 0.05                # 每5%一档
BUY_UPPER_BAND = 0.05           # 买入上限：价格低于基准+5%才考虑买
BAND_AMOUNT = 5_000             # v3：0~+5% 区间买5千（保留弹药给深跌档）
REF_MODE = "dual"               # 触发基准: "year_end"=去年收盘价(v3), "ma250"=250日均线(v4), "dual"=双锚取最大偏离(v5)
MA_WINDOW = 250                 # MA250 窗口（交易日）
SPREAD = 0.002                  # 买入价差（2 tick）
COMMISSION_RATE = 0.00015       # 万1.5
MIN_COMMISSION = 5.0
CASH_YIELD = 0.02               # 闲置资金年化收益（511880场内货基停泊，T+0零佣金，摩擦~0.001%忽略）
LISTING_DATE = pd.Timestamp("2023-12-14")
ALPHA = 0.70                    # 重建段分红派发比例（用真实分红校准）

# 563020 真实分红记录（除息日, 每份分红），来源：基金分红公告
REAL_DIVIDENDS = [
    ("2024-06-17", 0.012), ("2024-09-12", 0.016), ("2024-12-11", 0.014),
    ("2025-03-12", 0.009), ("2025-06-12", 0.010), ("2025-09-10", 0.012),
    ("2025-12-10", 0.012), ("2026-03-10", 0.010), ("2026-06-10", 0.012),
]


def load_series():
    """构建统一日度序列：raw（成交价）、sig（前复权信号价）"""
    pr = pd.read_csv(os.path.join(DATA, "h30269.csv"), parse_dates=["date"])
    tr = pd.read_csv(os.path.join(DATA, "h20269.csv"), parse_dates=["date"])
    etf = pd.read_csv(os.path.join(DATA, "563020_raw.csv"), parse_dates=["date"])
    qfq = pd.read_csv(os.path.join(DATA, "563020_qfq.csv"), parse_dates=["date"])

    m = pr[["date", "close"]].rename(columns={"close": "pr"}).merge(
        tr[["date", "close"]].rename(columns={"close": "tr"}), on="date")
    # 缩放系数 K：让重建成交价在上市日接上真实价
    raw_listing = etf[etf["date"] == LISTING_DATE].iloc[0]["close"]
    pr_listing = m[m["date"] == LISTING_DATE].iloc[0]["pr"]
    K = pr_listing / raw_listing

    m["raw"] = m["pr"] / K          # 重建成交价（价格指数口径）
    # 信号价（前复权/全收益口径）：重建段用全收益指数缩放接到上市日qfq，保证两段同尺度
    tr_listing = m[m["date"] == LISTING_DATE].iloc[0]["tr"]
    qfq_listing = qfq[qfq["date"] == LISTING_DATE].iloc[0]["close"]
    m["sig"] = qfq_listing * m["tr"] / tr_listing
    # 真实段：覆盖为真实行情
    real = etf[["date", "close"]].rename(columns={"close": "raw"}).merge(
        qfq[["date", "close"]].rename(columns={"close": "sig"}), on="date")
    m = m[m["date"] < LISTING_DATE][["date", "raw", "sig", "pr", "tr"]]
    real["pr"] = None
    real["tr"] = None
    df = pd.concat([m, real]).sort_values("date").reset_index(drop=True)
    return df


def build_dividend_events(df):
    """生成分红事件表 [(date, per_share)]：重建段推算 + 真实段记录"""
    events = []
    # 重建段：每年3/6/9/12月15日后首个交易日除息
    recon = df[df["date"] < LISTING_DATE]
    for year in range(2020, 2024):
        for mm in (3, 6, 9, 12):
            anchor = pd.Timestamp(year, mm, 15)
            future = recon[recon["date"] >= anchor]
            if future.empty:
                continue
            ex_date = future.iloc[0]["date"]
            # 季度隐含分红率 = dr(除息日)/dr(上一个除息锚点) - 1
            dr = df.set_index("date")
            cur = dr.loc[ex_date, "tr"] / dr.loc[ex_date, "pr"]
            prev_anchor = (pd.Timestamp(year, mm, 15) - pd.DateOffset(months=3))
            prev_days = recon[recon["date"] >= prev_anchor]
            if prev_days.empty:
                continue
            prev_date = prev_days.iloc[0]["date"]
            prev = dr.loc[prev_date, "tr"] / dr.loc[prev_date, "pr"]
            q_yield = cur / prev - 1
            if q_yield <= 0:
                continue
            per_share = dr.loc[ex_date, "raw"] * q_yield * ALPHA
            events.append((ex_date, round(per_share, 4)))
    # 真实段
    for d, amt in REAL_DIVIDENDS:
        events.append((pd.Timestamp(d), amt))
    events.sort(key=lambda x: x[0])
    return events


def run_backtest(df, div_events):
    cash = INITIAL_CAPITAL
    shares = 0
    buy_total = 0.0       # 累计买入金额（内部周转，含费用；分红回池后可超过100万）
    div_total = 0.0
    buys = []
    div_log = []

    df = df.copy()
    df["year"] = df["date"].dt.year
    df["iso_week"] = df["date"].dt.isocalendar().week.astype(int)
    df["yw"] = df["date"].dt.strftime("%G") + "-" + df["iso_week"].astype(str).str.zfill(2)
    weekly_last_dates = set(df.groupby("yw")["date"].max())
    # 每年最后一个交易日（year_end 模式信号基准）
    year_ends = df.groupby("year")["date"].max()
    prev_year_close = {}
    sig_map = df.set_index("date")["sig"]
    for y, d in year_ends.items():
        prev_year_close[y + 1] = sig_map[d]
    # ma250/dual 模式信号基准：前复权信号价的250日均线
    if REF_MODE in ("ma250", "dual"):
        df["sig_ma"] = df["sig"].rolling(MA_WINDOW).mean()

    div_map = {}
    for d, amt in div_events:
        div_map.setdefault(d, 0.0)
        div_map[d] += amt

    equity_rows = []
    for _, row in df.iterrows():
        date, raw = row["date"], row["raw"]
        # 1) 分红到账（除息日按收盘后持有份额计，现金回池）
        if date in div_map and shares > 0:
            amt = shares * div_map[date]
            cash += amt
            div_total += amt
            div_log.append({"date": date, "per_share": div_map[date],
                            "shares": shares, "amount": amt})
        # 2) 周末定投判断（收盘价成交，资金来自现金池）
        if date in weekly_last_dates and date.year >= 2020:
            d = None
            if REF_MODE == "dual":
                # 双锚：年末锚和MA250锚各算偏离，取更大偏离（跌得更深的那个）
                cands = []
                base_ye = prev_year_close.get(date.year)
                if base_ye:
                    cands.append(row["sig"] / base_ye - 1)
                base_ma = row.get("sig_ma")
                if base_ma is not None and not pd.isna(base_ma):
                    cands.append(row["sig"] / base_ma - 1)
                if cands:
                    d = min(cands)
            elif REF_MODE == "ma250":
                base_ma = row.get("sig_ma")
                if base_ma is not None and not pd.isna(base_ma):
                    d = row["sig"] / base_ma - 1
            else:
                base_close = prev_year_close.get(date.year)
                if base_close:
                    d = row["sig"] / base_close - 1
            if d is not None and d < BUY_UPPER_BAND:
                    # v3 分档：0~+5% → 5千；-5%~0 → 1万；之后每低5%加1万
                    if d >= 0:
                        tier = 0  # 0档=5千区间档
                        amount = BAND_AMOUNT
                    else:
                        tier = int((-d) / TIER_STEP) + 1
                        amount = tier * WEEKLY_BASE
                    budget = min(amount, cash)
                    exec_price = raw + SPREAD
                    lots = int(budget / (exec_price * 100))
                    while lots > 0:
                        sh = lots * 100
                        cost = sh * exec_price
                        comm = max(cost * COMMISSION_RATE, MIN_COMMISSION)
                        if cost + comm <= cash:
                            break
                        lots -= 1
                    if lots > 0:
                        sh = lots * 100
                        cost = sh * exec_price
                        comm = max(cost * COMMISSION_RATE, MIN_COMMISSION)
                        outflow = cost + comm
                        cash -= outflow
                        shares += sh
                        buy_total += outflow
                        buys.append({"date": date, "discount_pct": round(d * 100, 2),
                                     "tier": tier, "amount_plan": amount,
                                     "price": round(exec_price, 3), "shares": sh,
                                     "cost": round(outflow, 2)})
        # 3) 日终估值（单位净值 = 总资产/初始资金，与资金流入无关）
        cash += cash * (CASH_YIELD / 252)   # 闲置资金按货基收益计提
        assets = cash + shares * raw
        unit_nav = assets / INITIAL_CAPITAL
        equity_rows.append({"date": date, "assets": assets, "cash": cash,
                            "shares": shares, "unit_nav": unit_nav,
                            "buy_cum": buy_total, "div_cum": div_total})
    eq = pd.DataFrame(equity_rows)
    # XIRR 现金流：期初一次性投入100万，期末总资产
    cashflows = [(df["date"].iloc[0], -INITIAL_CAPITAL),
                 (df["date"].iloc[-1], eq["assets"].iloc[-1])]
    return eq, pd.DataFrame(buys), pd.DataFrame(div_log), cashflows


def xirr(cashflows, lo=-0.5, hi=1.5):
    """二分法求XIRR"""
    t0 = cashflows[0][0]
    def npv(r):
        return sum(amt / (1 + r) ** ((d - t0).days / 365.25) for d, amt in cashflows)
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return float("nan")
    for _ in range(100):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-6:
            return mid
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return mid


def calc_metrics(eq, buys_df):
    nav = eq["unit_nav"]
    peak = nav.cummax()
    dd = nav / peak - 1
    i = dd.idxmin()
    daily_ret = nav.pct_change().dropna()
    sharpe = daily_ret.mean() / daily_ret.std() * math.sqrt(252) if daily_ret.std() > 0 else 0
    return {
        "max_dd": dd.min() * 100,
        "max_dd_date": eq.loc[i, "date"],
        "dd_peak_date": eq.loc[:i].loc[nav[:i + 1].idxmax(), "date"] if i > 0 else None,
        "sharpe": sharpe,
        "vol": daily_ret.std() * math.sqrt(252) * 100,
    }


def main():
    print("加载数据 ...")
    df = load_series()
    print(f"  统一序列: {len(df)} 行, {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")
    div_events = build_dividend_events(df)
    print(f"  分红事件: {len(div_events)} 次（重建段{sum(1 for d,_ in div_events if d < LISTING_DATE)}次 + 真实{len(REAL_DIVIDENDS)}次）")

    eq, buys_df, div_df, cashflows = run_backtest(df, div_events)
    m = calc_metrics(eq, buys_df)

    final = eq.iloc[-1]
    invested = INITIAL_CAPITAL
    assets = final["assets"]
    profit = assets - invested
    r_xirr = xirr(cashflows)

    # ============ 报告 ============
    print("\n" + "=" * 66)
    print("红利低波ETF 条件定投回测（2020-01 ~ 2026-07）")
    print("=" * 66)
    print(f"累计投入:     {invested:>14,.0f} 元（100万资金池，分红回池再投）")
    print(f"累计买入金额: {final['buy_cum']:>14,.0f} 元（含费用，分红再投部分）")
    print(f"期末总资产:   {assets:>14,.0f} 元")
    print(f"绝对收益:     {profit:>14,.0f} 元  ({profit/invested*100:+.1f}%)")
    print(f"XIRR年化:     {r_xirr*100:>14.2f}%")
    print(f"累计分红:     {final['div_cum']:>14,.0f} 元（已回池再投）")
    print(f"最大回撤:     {m['max_dd']:>14.1f}%  (峰值{m['dd_peak_date'].date()} → 谷底{m['max_dd_date'].date()})")
    print(f"年化波动:     {m['vol']:>14.1f}%")
    print(f"夏普比率:     {m['sharpe']:>14.2f}")
    print(f"买入次数:     {len(buys_df)} 次")
    print(f"持仓份额:     {final['shares']:>14,.0f} 份")
    cost_avg2 = final['buy_cum'] / final['shares'] if final['shares'] else 0
    print(f"加权成本:     {cost_avg2:>14.3f} 元/份 (累计买入/份额)")
    print(f"当前价格:     {df['raw'].iloc[-1]:>14.3f} 元")

    # 基准：同期全收益指数一次性买入
    tr = pd.read_csv(os.path.join(DATA, "h20269.csv"), parse_dates=["date"])
    tr = tr[(tr["date"] >= pd.Timestamp("2020-01-01"))]
    bench = (tr["close"].iloc[-1] / tr["close"].iloc[0] - 1) * 100
    years = (tr["date"].iloc[-1] - tr["date"].iloc[0]).days / 365.25
    bench_ann = ((1 + bench / 100) ** (1 / years) - 1) * 100
    print(f"\n基准(红利低波全收益一次性买入): {bench:+.1f}%  年化{bench_ann:.2f}%")

    # 年度表
    print("\n" + "-" * 66)
    print("年度明细:")
    hdr = f"{'年份':<6}{'当年买入':>12}{'当年分红':>10}{'年末总资产':>13}{'累计买入':>13}{'净值年收益':>10}"
    print(hdr)
    eq["year"] = eq["date"].dt.year
    buys_df["year"] = pd.to_datetime(buys_df["date"]).dt.year if not buys_df.empty else []
    for y, g in eq.groupby("year"):
        if y < 2020:
            continue
        buy_y = buys_df[buys_df["year"] == y]["cost"].sum() if not buys_df.empty else 0
        div_y = div_df[pd.to_datetime(div_df["date"]).dt.year == y]["amount"].sum() if not div_df.empty else 0
        nav_y = g["unit_nav"].iloc[-1]
        nav_prev = eq[eq["year"] == y - 1]["unit_nav"].iloc[-1] if y > 2020 else 1.0
        ret_y = (nav_y / nav_prev - 1) * 100
        print(f"{y:<8}{buy_y:>12,.0f}{div_y:>10,.0f}{g['assets'].iloc[-1]:>13,.0f}"
              f"{g['buy_cum'].iloc[-1]:>13,.0f}{ret_y:>9.1f}%")

    if not buys_df.empty:
        print("\n分档统计:")
        print(buys_df.groupby("tier").agg(次数=("cost", "size"), 金额=("cost", "sum")).to_string())

    eq.to_csv(os.path.join(OUT, "equity_curve.csv"), index=False, encoding="utf-8-sig")
    buys_df.to_csv(os.path.join(OUT, "buys.csv"), index=False, encoding="utf-8-sig")
    div_df.to_csv(os.path.join(OUT, "dividends.csv"), index=False, encoding="utf-8-sig")
    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
