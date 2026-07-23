"""
长历史价格面板（2015 起回测用）
================================
v2 数据(fetch_data.py)只覆盖 2017/2019 以后，本模块把各腿延伸到 2015：
  - hlb     红利低波：中证 H20269 全收益指数(2014-06起) 缩放接到 563020 前复权价(2023-12-14上市)
  - bond10  十年国债：511260(2017-08上市) 之前用 511010(5年国债ETF) 日收益 × 1.85 久期比 反推拼接
  - soybean 豆粕：159985(2019-12上市) 之前用 新浪豆粕期货主力连续(M0) 日收益 反推拼接
  - sp500/gold/cash/hs300：腾讯前复权日线直接覆盖(均为2013年前上市)

拼接口径说明（近似，已在 timing-plan.md 声明）：
  - 511010 跟踪5年期国债，久期约4.3；511260 跟踪10年期，久期约8.0 → 日收益 × 1.85
  - M0 主力连续含换月跳空，与豆粕ETF实际跟踪口径接近，但不含基金费用与现金管理收益
缓存: data_long/*.csv（≤7天且覆盖起点则复用）
"""
import os
import re
import json
import time

import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DATA_LONG = os.path.join(BASE, "data_long")
os.makedirs(DATA_LONG, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
FETCH_START = "2014-06-01"   # 给拼接/预热留缓冲
LISTING_563020 = pd.Timestamp("2023-12-14")
LISTING_511260 = pd.Timestamp("2017-08-24")
LISTING_159985 = pd.Timestamp("2019-12-05")
DURATION_RATIO = 1.85        # 511260久期8.0 / 511010久期4.3

CHUNKS = [("2014-06-01", "2015-12-31"), ("2016-01-01", "2017-12-31"),
          ("2018-01-01", "2019-12-31"), ("2020-01-01", "2021-12-31"),
          ("2022-01-01", "2023-12-31"), ("2024-01-01", "2026-12-31")]


def _fresh(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        df = pd.read_csv(path, parse_dates=["date"], nrows=3)
        last = pd.read_csv(path).iloc[-1]["date"]
        return (pd.Timestamp.today() - pd.Timestamp(last)).days <= 7 and \
               df["date"].min() <= pd.Timestamp(FETCH_START) + pd.Timedelta(days=40)
    except Exception:
        return False


def fetch_tencent(code: str, market: str) -> pd.DataFrame:
    """腾讯前复权日线，分块拉取拼接"""
    cache = os.path.join(DATA_LONG, f"{code}.csv")
    if _fresh(cache):
        return pd.read_csv(cache, parse_dates=["date"])
    parts = []
    for start, end in CHUNKS:
        param = f"{market}{code},day,{start},{end},800,qfq"
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        rows = (r.json()["data"].get(f"{market}{code}") or {})
        rows = rows.get("qfqday") or rows.get("day") or []
        if rows:
            df = pd.DataFrame(rows).iloc[:, :6]
            df.columns = ["date", "open", "close", "high", "low", "vol"]
            parts.append(df)
        time.sleep(0.5)
    full = pd.concat(parts).drop_duplicates("date").sort_values("date")
    full["date"] = pd.to_datetime(full["date"])
    for c in ["open", "close", "high", "low"]:
        full[c] = full[c].astype(float)
    full.to_csv(cache, index=False)
    return full


def fetch_h20269() -> pd.DataFrame:
    """中证红利低波全收益指数（csindex，2014-06 起）"""
    cache = os.path.join(DATA_LONG, "h20269.csv")
    if _fresh(cache):
        return pd.read_csv(cache, parse_dates=["date"])
    r = requests.get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                     params={"indexCode": "H20269",
                             "startDate": FETCH_START.replace("-", ""),
                             "endDate": pd.Timestamp.today().strftime("%Y%m%d")},
                     headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"},
                     timeout=120)
    rows = r.json().get("data") or []
    df = pd.DataFrame(rows)
    out = df[["tradeDate", "close"]].rename(columns={"tradeDate": "date"})
    out["date"] = pd.to_datetime(out["date"], format="%Y%m%d")
    out = out.dropna().drop_duplicates("date").sort_values("date")
    out.to_csv(cache, index=False)
    return out


def fetch_m0() -> pd.DataFrame:
    """新浪豆粕期货主力连续日线"""
    cache = os.path.join(DATA_LONG, "m0.csv")
    if _fresh(cache):
        return pd.read_csv(cache, parse_dates=["date"])
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
           "var%20_m=/InnerFuturesNewService.getDailyKLine?symbol=M0")
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Referer": "https://finance.sina.com.cn/"}, timeout=30)
    m = re.search(r"\((\[.*\])\)", r.text, re.S)
    rows = json.loads(m.group(1))
    df = pd.DataFrame(rows)
    out = df[["d", "c"]].rename(columns={"d": "date", "c": "close"})
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = out["close"].astype(float)
    out = out[out["date"] >= pd.Timestamp(FETCH_START)].sort_values("date")
    out.to_csv(cache, index=False)
    return out


def _splice_back(early_ret: pd.Series, later: pd.Series) -> pd.Series:
    """以 later 首日为锚，用 early_ret 日收益向前反推价格"""
    anchor = later.index[0]
    pre = early_ret[early_ret.index < anchor].dropna()
    price = later.iloc[0]
    vals = {}
    for dt in reversed(pre.index):
        price = price / (1.0 + pre.loc[dt])
        vals[dt] = price
    return pd.concat([pd.Series(vals).sort_index(), later])


def build_panel_long() -> pd.DataFrame:
    """构建 2014-06 起的全腿价格面板（前复权口径）"""
    # hlb：H20269 全收益 → 缩放接 563020 qfq
    tr = fetch_h20269().set_index("date")["close"]
    qfq = pd.read_csv(os.path.join(DATA, "563020_qfq.csv"),
                      parse_dates=["date"]).set_index("date")["close"]
    hlb_recon = qfq[LISTING_563020] * tr / tr[LISTING_563020]
    hlb = pd.concat([hlb_recon[hlb_recon.index < LISTING_563020], qfq])
    hlb = hlb[~hlb.index.duplicated(keep="last")].sort_index()

    # bond10：511010 收益×1.85 反推 + 511260
    b5 = fetch_tencent("511010", "sh").set_index("date")["close"]
    b10 = fetch_tencent("511260", "sh").set_index("date")["close"]
    bond10 = _splice_back(b5.pct_change() * DURATION_RATIO,
                          b10[b10.index >= LISTING_511260])

    # soybean：M0 收益反推 + 159985
    m0 = fetch_m0().set_index("date")["close"]
    soy = fetch_tencent("159985", "sz").set_index("date")["close"]
    soybean = _splice_back(m0.pct_change(), soy[soy.index >= LISTING_159985])

    panel = {"hlb": hlb, "bond10": bond10, "soybean": soybean}
    for leg, code in [("sp500", "513500"), ("gold", "518880"),
                      ("cash", "511880"), ("hs300", "510300")]:
        mkt = "sz" if code.startswith("15") else "sh"
        panel[leg] = fetch_tencent(code, mkt).set_index("date")["close"]

    idx = hlb.index
    out = pd.DataFrame(index=idx)
    for leg, s in panel.items():
        out[leg] = s.reindex(idx).ffill()
    return out.dropna(subset=["hlb", "sp500", "bond10", "gold", "soybean", "cash"])


if __name__ == "__main__":
    p = build_panel_long()
    print(f"面板: {p.index[0].date()} ~ {p.index[-1].date()}, {len(p)} 个交易日")
    print(p.tail(3).round(3).to_string())
