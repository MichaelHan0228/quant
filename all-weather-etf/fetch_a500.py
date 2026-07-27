"""
A500加仓层数据下载
==================
- data/563360.csv   华泰柏瑞中证A500ETF 日线前复权（腾讯 fqkline，首批 2024-10-15 上市，
                    与 A500 PE 起点 2024-09-03 之间仅 1 个月空窗，衔接 510300 用）
- data/a500_pe.csv  中证A500指数(000510) PE_TTM 日频（中证指数官网 index-perf，
                    字段 peg 实为市盈率；真实数据自 2024-09-03 起，此前不存在）
7 天内缓存新鲜则跳过。
"""
import os
import time
import requests
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
ETF_FILE = os.path.join(DATA, "563360.csv")
PE_FILE = os.path.join(DATA, "a500_pe.csv")


def fresh(path: str, days: int = 7) -> bool:
    if not os.path.exists(path):
        return False
    df = pd.read_csv(path, parse_dates=["date"])
    return (pd.Timestamp.today() - df["date"].max()).days <= days


def fetch_563360(force: bool = False):
    """腾讯日线（前复权），同 fetch_data.py 口径"""
    if not force and fresh(ETF_FILE):
        print("563360: 缓存新鲜，跳过")
        return
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           "?param=sh563360,day,2024-10-01,2026-12-31,800,qfq")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    rows = r.json()["data"]["sh563360"]
    rows = rows.get("qfqday") or rows.get("day") or []
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "vol"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "close", "high", "low"]:
        df[c] = df[c].astype(float)
    df.to_csv(ETF_FILE, index=False, encoding="utf-8-sig")
    print(f"563360: {len(df)} 行, {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")


def fetch_a500_pe(force: bool = False):
    """中证A500(000510) PE_TTM（中证指数官网，2024-09-03 起有真实数据）"""
    if not force and fresh(PE_FILE):
        print("a500_pe: 缓存新鲜，跳过")
        return
    r = requests.get(
        "https://www.csindex.com.cn/csindex-home/perf/index-perf",
        params={"indexCode": "000510", "startDate": "20240101",
                "endDate": pd.Timestamp.today().strftime("%Y%m%d")},
        headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"},
        timeout=120)
    rows = r.json().get("data") or []
    if not rows:
        raise RuntimeError("中证指数返回空数据")
    df = pd.DataFrame(rows)
    out = df[["tradeDate", "peg"]].rename(columns={"tradeDate": "date", "peg": "pe"})
    out["date"] = pd.to_datetime(out["date"], format="%Y%m%d")
    out = out.dropna().sort_values("date")
    out.to_csv(PE_FILE, index=False)
    print(f"a500_pe: {len(out)} 行, {out['date'].iloc[0].date()} ~ {out['date'].iloc[-1].date()},"
          f" PE范围 {out['pe'].min():.2f}~{out['pe'].max():.2f}")


if __name__ == "__main__":
    fetch_563360()
    time.sleep(0.5)
    fetch_a500_pe()
