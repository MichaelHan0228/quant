# -*- coding: utf-8 -*-
"""双低可转债轮动 —— 每日检查回测引擎 + 绩效指标

双低值 = 转债收盘价 + 转股溢价率*100
每日收盘后排名, 次日开盘调仓(先卖后买, 等权, 10张整数手)
在持者排名未跌出 N+BUFFER 则继续持有(调仓缓冲, 逻辑与周频版一致)
每日检查的意义: 强赎公告等风险事件次日开盘即可退出, 不用等到周末

用法:
    python backtest.py                      # 全量, 默认 N=10
    python backtest.py --sample --start 2020-01-01 --end 2021-12-31 --n 15
"""
import os
import sys
import argparse
from bisect import bisect_right

# 项目内 signal.py 会遮蔽标准库 signal(numpy/pandas 依赖链需要),
# 把脚本目录/空串/cwd 移到 sys.path 末尾, 让标准库优先
_script_dir = os.path.dirname(os.path.abspath(__file__))
for _p in (_script_dir, "", os.getcwd()):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.append(_script_dir)

import numpy as np
import pandas as pd

import config as C

sys.stdout.reconfigure(encoding="utf-8")

REDEEM_BAD_STATUS = ("已公告强赎", "公告要强赎")  # 视为已公告强赎的状态


# ----------------------------------------------------------------------
# 数据加载
# ----------------------------------------------------------------------

def load_universe(sample_only=False):
    uni = pd.read_csv(C.UNIVERSE_CSV, encoding="utf-8-sig",
                      dtype={"SECURITY_CODE": str, "CONVERT_STOCK_CODE": str})
    uni["code"] = uni["SECURITY_CODE"].str.zfill(6)
    for col in ["LISTING_DATE", "DELIST_DATE", "EXPIRE_DATE", "VALUE_DATE"]:
        uni[col] = pd.to_datetime(uni[col], errors="coerce")
    for col in ["ACTUAL_ISSUE_SCALE", "TRANSFER_PRICE", "INITIAL_TRANSFER_PRICE"]:
        uni[col] = pd.to_numeric(uni[col], errors="coerce")
    uni["stock"] = (uni["CONVERT_STOCK_CODE"].fillna("")
                    .str.replace(r"\..*$", "", regex=True).str.zfill(6))
    if sample_only:  # 只保留有日线缓存的券(样本模式)
        cached = {f[:-4] for f in os.listdir(C.CB_DAILY_DIR) if f.endswith(".csv")}
        uni = uni[uni["code"].isin(cached)].reset_index(drop=True)
    return uni


def load_redeem_bad_codes():
    """集思录当前快照中已/将公告强赎的代码集合(实盘 signal.py 用;
    回测不用它——历史区间以 DELIST_DATE 窗口代替, 见 build_delist_deadlines)"""
    if not os.path.exists(C.REDEEM_CSV):
        return set()
    df = pd.read_csv(C.REDEEM_CSV, encoding="utf-8-sig", dtype={"code": str})
    df["code"] = df["code"].str.zfill(6)
    mask = df["强赎状态"].astype(str).str.contains("强赎") & \
           ~df["强赎状态"].astype(str).str.contains("不强赎")
    return set(df.loc[mask, "code"])


def build_delist_deadlines(uni):
    """每只券的最后交易日映射 code -> Timestamp(无则不含该键)。

    用 DELIST_DATE 作为"强赎/到期最后交易日"的硬事实锚点:
    候选池禁止买入窗口 / 持仓强制退出窗口均按距该日的天数计算,
    等效于"公告强赎后次日退出"的近似(公告到最后交易通常 2~4 周)。
    """
    dl = uni.loc[uni["DELIST_DATE"].notna(), ["code", "DELIST_DATE"]]
    return dict(zip(dl["code"], dl["DELIST_DATE"]))


