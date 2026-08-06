"""
可转债双低周频轮动回测（2018 起）
====================================
规则（经典双低 + 信用过滤 + 强赎保护）：
  - 每周最后一个交易日，对全市场转债计算 双低值 = 价格 + 转股溢价率×100
  - 过滤：上市满10日、距退市>10日、价格 ≤130（强赎区不买）、评级 ≥AA−
  - 持仓：双低值最低前 15 只，等权；掉出前 20 名或价格 >130 → 轮出
  - 费用：佣金万1（无最低）+ 每边 0.1% 滑点；T+0 品种，周五收盘价成交
  - 转股价重建：初始转股价 −分红/送转调整；下修残差按巨潮公告日记入
基准：中证转债指数 000832（csindex）
"""
import os
import math

import numpy as np
import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "output")
os.makedirs(OUT, exist_ok=True)

UA = "Mozilla/5.0"
START = "2018-01-01"
TOP_N = 15                # 持仓只数
HOLD_BUFFER = 20          # 掉出前20才卖（滞回带）
MAX_PRICE = 130.0         # 强赎区上限
MIN_RATING = {"AAA": 5, "AA+": 4, "AA": 3, "AA-": 2, "A+": 1, "A": 0}
RATING_FLOOR = 2          # ≥AA−
COMM = 0.0001
SLIP = 0.001


def rating_ok(r) -> bool:
    if not isinstance(r, str) or not r.strip():
        return False
    r = r.strip().upper()
    return MIN_RATING.get(r, -1) >= RATING_FLOOR


# ── 数据集构建 ────────────────────────────────────────────────

def build_tp_series(cb_row, div_df, rev_dates, stock_px):
    """单只转债的转股价时间序列（date, tp）
    口径：初始转股价 → 除息日 −每股税前派息 → 送转日 ÷(1+每10股送转/10)
         → 下修公告日：tp = max(前20日均价, 前一日收盘价)（下修底线，多数公司修到底；
           无"最终转股价"锚可用（东财该字段全空），此为规则近似，文档已标注）"""
    init = pd.to_numeric(cb_row["INITIAL_TRANSFER_PRICE"], errors="coerce")
    if pd.isna(init) or init <= 0:
        return None
    # 上市前的分红/下修与转股价无关（初始转股价已反映发行时点状态），只取上市后事件
    listing = pd.to_datetime(cb_row.get("LISTING_DATE"), errors="coerce")
    events = []   # (date, kind, payload)
    if div_df is not None and not div_df.empty:
        for _, d in div_df.iterrows():
            dt = pd.to_datetime(d.get("EX_DIVIDEND_DATE"), errors="coerce")
            if pd.isna(dt) or (pd.notna(listing) and dt < listing):
                continue
            cash_div = pd.to_numeric(d.get("PRETAX_BONUS_RMB"), errors="coerce")
            cash_div = 0 if pd.isna(cash_div) else cash_div
            stock_div = (pd.to_numeric(d.get("TRANSFER_RATIO"), errors="coerce") or 0) + \
                        (pd.to_numeric(d.get("BONUS_RATIO"), errors="coerce") or 0)
            stock_div = 0 if pd.isna(stock_div) else stock_div
            if cash_div or stock_div:
                events.append((dt, "div", cash_div, stock_div / 10.0))
    for dt in (rev_dates or []):
        dt = pd.to_datetime(dt, errors="coerce")
        if pd.notna(dt) and not (pd.notna(listing) and dt < listing):
            events.append((dt, "rev", 0, 0))
    events.sort(key=lambda x: x[0])

    tp = init
    segs = [(pd.Timestamp("1900-01-01"), tp)]
    for dt, kind, cash_div, stock_ratio in events:
        if kind == "div":
            tp = tp - cash_div
            if stock_ratio > 0:
                tp = tp / (1 + stock_ratio)
        else:  # 下修：tp = max(前20日均价, 前日收盘)，不能超过原值
            if stock_px is not None and len(stock_px) >= 5:
                hist = stock_px[stock_px.index < dt]
                if len(hist) >= 5:
                    floor = max(hist.iloc[-20:].mean(), hist.iloc[-1])
                    tp = min(tp, floor)
        if tp > 0:
            segs.append((dt, round(tp, 4)))
    s = pd.Series({d: v for d, v in segs if v}).sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_dataset():
    cb = pd.read_csv(os.path.join(DATA, "cb_list.csv"),
                     dtype={"SECURITY_CODE": str, "CONVERT_STOCK_CODE": str})
    rev_path = os.path.join(DATA, "downward_revisions.csv")
    rev = pd.read_csv(rev_path, dtype={"stock": str}) if os.path.exists(rev_path) \
        else pd.DataFrame(columns=["stock", "date"])
    rev_map = rev.groupby("stock")["date"].apply(list).to_dict()   # 下修公告按正股归集

    # 先载正股价格（下修重建要用）
    stocks = {}
    for b in cb["CONVERT_STOCK_CODE"].dropna().unique():
        sp = os.path.join(DATA, "stock_daily", f"{b}.csv")
        if os.path.exists(sp):
            stocks[b] = pd.read_csv(sp, parse_dates=["date"]).set_index("date")["close"].dropna()

    bonds = {}
    for _, row in cb.iterrows():
        code = row["SECURITY_CODE"]
        path = os.path.join(DATA, "cb_daily", f"{code}.csv")
        stock = row["CONVERT_STOCK_CODE"] if isinstance(row["CONVERT_STOCK_CODE"], str) else ""
        if not os.path.exists(path) or stock not in stocks:
            continue
        px = pd.read_csv(path, parse_dates=["date"]).set_index("date")["close"].dropna()
        if px.empty:
            continue
        div_path = os.path.join(DATA, "dividends", f"{stock}.csv")
        div_df = pd.read_csv(div_path) if os.path.exists(div_path) else pd.DataFrame()
        tp = build_tp_series(row, div_df, rev_map.get(stock), stocks[stock])
        bonds[code] = {
            "name": row["SECURITY_NAME_ABBR"], "stock": stock,
            "listing": pd.to_datetime(row["LISTING_DATE"]),
            "delist": pd.to_datetime(row["DELIST_DATE"]) if pd.notna(row["DELIST_DATE"]) else pd.NaT,
            "rating": row.get("RATING"), "px": px, "tp": tp,
        }
    print(f"可用转债: {len(bonds)} 只, 正股 {len(stocks)} 只")
    return bonds, stocks


