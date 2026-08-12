# -*- coding: utf-8 -*-
"""从 MiniQMT 拉取回测用前复权日线（2013 起，预留 25 根 warmup 窗口）。

标的 = 5 只策略池 + 511010 国债ETF（早期标的未上市时的替补）。
运行: py -3.13 fetch_qmt_data.py   （需先打开 MiniQMT 并登录）
输出: data_qmt/{code}.csv  列: date,open,close,high,low,vol
"""
import datetime as dt
import os

import pandas as pd
from xtquant import xtdata

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data_qmt")
os.makedirs(DATA_DIR, exist_ok=True)

START = "20130101"   # 2014 回测起点 + warmup；513100/518880/511010 均 2013 年上市
TODAY = dt.date.today().strftime("%Y%m%d")

ETFS = {
    "512890": "512890.SH",   # 红利低波ETF  2018-12 上市
    "159949": "159949.SZ",   # 创业板50ETF  2016-07 上市
    "513100": "513100.SH",   # 纳指ETF      2013-05 上市
    "518880": "518880.SH",   # 黄金ETF      2013-07 上市
    "159985": "159985.SZ",   # 豆粕ETF      2019-12 上市
    "511010": "511010.SH",   # 国债ETF      2013-08 上市（替补）
}


def fetch(qmt_code: str) -> pd.DataFrame:
    xtdata.download_history_data(qmt_code, period="1d", start_time=START, end_time=TODAY)
    d = xtdata.get_market_data_ex(
        [], [qmt_code], period="1d", start_time=START, end_time=TODAY,
        dividend_type="front")
    df = d[qmt_code]
    if df.empty:
        raise RuntimeError(f"{qmt_code} 无数据，MiniQMT 是否已登录？")
    idx = df.index.astype(str)
    try:
        dates = pd.to_datetime(idx, format="%Y%m%d")
    except ValueError:
        dates = pd.to_datetime(idx.astype("int64"), unit="ms")
    out = pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": df["open"].astype(float).values,
        "close": df["close"].astype(float).values,
        "high": df["high"].astype(float).values,
        "low": df["low"].astype(float).values,
        "vol": df["volume"].astype(float).values,
    })
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def main():
    try:
        xtdata.enable_hello = False
    except Exception:
        pass
    print(f"下载区间 {START} ~ {TODAY}（MiniQMT 前复权日线）")
    for code, qc in ETFS.items():
        df = fetch(qc)
        df.to_csv(os.path.join(DATA_DIR, f"{code}.csv"), index=False)
        print(f"  {qc}: {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}, {len(df)} 根")
    print(f"完成，输出目录: {DATA_DIR}")


if __name__ == "__main__":
    main()