def load_daily(path):
    df = pd.read_csv(path, encoding="utf-8-sig")
    if "volume" not in df.columns and "vol" in df.columns:
        df = df.rename(columns={"vol": "volume"})  # 旧缓存列名兼容
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


class DataStore:
    """按 code 懒加载日线, 提供 asof 价格查询"""

    def __init__(self, uni):
        self.cb = {}       # code -> DataFrame
        self.stock = {}    # stock code -> DataFrame
        for code in uni["code"]:
            p = os.path.join(C.CB_DAILY_DIR, f"{code}.csv")
            if os.path.exists(p):
                self.cb[code] = load_daily(p)
        for sc in uni["stock"].unique():
            if not sc or sc == "000000":
                continue
            p = os.path.join(C.STOCK_DAILY_DIR, f"{sc}.csv")
            if os.path.exists(p):
                self.stock[sc] = load_daily(p)

    def cb_bar(self, code, date):
        """当日 K 线(无则 None)"""
        df = self.cb.get(code)
        if df is None:
            return None
        hit = df[df["date"] == date]
        return hit.iloc[0] if not hit.empty else None

    def cb_close_asof(self, code, date):
        return self._asof(self.cb.get(code), date, "close")

    def cb_avg_amount(self, code, date, window=20):
        """近 window 个成交日的日均成交额(元)近似 = 均价×成交量。
        转债日线 volume 单位为张(已用兴业转债验证: 278万张×118元 ≈ 3.3亿, 与盘面吻合)"""
        df = self.cb.get(code)
        if df is None or df.empty:
            return np.nan
        idx = df["date"].searchsorted(date, side="right")
        if idx <= 0:
            return np.nan
        tail = df.iloc[max(0, idx - window):idx]
        amt = (tail["close"] * tail["volume"]).dropna()
        return float(amt.mean()) if not amt.empty else np.nan

    def cb_open_asof(self, code, date):
        return self._asof(self.cb.get(code), date, "open")

    def stock_close_asof(self, sc, date):
        return self._asof(self.stock.get(sc), date, "close")

    @staticmethod
    def _asof(df, date, col):
        if df is None or df.empty:
            return np.nan
        idx = df["date"].searchsorted(date, side="right") - 1
        if idx < 0:
            return np.nan
        return float(df[col].iloc[idx])

    def listed_days(self, code, date):
        """截至 date 的上市交易日数(用日线行数近似)"""
        df = self.cb.get(code)
        if df is None:
            return 0
        return int(df["date"].searchsorted(date, side="right"))

    def last_bar(self, code):
        df = self.cb.get(code)
        return df.iloc[-1] if df is not None and not df.empty else None


# ----------------------------------------------------------------------
# 转股价时间轴: 初始转股价 + 分红送转逐次调整
# ----------------------------------------------------------------------

def load_dividend_events():
    """正股分红送转事件: stock -> [(date, 每股股息, 每股送转比例)], 按日期排序"""
    events = {}
    if not os.path.isdir(C.DIVIDENDS_DIR):
        return events
    for f in os.listdir(C.DIVIDENDS_DIR):
        if not f.endswith(".csv"):
            continue
        try:
            df = pd.read_csv(os.path.join(C.DIVIDENDS_DIR, f), encoding="utf-8-sig")
        except Exception:
            continue
        df["EX_DIVIDEND_DATE"] = pd.to_datetime(df["EX_DIVIDEND_DATE"], errors="coerce")
        evs = []
        for r in df.itertuples():
            if pd.isna(r.EX_DIVIDEND_DATE):
                continue
            d = (float(r.PRETAX_BONUS_RMB) if pd.notna(r.PRETAX_BONUS_RMB) else 0.0) / 10.0
            b = (float(r.BONUS_RATIO) if pd.notna(r.BONUS_RATIO) else 0.0) / 10.0
            if d > 0 or b > 0:
                evs.append((r.EX_DIVIDEND_DATE, d, b))
        if evs:
            events[f[:-4].zfill(6)] = sorted(evs)
    return events