def build_panel(bonds, stocks):
    """构建周频面板：每周五的 转债价格/转股溢价率/双低值"""
    all_dates = sorted({d for b in bonds.values() for d in b["px"].index
                        if d >= pd.Timestamp("2017-12-01")})
    weeks = pd.Series(all_dates).groupby(pd.Series(all_dates).dt.to_period("W-FRI")).max().tolist()
    panel = {}
    for code, b in bonds.items():
        spx = stocks[b["stock"]]
        rows = []
        for w in weeks:
            cb_px = b["px"][b["px"].index <= w]
            st_px = spx[spx.index <= w]
            if cb_px.empty or st_px.empty:
                continue
            tp = None
            if b["tp"] is not None:
                tp_s = b["tp"][b["tp"].index <= w]
                if not tp_s.empty:
                    tp = tp_s.iloc[-1]
            price = cb_px.iloc[-1]
            stock_p = st_px.iloc[-1]
            prem = (price / (100 * stock_p / tp) - 1) if tp and stock_p > 0 else np.nan
            rows.append({"date": w, "price": price, "prem": prem,
                         "dl": price + prem * 100 if not np.isnan(prem) else np.nan})
        panel[code] = pd.DataFrame(rows).set_index("date")
    return panel, weeks


def fetch_csi_cb_index():
    """中证转债 000832（基准）"""
    cache = os.path.join(DATA, "csi_cb_000832.csv")
    if os.path.exists(cache):
        return pd.read_csv(cache, parse_dates=["date"]).set_index("date")["close"]
    r = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                     params={"indexCode": "000832", "startDate": "20170101",
                             "endDate": pd.Timestamp.today().strftime("%Y%m%d")},
                     headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"},
                     timeout=120)
    rows = r.json().get("data") or []
    df = pd.DataFrame(rows)
    out = df[["tradeDate", "close"]].rename(columns={"tradeDate": "date"})
    out["date"] = pd.to_datetime(out["date"], format="%Y%m%d")
    out = out.dropna().drop_duplicates("date").sort_values("date")
    out.to_csv(cache, index=False)
    return out.set_index("date")["close"]


# ── 回测引擎 ─────────────────────────────────────────────────

