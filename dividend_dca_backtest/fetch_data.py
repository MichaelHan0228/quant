"""
数据下载脚本
============
1. 中证红利低波指数 H30269（价格指数，2019-01 ~ 今，中证指数官网）
2. 中证红利低波全收益指数 H20269（全收益，用于重建段分红推算）
3. 563020 红利低波ETF易方达 日线（腾讯，不复权+前复权）

输出: data/*.csv
"""
import os
import time
import requests
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_csindex(code: str, start: str = "20190101", end: str = "20260721") -> pd.DataFrame:
    """中证指数官网日线"""
    url = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
    params = {"indexCode": code, "startDate": start, "endDate": end}
    h = {**HEADERS, "Referer": "https://www.csindex.com.cn/"}
    r = requests.get(url, params=params, headers=h, timeout=30)
    rows = r.json().get("data") or []
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["tradeDate"], format="%Y%m%d")
    df = df[["date", "open", "high", "low", "close"]].sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    return df


def fetch_tencent_kline(code: str, fq: str = "") -> pd.DataFrame:
    """腾讯日线。fq='qfq'前复权, ''=不复权"""
    param = f"sh{code},day,2020-01-01,2026-12-31,800,{fq}"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    d = r.json()["data"][f"sh{code}"]
    key = "qfqday" if fq == "qfq" and "qfqday" in d else "day"
    rows = d[key]
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "vol"][:len(rows[0])])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "close", "high", "low"]:
        df[c] = df[c].astype(float)
    return df.sort_values("date").reset_index(drop=True)


def main():
    print("下载 H30269 价格指数 ...")
    h30269 = fetch_csindex("H30269")
    h30269.to_csv(os.path.join(DATA_DIR, "h30269.csv"), index=False, encoding="utf-8-sig")
    print(f"  {len(h30269)} 行, {h30269['date'].iloc[0].date()} ~ {h30269['date'].iloc[-1].date()}")
    time.sleep(1)

    print("下载 H20269 全收益指数 ...")
    h20269 = fetch_csindex("H20269")
    h20269.to_csv(os.path.join(DATA_DIR, "h20269.csv"), index=False, encoding="utf-8-sig")
    print(f"  {len(h20269)} 行, {h20269['date'].iloc[0].date()} ~ {h20269['date'].iloc[-1].date()}")

    print("下载 563020 不复权日线 ...")
    raw = fetch_tencent_kline("563020", fq="")
    raw.to_csv(os.path.join(DATA_DIR, "563020_raw.csv"), index=False, encoding="utf-8-sig")
    print(f"  {len(raw)} 行, {raw['date'].iloc[0].date()} ~ {raw['date'].iloc[-1].date()}")

    print("下载 563020 前复权日线 ...")
    qfq = fetch_tencent_kline("563020", fq="qfq")
    qfq.to_csv(os.path.join(DATA_DIR, "563020_qfq.csv"), index=False, encoding="utf-8-sig")
    print(f"  {len(qfq)} 行, {qfq['date'].iloc[0].date()} ~ {qfq['date'].iloc[-1].date()}")

    # 数据校验：指数 vs ETF 重叠段的相关性/偏差
    import numpy as np
    m = pd.merge(h30269[["date", "close"]].rename(columns={"close": "idx"}),
                 raw[["date", "close"]].rename(columns={"close": "etf"}), on="date")
    m["ratio"] = m["idx"] / m["etf"]
    print(f"\n重叠段 {m['date'].iloc[0].date()} ~ {m['date'].iloc[-1].date()}: "
          f"指数/ETF 比值 首 {m['ratio'].iloc[0]:.1f} 末 {m['ratio'].iloc[-1]:.1f} "
          f"波动 {m['ratio'].std()/m['ratio'].mean()*100:.2f}%")
    tr = pd.merge(h30269[["date", "close"]].rename(columns={"close": "pr"}),
                  h20269[["date", "close"]].rename(columns={"close": "tr"}), on="date")
    tr["dr"] = tr["tr"] / tr["pr"]
    tr["year"] = tr["date"].dt.year
    for y, g in tr.groupby("year"):
        if y < 2020:
            continue
        yld = (g["dr"].iloc[-1] / g["dr"].iloc[0] - 1) * 100
        print(f"  {y} 隐含分红收益率: {yld:.2f}%")


if __name__ == "__main__":
    main()