def load_revision_events():
    """下修公告事件: stock -> [date, ...], 按日期排序"""
    if not os.path.exists(C.REVISIONS_CSV):
        return {}
    df = pd.read_csv(C.REVISIONS_CSV, encoding="utf-8-sig", dtype={"stock": str})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    out = {}
    for stock, g in df.groupby(df["stock"].str.zfill(6)):
        out[stock] = sorted(g["date"])
    return out


_stock_close_cache = {}


def _stock_closes(stock):
    """正股收盘价序列(带缓存): DataFrame[date, close] 或 None"""
    if stock in _stock_close_cache:
        return _stock_close_cache[stock]
    p = os.path.join(C.STOCK_DAILY_DIR, f"{stock}.csv")
    df = None
    if os.path.exists(p):
        df = pd.read_csv(p, encoding="utf-8-sig")
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")[["date", "close"]].dropna().reset_index(drop=True)
    _stock_close_cache[stock] = df
    return df


def estimate_revision_tp(closes, date):
    """下修新价近似: max(公告日前 20 日均价, 前一交易日收盘价)
    (主流下修条款的下限, 多数公司下修到底; 会比真实新价略高, 方向保守)"""
    if closes is None:
        return np.nan
    hist = closes[closes["date"] <= date]["close"]
    if hist.empty:
        return np.nan
    return float(max(hist.tail(20).mean(), hist.iloc[-1]))


def build_tp_timelines(uni, div_events, rev_events=None):
    """每只券的转股价时间轴: code -> (dates, tps)。

    以发行起息日(VALUE_DATE, 缺省上市日)的初始转股价为起点, 此后:
    - 分红送转: TP_new = (TP - 每股股息) / (1 + 每股送转)  (兴业转债已验证)
    - 下修公告: TP_new ≈ max(20日均价, 前收盘价), 仅当低于当前 TP 时生效
    不含增发/配股调整(无免费数据源), 残余误差方向为高估溢价率 → 少买, 偏保守。
    """
    rev_events = rev_events or {}
    timelines = {}
    for r in uni.itertuples():
        tp0 = r.INITIAL_TRANSFER_PRICE
        if not np.isfinite(tp0) or tp0 <= 0:
            continue
        anchor = r.VALUE_DATE if pd.notna(r.VALUE_DATE) else r.LISTING_DATE
        if pd.isna(anchor):
            continue
        # 合并两类事件按时间排序处理
        events = [(d, "div", dv, b) for d, dv, b in div_events.get(r.stock, []) if d > anchor]
        events += [(d, "rev", 0.0, 0.0) for d in rev_events.get(r.stock, []) if d > anchor]
        events.sort(key=lambda x: x[0])
        dates, tps = [anchor], [float(tp0)]
        tp = float(tp0)
        closes = None
        for date, kind, d, b in events:
            if kind == "div":
                tp = (tp - d) / (1.0 + b)
            else:
                if closes is None:
                    closes = _stock_closes(r.stock)
                est = estimate_revision_tp(closes, date)
                if np.isfinite(est) and est < tp:
                    tp = est
                else:
                    continue
            if tp <= 0:
                break
            dates.append(date)
            tps.append(tp)
        timelines[r.code] = (dates, tps)
    return timelines


def tp_at(timeline, date):
    """查某天的转股价(最后一条不晚于 date 的记录)"""
    if timeline is None:
        return np.nan
    dates, tps = timeline
    idx = bisect_right(dates, date) - 1
    return tps[idx] if idx >= 0 else np.nan


# ----------------------------------------------------------------------
# 信号: 双低值与过滤
# ----------------------------------------------------------------------

