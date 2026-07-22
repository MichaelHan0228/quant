"""
全天候策略数据下载
==================
腾讯日线（前复权），分段拉取后拼接去重。
- 513500 标普500 / 518880 黄金 / 511010 国债(5年) / 511260 十年国债 / 159985 豆粕 / 511880 货币
- 红利低波：复用 dividend_dca_backtest 的 H30269/H20269/563020 数据
输出: data/*.csv
"""
import os
import time
import shutil
import requests
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

HEADERS = {"User-Agent": "Mozilla/5.0"}
DIVIDEND_PROJ = r"D:/研究/quant/dividend_dca_backtest/data"

TICKERS = {
    "513500": "sh",   # 标普500ETF
    "518880": "sh",   # 黄金ETF
    "511010": "sh",   # 国债ETF(5年)
    "511260": "sh",   # 十年国债ETF
    "159985": "sz",   # 豆粕ETF
    "511880": "sh",   # 货币ETF
    "510300": "sh",   # 沪深300ETF（基准）
}

# 分段区间（覆盖2018-2026）
CHUNKS = [("2017-01-01", "2019-12-31"), ("2020-01-01", "2022-12-31"),
          ("2023-01-01", "2026-12-31")]


def fetch_kline(market: str, code: str, start: str, end: str) -> pd.DataFrame:
    param = f"{market}{code},day,{start},{end},800,qfq"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
    r = requests.get(url, headers=HEADERS, timeout=15)
    d = r.json()["data"][f"{market}{code}"]
    rows = d.get("qfqday") or d.get("day") or []
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "vol"][:len(rows[0])] if rows else None)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "close", "high", "low"]:
        df[c] = df[c].astype(float)
    return df


def main():
    for code, mkt in TICKERS.items():
        parts = []
        for start, end in CHUNKS:
            df = fetch_kline(mkt, code, start, end)
            if not df.empty:
                parts.append(df)
            time.sleep(0.5)
        full = pd.concat(parts).drop_duplicates("date").sort_values("date").reset_index(drop=True)
        full.to_csv(os.path.join(DATA_DIR, f"{code}.csv"), index=False, encoding="utf-8-sig")
        print(f"{code}: {len(full)} 行, {full['date'].iloc[0].date()} ~ {full['date'].iloc[-1].date()}")

    # 复用红利项目数据
    for f in ["h30269.csv", "h20269.csv", "563020_raw.csv", "563020_qfq.csv"]:
        src = os.path.join(DIVIDEND_PROJ, f)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(DATA_DIR, f))
            print(f"复用 {f}")


if __name__ == "__main__":
    main()
