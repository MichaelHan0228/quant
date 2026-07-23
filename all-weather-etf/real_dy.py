# -*- coding: utf-8 -*-
"""真实股息率推导：中证全收益/价格反解日分红（替代 PE 镜像）
============================================================
原理：全收益指数 = 价格指数 × 累计分红再投因子，两指数日收益差即当日分红：
    div_t = PX_{t-1} × (TR_t/TR_{t-1} − PX_t/PX_{t-1})
    DY_t  = Σ div_{t-249..t} / PX_t         —— TTM分红 ÷ 当下价格

口径说明：分母是当下价格，价格暴涨时 DY 立刻下降，无滞后。
  旧版曾用 (TR_t/TR_{t-250})/(PX_t/PX_{t-250})−1（分红累积增速，隐含分母是
  一年前的价格）：分母滞后一年，2015 泡沫顶 DY 虚高、信号满仓进股灾，已废弃
  （2026-07-23 修正为日分红反解口径）。

数据：csindex index-perf 同一端点，H20269=红利低波全收益、H30269=红利低波价格。
  均免费无 key。缓存 data_long/h20269_tr.csv、h30269_px.csv（7天新鲜复用）。

用途：timing_signal_dy 的真实 DY 信号源，替代 "DY分位 ≈ 1−PE分位" 镜像假设。
"""
import os

import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_LONG = os.path.join(BASE, "data_long")
OUT = os.path.join(BASE, "output")
os.makedirs(DATA_LONG, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HIST_START = "2009-01-01"
DY_WINDOW = 250          # 滚动一年（交易日）


def _fresh(path: str, hist_start: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        head = pd.read_csv(path, parse_dates=["date"], nrows=3)
        last = pd.read_csv(path).iloc[-1]["date"]
        return (pd.Timestamp.today() - pd.Timestamp(last)).days <= 7 and \
            head["date"].min() <= pd.Timestamp(hist_start) + pd.Timedelta(days=40)
    except Exception:
        return False


def fetch_csindex_close(index_code: str, cache_name: str,
                        hist_start: str = HIST_START) -> pd.Series:
    """csindex index-perf 收盘价日频（tradeDate + close）。失败回退缓存。"""
    cache = os.path.join(DATA_LONG, cache_name)
    if _fresh(cache, hist_start):
        df = pd.read_csv(cache, parse_dates=["date"])
        return df.set_index("date")["close"]
    try:
        r = requests.get(
            "https://www.csindex.com.cn/csindex-home/perf/index-perf",
            params={"indexCode": index_code,
                    "startDate": hist_start.replace("-", ""),
                    "endDate": pd.Timestamp.today().strftime("%Y%m%d")},
            headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"},
            timeout=120)
        rows = r.json().get("data") or []
        if not rows:
            raise RuntimeError(f"csindex {index_code} 返回空数据")
        df = pd.DataFrame(rows)
        out = df[["tradeDate", "close"]].rename(columns={"tradeDate": "date"})
        out["date"] = pd.to_datetime(out["date"], format="%Y%m%d")
        out = out.dropna().drop_duplicates("date").sort_values("date")
        out.to_csv(cache, index=False)
        return out.set_index("date")["close"]
    except Exception as e:
        if os.path.exists(cache):
            print(f"[WARN] csindex {index_code} 拉取失败({e})，使用本地缓存 {cache}")
            df = pd.read_csv(cache, parse_dates=["date"])
            return df.set_index("date")["close"]
        raise


def build_real_dy(hist_start: str = HIST_START) -> pd.DataFrame:
    """真实股息率序列：date, tr, px, div_daily, dy（TTM分红/当下价格，小数）"""
    tr = fetch_csindex_close("H20269", "h20269_tr.csv", hist_start)
    px = fetch_csindex_close("H30269", "h30269_px.csv", hist_start)
    df = pd.concat([tr.rename("tr"), px.rename("px")], axis=1, join="inner").dropna()
    # 日收益差 = 当日分红（指数点位口径）；非分红日理论上为0，收盘价两位小数
    # 会引入 ±噪声，负值按0处理（分红不可能为负）
    df["div_daily"] = (df["px"].shift(1) *
                       (df["tr"] / df["tr"].shift(1) - df["px"] / df["px"].shift(1))).clip(lower=0)
    df["dy"] = df["div_daily"].rolling(DY_WINDOW).sum() / df["px"]
    return df


def main():
    df = build_real_dy()
    df.to_csv(os.path.join(OUT, "real_dy.csv"), encoding="utf-8-sig",
              index_label="date", float_format="%.6f")
    dy = df["dy"].dropna()
    print(f"TR区间: {df.index[0].date()} 起 | PX区间: 同 | DY可用: {dy.index[0].date()} ~ {dy.index[-1].date()}")
    print(f"\n真实股息率（TTM分红/当下价格）统计:")
    print(f"  最新({dy.index[-1].date()}): {dy.iloc[-1]:.2%}")
    print(f"  全期: min {dy.min():.2%} ({dy.idxmin().date()})  max {dy.max():.2%} ({dy.idxmax().date()})  均值 {dy.mean():.2%}")
    print(f"\n关键年份年末值:")
    for y in (2015, 2018, 2021, 2022, 2024, 2025):
        sub = dy[dy.index.year == y]
        if len(sub):
            print(f"  {y} 年末: {sub.iloc[-1]:.2%}   年内区间 {sub.min():.2%} ~ {sub.max():.2%}")
    print(f"\n全历史分位（最新值所处）: {(dy <= dy.iloc[-1]).mean():.1%}")
    print(f"已保存: {os.path.join(OUT, 'real_dy.csv')}")


if __name__ == "__main__":
    main()