def compute_rank(store, uni, signal_date, deadlines, tp_timelines):
    """返回当日通过过滤的候选 DataFrame(含双低值, 升序)"""
    ban_delta = pd.Timedelta(days=C.REDEEM_BAN_DAYS)
    rows = []
    for r in uni.itertuples():
        code = r.code
        bar = store.cb_bar(code, signal_date)
        if bar is None or not np.isfinite(bar["close"]):
            continue  # 当日无成交价
        close = float(bar["close"])
        if close > C.MAX_PRICE:
            continue
        dl = deadlines.get(code)
        if dl is not None and dl - signal_date <= ban_delta:
            continue  # 临近最后交易日(强赎/到期), 禁止买入
        if pd.isna(r.LISTING_DATE) or r.LISTING_DATE > signal_date:
            continue
        if store.listed_days(code, signal_date) < C.MIN_LISTED_DAYS:
            continue
        if pd.notna(r.DELIST_DATE) and r.DELIST_DATE <= signal_date:
            continue
        if pd.isna(r.EXPIRE_DATE) or r.EXPIRE_DATE <= signal_date + pd.DateOffset(years=C.MIN_EXPIRE_YEARS):
            continue
        if not np.isfinite(r.ACTUAL_ISSUE_SCALE) or r.ACTUAL_ISSUE_SCALE < C.MIN_ISSUE_SCALE:
            continue
        rating = str(r.RATING).strip() if pd.notna(r.RATING) else ""
        if rating not in C.ALLOWED_RATINGS:
            continue
        if C.MIN_AVG_AMOUNT > 0:
            amt = store.cb_avg_amount(code, signal_date)
            if not np.isfinite(amt) or amt < C.MIN_AVG_AMOUNT:
                continue  # 流动性不足
        # 转股溢价率: 收盘价 / (正股价*100/转股价) - 1; 转股价取当日时间轴值
        sc_close = store.stock_close_asof(r.stock, signal_date)
        tp = tp_at(tp_timelines.get(code), signal_date)
        if not np.isfinite(tp) and np.isfinite(r.TRANSFER_PRICE):
            tp = r.TRANSFER_PRICE  # 无时间轴时的兜底(缺失 INITIAL 的老券)
        if not np.isfinite(sc_close) or not np.isfinite(tp) or tp <= 0:
            continue
        conv_value = sc_close * 100.0 / tp
        if conv_value <= 0:
            continue
        premium = close / conv_value - 1.0
        rows.append({"code": code, "close": close, "premium": premium,
                     "double_low": close + premium * 100.0})
    if not rows:
        return pd.DataFrame(columns=["code", "close", "premium", "double_low", "rank"])
    df = pd.DataFrame(rows).sort_values("double_low").reset_index(drop=True)
    df["rank"] = df.index
    return df


def select_target(ranked, holdings, n):
    """缓冲轮动: 在持且排名 < N+BUFFER 保留, 其余名额按双低升序补足"""
    rank_map = dict(zip(ranked["code"], ranked["rank"]))
    target = [c for c in holdings
              if c in rank_map and rank_map[c] < n + C.BUFFER_RANK]
    held = set(target)
    for c in ranked["code"]:
        if len(target) >= n:
            break
        if c not in held:
            target.append(c)
    return target[:n]


# ----------------------------------------------------------------------
# 回测引擎
# ----------------------------------------------------------------------

