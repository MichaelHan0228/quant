# -*- coding: utf-8 -*-
"""双低可转债轮动 —— 实盘信号工具

拉取东财可转债实时比价表(同 akshare bond_cov_comparison), 计算双低值,
套用与回测一致的过滤条件, 输出 Top-N 清单;
--holdings 113050,128xxx 可对比在持, 给出 买入/卖出/持有 建议。

用法:
    python signal.py
    python signal.py --holdings 113050,128013 --n 15
"""
import sys
import time
import argparse
from datetime import datetime

import pandas as pd
import requests

import config as C
from backtest import load_universe, load_redeem_bad_codes, clean_rating

sys.stdout.reconfigure(encoding="utf-8")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def fetch_comparison():
    """东财可转债比价表(在市转债实时价格/溢价率)"""
    url = "https://16.push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": 1, "pz": 100, "po": 1, "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2, "invt": 2, "fid": "f243", "fs": "b:MK0354",
        "fields": "f2,f6,f12,f14,f227,f229,f232,f234,f236,f243",
    }
    rows, page = [], 1
    while True:
        params["pn"] = page
        js = None
        for _ in range(C.RETRY_TIMES):
            try:
                r = requests.get(url, params=params, headers=HEADERS, timeout=15)
                js = r.json()
                break
            except Exception:
                import time
                time.sleep(C.RETRY_INTERVAL)
        if js is None:
            raise RuntimeError("比价表请求失败")
        diff = (js.get("data") or {}).get("diff") or []
        if not diff:
            break
        rows.extend(diff)
        if len(diff) < 100:
            break
        page += 1
    df = pd.DataFrame(rows)
    df = df.rename(columns={
        "f2": "转债最新价", "f6": "成交额", "f12": "code", "f14": "转债名称", "f227": "上市日期",
        "f229": "纯债价值", "f232": "正股最新价", "f234": "转股价",
        "f236": "转股价值", "f243": "转股溢价率",
    })
    df["code"] = df["code"].astype(str).str.zfill(6)
    for col in ["转债最新价", "正股最新价", "转股价", "转股价值", "转股溢价率", "成交额"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["双低值"] = df["转债最新价"] + df["转股溢价率"]
    return df


def fetch_remain_shares(code):
    """东财 push2 stock/get 的 f84 = 转债剩余张数; 全部重试失败返回 None"""
    secid = ("1." if code.startswith("11") else "0.") + code
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": secid, "fields": "f84",
              "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": 2, "invt": 2}
    for _ in range(C.RETRY_TIMES):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=10)
            v = (r.json().get("data") or {}).get("f84")
            return float(v) if isinstance(v, (int, float)) else None
        except Exception:
            time.sleep(C.RETRY_INTERVAL)
    return None


def attach_remain_scale(df):
    """给候选表加 剩余规模(亿) 列并剔除不足 MIN_REMAIN_SCALE 的券。
    push2 拉取失败的券保留并告警(不崩, 宁可漏过滤)。"""
    df = df.copy()
    if df.empty:
        df["剩余规模(亿)"] = pd.Series(dtype=float)
        return df
    vals, failed = [], 0
    for code in df["code"]:
        sh = fetch_remain_shares(code)
        if sh is None:
            failed += 1
            vals.append(None)
        else:
            vals.append(round(sh * 100.0 / 1e8, 2))  # 张×100元 → 亿
        time.sleep(0.2)
    df["剩余规模(亿)"] = vals
    if failed:
        print(f"[WARN] {failed} 只券剩余张数拉取失败(push2 断连?), "
              f"这些券跳过剩余规模过滤")
    return df[df["剩余规模(亿)"].isna() | (df["剩余规模(亿)"] >= C.MIN_REMAIN_SCALE)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--holdings", default="", help="在持转债代码, 逗号分隔")
    ap.add_argument("--n", type=int, default=10)
    args = ap.parse_args()
    holdings = [c.strip().zfill(6) for c in args.holdings.split(",") if c.strip()]

    comp = fetch_comparison()
    uni = load_universe()
    uni = uni.set_index("code")
    redeem_bad = load_redeem_bad_codes()
    today = pd.Timestamp(datetime.now().date())

    rows = []
    for _, r in comp.iterrows():
        code = r["code"]
        if code not in uni.index:
            continue
        u = uni.loc[code]
        if code in redeem_bad:
            continue
        # 临近最后交易日(强赎/到期)禁买, 与回测 REDEEM_BAN_DAYS 口径一致
        if pd.notna(u["DELIST_DATE"]) and u["DELIST_DATE"] - today <= pd.Timedelta(days=C.REDEEM_BAN_DAYS):
            continue
        if not pd.notna(r["转债最新价"]) or r["转债最新价"] > C.MAX_PRICE:
            continue
        if pd.notna(r["成交额"]) and r["成交额"] < C.MIN_AVG_AMOUNT:
            continue  # 当日成交额不足(实时单口径, 回测用20日均值)
        if pd.isna(u["EXPIRE_DATE"]) or u["EXPIRE_DATE"] <= today + pd.DateOffset(years=C.MIN_EXPIRE_YEARS):
            continue
        if not pd.notna(u["ACTUAL_ISSUE_SCALE"]) or u["ACTUAL_ISSUE_SCALE"] < C.MIN_ISSUE_SCALE:
            continue
        rating = clean_rating(u["RATING"]) if pd.notna(u["RATING"]) else ""
        if rating not in C.ALLOWED_RATINGS:
            continue
        if pd.isna(u["LISTING_DATE"]):  # 上市天数在实时表无法精确, 粗过滤
            continue
        rows.append({"code": code, "转债名称": r["转债名称"], "转债最新价": r["转债最新价"],
                     "转股溢价率": r["转股溢价率"], "双低值": r["双低值"]})
    df = pd.DataFrame(rows).sort_values("双低值").reset_index(drop=True)
    df["rank"] = df.index

    # 剩余规模过滤: clist 无剩余张数字段(f84 实测返回 '-'),
    # 先取双低前 50 候选, 逐个调 push2 stock/get 取 f84(剩余张数),
    # 剩余规模(亿) = f84×100元/1e8 < MIN_REMAIN_SCALE 的剔除。
    df = attach_remain_scale(df.head(50)).reset_index(drop=True)
    df["rank"] = df.index

    print(f"\n=== 双低 Top-{args.n} (过滤后共 {len(df)} 只) ===")
    print(df.head(args.n).to_string(index=False))

    if holdings:
        rank_map = dict(zip(df["code"], df["rank"]))
        target = set(df.head(args.n)["code"])
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
