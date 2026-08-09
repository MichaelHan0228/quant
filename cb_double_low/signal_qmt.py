# -*- coding: utf-8 -*-
"""双低可转债轮动 —— 实盘信号工具 (QMT 版)

数据底座为本地 MiniQMT (xtquant), 不经过东财 push2, 规避其 IP 风控:
  - 转债实时价/五档/成交额: xtdata.get_full_tick
  - 剩余规模: xtdata.get_instrument_detail 的 FloatVolume (面值口径, 与东财 f84×100 吻合)
  - 转股溢价率: 自算(与回测口径一致) = 转债价 / (正股价*100/转股价) - 1,
    转股价取 build_tp_timelines 时间轴当日值(初始转股价+分红送转逐次调整)
东财版见 signal.py —— 无 QMT 的机器上用那个。

⚠️ 运行环境: xtquant 的二进制 pyd 只支持 Python 3.6~3.13, 本机 Python 3.14 跑不了:
    py -3.13 signal_qmt.py
    py -3.13 signal_qmt.py --holdings 113050,128013 --n 15
(需先打开 MiniQMT 并登录)
"""
import os
import sys
import argparse
from datetime import datetime

_script_dir = os.path.dirname(os.path.abspath(__file__))
for _p in (_script_dir, "", os.getcwd()):
    while _p in sys.path:
        sys.path.remove(_p)
sys.path.append(_script_dir)

import pandas as pd

import config as C
from backtest import (load_universe, load_redeem_bad_codes, load_dividend_events,
                      build_tp_timelines, tp_at, clean_rating)

sys.stdout.reconfigure(encoding="utf-8")


def _mkt(code: str) -> str:
    if code.startswith(("6", "9", "11", "13", "5")):
        return "SH"
    if code.startswith(("8", "4")):
        return "BJ"
    return "SZ"