def run_backtest(bonds, panel, weeks, cfg: dict | None = None):
    """cfg: top_n(持仓数), max_price(买入价格上限), rating_on(评级过滤开关)"""
    cfg = {"top_n": TOP_N, "max_price": MAX_PRICE, "rating_on": True, **(cfg or {})}
    top_n = cfg["top_n"]
    hold_buffer = top_n + 5
    cash = 1.0                     # 净值口径：起始 1 元
    positions = {}
    nav = 1.0
    curve, trades = [], []
    fee_total = 0.0

    for w in weeks:
        if w < pd.Timestamp(START):
            continue
        # 本周价格表
        prices, dls, metas = {}, {}, {}
        for code, df in panel.items():
            if w not in df.index:
                continue
            r = df.loc[w]
            b = bonds[code]
            if pd.isna(r["price"]) or r["price"] <= 0:
                continue
            prices[code] = r["price"]
            dls[code] = r["dl"]
            metas[code] = b
        if not prices:
            curve.append({"date": w, "nav": nav})
            continue
        # 当前净值（按本周价格重估）
        nav = cash + sum(sh * prices.get(c, 0) for c, sh in positions.items())

        # ── 卖出：掉出缓冲带 / 价格超限 / 临近退市 ──
        eligible = {}
        for code, dl in dls.items():
            b = metas[code]
            if pd.isna(dl):
                continue
            if w < b["listing"] + pd.Timedelta(days=10):
                continue
            if pd.notna(b["delist"]) and w > b["delist"] - pd.Timedelta(days=10):
                continue
            if prices[code] > cfg["max_price"]:
                continue
            if cfg["rating_on"] and not rating_ok(b["rating"]):
                continue
            eligible[code] = dl
        ranked = sorted(eligible, key=eligible.get)
        buffer_set = set(ranked[:hold_buffer])

        for code in list(positions):
            sell_reason = None
            if code not in prices:
                sell_reason = "无价格"
            elif code not in buffer_set:
                sell_reason = f"掉出前{hold_buffer}"
            elif prices[code] > cfg["max_price"]:
                sell_reason = "强赎区"
            if sell_reason:
                proceeds = positions[code] * prices[code] * (1 - SLIP)
                fee = proceeds * COMM
                cash += proceeds - fee
                fee_total += fee
                trades.append({"date": w, "code": code, "side": "卖",
                               "price": prices[code], "reason": sell_reason})
                del positions[code]

        # ── 买入：补满 top_n，等权 ──
        nav = cash + sum(sh * prices.get(c, 0) for c, sh in positions.items())
        slots = top_n - len(positions)
        if slots > 0 and cash > 0.001:
            tgt_each = nav / top_n
            for code in ranked:
                if len(positions) >= top_n:
                    break
                if code in positions:
                    continue
                cur_val = positions.get(code, 0) * prices.get(code, 0)
                need = tgt_each - cur_val
                if need <= nav * 0.01 or need > cash:
                    continue
                cost = need * (1 + SLIP)
                fee = cost * COMM
                if cost + fee > cash:
                    cost = cash / (1 + SLIP + COMM)
                    fee = cost * COMM
                    need = cost
                positions[code] = need / prices[code]   # 份额=市值/价格
                cash -= cost + fee
                fee_total += fee
                trades.append({"date": w, "code": code, "side": "买",
                               "price": prices[code], "reason": "入榜"})
        nav = cash + sum(sh * prices.get(c, 0) for c, sh in positions.items())
        curve.append({"date": w, "nav": nav, "n_pos": len(positions)})
    return pd.DataFrame(curve), pd.DataFrame(trades), fee_total


def metrics_df(curve, freq=52):
    nav = curve["nav"]
    dd = nav / nav.cummax() - 1
    ret = nav.pct_change().dropna()
    years = (curve["date"].iloc[-1] - curve["date"].iloc[0]).days / 365.25
    total = nav.iloc[-1] - 1
    ann = (1 + total) ** (1 / years) - 1
    sharpe = ret.mean() / ret.std() * math.sqrt(freq) if ret.std() > 0 else 0
    return {"total": total * 100, "ann": ann * 100, "mdd": dd.min() * 100,
            "sharpe": sharpe, "calmar": ann / abs(dd.min()) if dd.min() else 0}


def main():
    print("构建数据集 ...")
    bonds, stocks = load_dataset()
    panel, weeks = build_panel(bonds, stocks)
    print(f"周频面板: {len(weeks)} 周")

    curve, trades, fees = run_backtest(bonds, panel, weeks)
    m = metrics_df(curve)

    idx = fetch_csi_cb_index()
    idx_w = idx.resample("W-FRI").last().dropna()
    idx_w = idx_w[(idx_w.index >= curve["date"].iloc[0])]
    idx_nav = idx_w / idx_w.iloc[0]
    bm = metrics_df(pd.DataFrame({"date": idx_nav.index, "nav": idx_nav.values}))

    print("\n" + "=" * 74)
    print(f"可转债双低周频轮动（{curve['date'].iloc[0].date()} ~ {curve['date'].iloc[-1].date()}）")
    print("=" * 74)
    print(f"{'':<12}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}")
    print(f"{'双低轮动':<12}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%{m['sharpe']:>7.2f}{m['calmar']:>8.2f}")
    print(f"{'中证转债指数':<12}{bm['total']:>8.1f}%{bm['ann']:>7.2f}%{bm['mdd']:>8.1f}%{bm['sharpe']:>7.2f}{bm['calmar']:>8.2f}")
    print(f"交易 {len(trades)} 笔, 费用占净值 {fees:.4f}")

    print("\n年度收益:")
    c = curve.copy()
    c["year"] = c["date"].dt.year
    prev_s = prev_b = None
    for y, g in c.groupby("year"):
        base_s = g["nav"].iloc[0] if prev_s is None else prev_s
        r = g["nav"].iloc[-1] / base_s - 1
        prev_s = g["nav"].iloc[-1]
        iy = idx_nav[idx_nav.index.year == y]
        if len(iy):
            base_b = iy.iloc[0] if prev_b is None else prev_b
            br = iy.iloc[-1] / base_b - 1
            prev_b = iy.iloc[-1]
        else:
            br = float("nan")
        print(f"  {y}: 双低 {r*100:+.1f}%   指数 {br*100:+.1f}%")

    print("\n卖出原因:", trades[trades["side"] == "卖"]["reason"].value_counts().to_dict())
    curve.to_csv(os.path.join(OUT, "nav_double_low.csv"), index=False, encoding="utf-8-sig")
    trades.to_csv(os.path.join(OUT, "trades_double_low.csv"), index=False, encoding="utf-8-sig")
    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