def run_backtest(uni, store, bench, n, start_date, end_date):
    cal = bench["date"].tolist()
    cal = [d for d in cal if d >= start_date and (end_date is None or d <= end_date)]
    if len(cal) < 10:
        raise RuntimeError("交易日历过短, 检查基准数据")
    deadlines = build_delist_deadlines(uni)
    ban_delta = pd.Timedelta(days=C.REDEEM_BAN_DAYS)
    div_events = load_dividend_events()
    rev_events = load_revision_events() if C.APPLY_REVISION_EST else {}
    tp_timelines = build_tp_timelines(uni, div_events, rev_events)

    cash = float(C.INITIAL_CASH)
    holdings = {}          # code -> 张数
    last_price = {}        # code -> 最近已知收盘价(估值用)
    trades = []            # (date, code, side, price, qty, amount, reason)
    equity_rows = []
    started = False

    def portfolio_value(date):
        v = cash
        for c, q in holdings.items():
            px = store.cb_close_asof(c, date)
            if np.isfinite(px):
                last_price[c] = px
            px = last_price.get(c)  # 无当日价用最近已知价估值
            if px is not None:
                v += q * px
        return v

    # 每日循环: 开盘执行昨日信号 -> 当日估值 -> 收盘算新信号(次日执行)
    # 排名/缓冲逻辑与周频版完全一致; 每日检查使强赎等风险次日即可退出
    pending_target = None
    for di, d in enumerate(cal):
        # ---- 开盘执行昨日信号(先卖后买) ----
        if pending_target is not None:
            target = pending_target
            exec_date = d

            # ---- 先卖 ----
            for code in list(holdings.keys()):
                bar = store.cb_bar(code, exec_date)
                lb = store.last_bar(code)
                # 强制退出: 到期退市, 以最后可得收盘价了结
                if bar is None and lb is not None and lb["date"] <= exec_date:
                    price = float(lb["close"]) * (1 - C.SLIPPAGE)
                    amount = holdings[code] * price * (1 - C.COMMISSION)
                    cash += amount
                    trades.append((lb["date"], code, "SELL", float(lb["close"]),
                                   holdings[code], amount, "delist_exit"))
                    del holdings[code]
                    continue
                if code in target:
                    continue
                # 次日开盘卖出(临近最后交易日的券标记 redeem_exit); 当日无成交则继续持有, 次日再试
                if bar is not None and np.isfinite(bar["open"]):
                    dl = deadlines.get(code)
                    reason = ("redeem_exit" if dl is not None and dl - d <= ban_delta
                              else "rotate_out")
                    price = float(bar["open"]) * (1 - C.SLIPPAGE)
                    amount = holdings[code] * price * (1 - C.COMMISSION)
                    cash += amount
                    trades.append((exec_date, code, "SELL", float(bar["open"]),
                                   holdings[code], amount, reason))
                    del holdings[code]
            started = started or bool(trades)

            # ---- 后买 ----
            buys = [c for c in target if c not in holdings]
            if buys:
                equity = portfolio_value(exec_date)
                tgt_val = equity / n
                for code in buys:
                    b = store.cb_bar(code, exec_date)
                    if b is None or not np.isfinite(b["open"]):
                        continue  # 当日无成交, 放弃该笔, 次日信号重算后再试
                    open_px = float(b["open"])
                    eff_px = open_px * (1 + C.SLIPPAGE)
                    lots = int(tgt_val / (eff_px * 10))  # 1手=10张
                    qty = lots * 10
                    if qty <= 0:
                        continue
                    amount = qty * eff_px * (1 + C.COMMISSION)
                    if amount > cash:
                        lots = int(cash / (eff_px * (1 + C.COMMISSION) * 10))
                        qty = lots * 10
                        if qty <= 0:
                            continue
                        amount = qty * eff_px * (1 + C.COMMISSION)
                    cash -= amount
                    holdings[code] = holdings.get(code, 0) + qty
                    trades.append((exec_date, code, "BUY", open_px, qty, amount, "rotate_in"))

        # ---- 当日估值 ----
        if started or holdings:
            equity_rows.append({"date": d, "equity": portfolio_value(d)})

        # ---- 收盘后发信号(次日开盘执行) ----
        if di + 1 >= len(cal):
            break
        ranked = compute_rank(store, uni, d, deadlines, tp_timelines)
        pending_target = select_target(ranked, holdings, n)

    equity = pd.DataFrame(equity_rows).drop_duplicates(subset="date")
    tr = pd.DataFrame(trades, columns=["date", "code", "side", "price",
                                       "qty", "amount", "reason"])
    return equity, tr