def fetch_qmt_snapshot():
    """QMT 实时快照: 在市转债的 最新价/成交额/剩余规模/正股价。
    返回 (cb_rows: dict[code -> {price, amount, remain_yi}], stock_px: dict[stock -> price])
    """
    from xtquant import xtdata
    try:
        xtdata.enable_hello = False
    except Exception:
        pass

    sector = xtdata.get_stock_list_in_sector("沪深转债")
    if not sector:
        raise RuntimeError("QMT 沪深转债板块为空, 检查 MiniQMT 是否已登录")
    cb_codes = [s for s in sector]  # 形如 110085.SH
    ticks = xtdata.get_full_tick(cb_codes)

    cb_rows = {}
    stock_set = set()
    for sc in cb_codes:
        t = ticks.get(sc) or {}
        px = t.get("lastPrice") or 0
        if px <= 0:
            continue  # 停牌/无行情
        det = xtdata.get_instrument_detail(sc) or {}
        code = sc.split(".")[0]
        cb_rows[code] = {
            "price": float(px),
            "amount": float(t.get("amount") or 0),          # 成交额(元)
            "remain_yi": float(det.get("FloatVolume") or 0) / 1e8,  # 剩余规模(亿, 面值)
        }
        stock_set.add(sc)
    return cb_rows, ticks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdings", default="", help="在持转债代码, 逗号分隔")
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()
    holdings = [c.strip().zfill(6) for c in args.holdings.split(",") if c.strip()]

    from xtquant import xtdata
    cb_rows, _ = fetch_qmt_snapshot()
    print(f"QMT 在市转债快照: {len(cb_rows)} 只")

    uni = load_universe().set_index("code")
    redeem_bad = load_redeem_bad_codes()
    today = pd.Timestamp(datetime.now().date())

    # 转股价时间轴(与回测同一套: 初始转股价 + 分红送转逐次调整)
    tp_timelines = build_tp_timelines(uni.reset_index(), load_dividend_events())

    # 正股实时价(自算溢价率用)
    stocks = sorted({uni.loc[c, "stock"] for c in cb_rows
                     if c in uni.index and uni.loc[c, "stock"] and uni.loc[c, "stock"] != "000000"})
    scodes = [f"{s}.{_mkt(s)}" for s in stocks]
    sticks = xtdata.get_full_tick(scodes)
    stock_px = {s.split(".")[0]: float(t.get("lastPrice") or 0)
                for s, t in sticks.items() if t}

    # 上市天数: 用 QMT 交易日历数(次新券精确判断 >=10 个交易日)
    # 注意 xtdata 时间参数必须是 YYYYMMDD(带横线会静默返回空), 返回毫秒时间戳
    cal = xtdata.get_trading_dates("SH", start_time="20190101",
                                   end_time=today.strftime("%Y%m%d"))

    rows = []
    for code, q in cb_rows.items():
        if code not in uni.index:
            continue
        u = uni.loc[code]
        if code in redeem_bad:
            continue
        # 临近最后交易日(强赎/到期)禁买, 与回测 REDEEM_BAN_DAYS 口径一致
        if pd.notna(u["DELIST_DATE"]) and u["DELIST_DATE"] - today <= pd.Timedelta(days=C.REDEEM_BAN_DAYS):
            continue
        if q["price"] > C.MAX_PRICE:
            continue
        if q["remain_yi"] < C.MIN_REMAIN_SCALE:
            continue  # 剩余规模不足(实时精确值, FloatVolume 面值口径)
        if pd.isna(u["EXPIRE_DATE"]) or u["EXPIRE_DATE"] <= today + pd.DateOffset(years=C.MIN_EXPIRE_YEARS):
            continue
        rating = clean_rating(u["RATING"]) if pd.notna(u["RATING"]) else ""
        if rating not in C.ALLOWED_RATINGS:
            continue
        if pd.isna(u["LISTING_DATE"]) or u["LISTING_DATE"] > today:
            continue
        # 上市满 10 个交易日: 从上市日到今天的交易日数(用全市场日历近似)
        ld_ms = int(pd.Timestamp(u["LISTING_DATE"]).timestamp() * 1000)
        listed_days = sum(1 for d in cal if d >= ld_ms)
        if listed_days < C.MIN_LISTED_DAYS:
            continue
        # 溢价率自算: 转债价 / (正股价*100/转股价) - 1
        spx = stock_px.get(u["stock"], 0)
        tp = tp_at(tp_timelines.get(code), today)
        if spx <= 0 or not pd.notna(tp) or tp <= 0:
            continue
        premium = q["price"] / (spx * 100.0 / tp) - 1.0
        rows.append({"code": code, "转债名称": u["SECURITY_NAME_ABBR"],
                     "转债最新价": q["price"], "转股溢价率": round(premium * 100, 2),
                     "双低值": round(q["price"] + premium * 100, 2),
                     "剩余规模(亿)": round(q["remain_yi"], 2)})
    df = pd.DataFrame(rows).sort_values("双低值").reset_index(drop=True)
    df["rank"] = df.index

    print(f"\n=== 双低 Top-{args.n} (过滤后共 {len(df)} 只, QMT 源) ===")
    print(df.head(args.n).to_string(index=False))

    if holdings:
        rank_map = dict(zip(df["code"], df["rank"]))
        print("\n=== 持仓建议 ===")
        for c in holdings:
            if c not in rank_map:
                print(f"  {c}: 卖出 (已不符合过滤条件/退市)")
            elif rank_map[c] >= args.n + C.BUFFER_RANK:
                print(f"  {c}: 卖出 (双低排名 {rank_map[c]}, 跌出 {args.n}+{C.BUFFER_RANK} 缓冲)")
            else:
                print(f"  {c}: 持有 (双低排名 {rank_map[c]})")
        buy = [c for c in df.head(args.n)["code"] if c not in holdings]
        held_cnt = sum(1 for c in holdings
                       if c in rank_map and rank_map[c] < args.n + C.BUFFER_RANK)
        print(f"  应买入 {max(0, args.n - held_cnt)} 只候选: {buy[:max(0, args.n - held_cnt)]}")


if __name__ == "__main__":
    main()
