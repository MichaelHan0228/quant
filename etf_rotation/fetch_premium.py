"""拉取 QDII ETF 溢价率历史：腾讯不复权收盘价 / 天天基金单位净值 - 1。

QDII ETF 净值每晚公布（T 日净值 T 日晚间可得），实盘可用 IOPV 实时替代；
回测用 T 日净值对 T 日收盘，属轻微口径简化（常规做法）。

用法: python fetch_premium.py [代码...]   （默认 513100）
输出: data/{code}_premium.csv  (date, close_raw, nav, premium)
"""
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"


def _get(url: str, encoding: str = "utf-8") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://fund.eastmoney.com/"})
    for attempt in range(3):
        try:
            return urllib.request.urlopen(req, timeout=15).read().decode(
                encoding, errors="ignore")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))


def _raw_kline(tencent_code: str, start: str = "2013-01-01") -> pd.DataFrame:
    """腾讯不复权日K（溢价必须用不复权价对净值）。"""
    end = pd.Timestamp.today().strftime("%Y-%m-%d")
    rows = []
    while True:
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={tencent_code},day,{start},{end},640,")
        node = json.loads(_get(url))["data"][tencent_code]
        chunk = node.get("day") or []
        if not chunk:
            break
        rows = chunk + rows
        if len(chunk) < 640 or chunk[0][0] <= start:
            break
        end = (pd.Timestamp(chunk[0][0]) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.3)
    df = pd.DataFrame([r[:2] for r in rows], columns=["date", "close_raw"])
    return df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)


def _unit_nav(code: str) -> pd.DataFrame:
    """天天基金单位净值历史（pingzhongdata）。"""
    raw = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js")
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", raw, re.S)
    if not m:
        raise RuntimeError(f"{code} 净值数据解析失败")
    rows = json.loads(m.group(1))
    df = pd.DataFrame({"date": [pd.Timestamp(r["x"], unit="ms").strftime("%Y-%m-%d")
                                for r in rows],
                       "nav": [float(r["y"]) for r in rows]})
    return df.drop_duplicates(subset="date").sort_values("date").reset_index(drop=True)


def _split_adjust(s: pd.Series) -> pd.Series:
    """份额拆分/合并调整：相邻值比值超出 [0.7, 1.43]（日净值/价格不可能单日 ±30%）
    视为拆分事件，将之前的所有历史值按该比例缩放，使序列连续（后复权思路）。"""
    vals = s.values.astype(float).copy()
    factor = 1.0
    for t in range(len(vals) - 1, 0, -1):
        r = vals[t] / vals[t - 1] if vals[t - 1] else 1.0
        if r < 0.7 or r > 1.43:
            vals[:t] *= r   # t-1 及之前缩放到 t 的拆分后口径
    return pd.Series(vals, index=s.index)


def fetch_premium(code: str) -> pd.DataFrame:
    prefix = "sh" if code.startswith("6") else "sz"
    px = _raw_kline(f"{prefix}{code}")
    nav = _unit_nav(code)
    df = px.merge(nav, on="date", how="inner")
    df["close_raw"] = df["close_raw"].astype(float)
    df = df.sort_values("date").reset_index(drop=True)
    # 拆分调整（价格与净值的拆分生效日常差几天，各自独立调整后再对齐）
    df["close_raw"] = _split_adjust(df["close_raw"])
    df["nav"] = _split_adjust(df["nav"])
    df["premium"] = df["close_raw"] / df["nav"] - 1
    # 残余脏数据兜底：|溢价|>50% 视为错误置 NaN（引擎按缺失 ffill）
    df.loc[df["premium"].abs() > 0.5, "premium"] = float("nan")
    df["premium"] = df["premium"].ffill()
    out = df[["date", "close_raw", "nav", "premium"]]
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{code}_premium.csv"
    out.to_csv(path, index=False)
    print(f"{code}: {out['date'].iloc[0]} ~ {out['date'].iloc[-1]}, {len(out)} 天, "
          f"溢价率 min={out['premium'].min():.2%} max={out['premium'].max():.2%} "
          f"均值={out['premium'].mean():.2%}, >3%天数={(out['premium'] > 0.03).sum()}")
    print(f"  已保存: {path}")
    return out


if __name__ == "__main__":
    for code in (sys.argv[1:] or ["513100"]):
        fetch_premium(code)