# ----------------------------------------------------------------------
# 绩效指标
# ----------------------------------------------------------------------

def perf_metrics(equity, bench, trades, n):
    eq = equity.set_index("date")["equity"].sort_index()
    ret = eq.pct_change().dropna()
    total = eq.iloc[-1] / eq.iloc[0] - 1
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    ann = (1 + total) ** (1 / years) - 1 if years > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan
    calmar = ann / abs(dd) if dd != 0 else np.nan
    weekly = eq.resample("W").last().pct_change().dropna()
    win_rate = (weekly > 0).mean() if len(weekly) else np.nan
    yearly = (eq.resample("YE").last().pct_change())
    first_year = eq[eq.index.year == eq.index[0].year]
    yearly.iloc[0] = first_year.iloc[-1] / eq.iloc[0] - 1
    yearly.index = yearly.index.year

    # 基准同期
    b = bench.set_index("date")["close"].sort_index()
    b = b[(b.index >= eq.index[0]) & (b.index <= eq.index[-1])]
    b_total = b.iloc[-1] / b.iloc[0] - 1 if len(b) > 1 else np.nan
    b_ann = (1 + b_total) ** (1 / years) - 1 if years > 0 and np.isfinite(b_total) else np.nan

    summary = {
        "N": n,
        "区间": f"{eq.index[0].date()} ~ {eq.index[-1].date()}",
        "期末净值": round(eq.iloc[-1] / eq.iloc[0], 4),
        "总收益率": f"{total:.2%}",
        "年化收益": f"{ann:.2%}",
        "最大回撤": f"{dd:.2%}",
        "夏普": round(sharpe, 3),
        "Calmar": round(calmar, 3),
        "周胜率": f"{win_rate:.2%}",
        "交易次数": len(trades),
        "基准总收益": f"{b_total:.2%}",
        "基准年化": f"{b_ann:.2%}",
    }
    return summary, yearly


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="只用有日线缓存的样本券")
    ap.add_argument("--start", default=C.START_DATE)
    ap.add_argument("--end", default=C.END_DATE)
    ap.add_argument("--n", default=",".join(map(str, C.N_LIST)),
                    help="持仓只数, 逗号分隔, 如 10,15,20")
    args = ap.parse_args()

    start_date = pd.Timestamp(args.start)
    end_date = pd.Timestamp(args.end) if args.end else None

    print("加载数据 ...")
    uni = load_universe(sample_only=args.sample)
    print(f"宇宙 {len(uni)} 只{'(样本)' if args.sample else ''}")
    store = DataStore(uni)
    bench = load_daily(C.BENCH_CSV)
    print(f"转债日线 {len(store.cb)} 只, 正股日线 {len(store.stock)} 只, "
          f"基准 {len(bench)} 行")

    os.makedirs(C.OUTPUT_DIR, exist_ok=True)
    summaries = []
    for n in [int(x) for x in args.n.split(",")]:
        print(f"\n===== 回测 N={n} =====")
        equity, trades = run_backtest(uni, store, bench, n, start_date, end_date)
        if equity.empty:
            print("  无回测结果(检查区间与数据)")
            continue
        summary, yearly = perf_metrics(equity, bench, trades, n)
        summaries.append(summary)
        equity.to_csv(os.path.join(C.OUTPUT_DIR, f"equity_curve_n{n}.csv"),
                      index=False, encoding="utf-8-sig")
        trades.to_csv(os.path.join(C.OUTPUT_DIR, f"trades_n{n}.csv"),
                      index=False, encoding="utf-8-sig")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print("  年度收益:")
        for y, r in yearly.items():
            print(f"    {y}: {r:.2%}")

    if summaries:
        sdf = pd.DataFrame(summaries)
        sdf.to_csv(os.path.join(C.OUTPUT_DIR, "summary.csv"),
                   index=False, encoding="utf-8-sig")
        print(f"\nsummary.csv 已保存")


if __name__ == "__main__":
    main()
