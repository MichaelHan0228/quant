"""
红利低波股息率(DY)择时信号
============================
思路：DY 处于历史高位 = 红利股便宜、价值未体现 → 调高 hlb 仓位。

数据说明（重要）：
  中证指数官网不发布红利低波股息率的日频历史（估值接口无公开 API，已探测 404）。
  本信号用 PE 镜像：红利指数分红率长期稳定（40~50%），DY = 分红率/PE，
  故 **DY 分位 ≈ 1 − PE 分位**。PE 用中证官方 H30269（红利低波价格指数，2009 年起）。

两种分位口径：
  - dy_pct_5y   ：5 年滚动分位（与 ERP 版一致，主口径）
  - dy_pct_full ：全历史分位（2009 起扩展窗口；股息率无利率中枢漂移问题，作对照）

档位映射与 ERP 版相同（BANDS，来自 timing_signal）。
"""
import os

import pandas as pd
import requests

from timing_signal import UA, BANDS, ROLL_WINDOW, MIN_OBS, band_adjust

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "output")
os.makedirs(DATA, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

PE_FILE = os.path.join(DATA, "h30269_pe.csv")
HIST_START = "2009-01-01"


def fetch_h30269_pe(force: bool = False, hist_start: str = HIST_START) -> pd.Series:
    """红利低波指数 PE 日频（csindex index-perf，peg 字段即市盈率）。失败回退缓存。"""
    if not force and os.path.exists(PE_FILE):
        df = pd.read_csv(PE_FILE, parse_dates=["date"])
        if (pd.Timestamp.today() - df["date"].max()).days <= 7 and \
                df["date"].min() <= pd.Timestamp(hist_start) + pd.Timedelta(days=31):
            return df.set_index("date")["pe"]
    try:
        r = requests.get(
            "https://www.csindex.com.cn/csindex-home/perf/index-perf",
            params={"indexCode": "H30269",
                    "startDate": hist_start.replace("-", ""),
                    "endDate": pd.Timestamp.today().strftime("%Y%m%d")},
            headers={"User-Agent": UA, "Referer": "https://www.csindex.com.cn/"},
            timeout=120)
        rows = r.json().get("data") or []
        if not rows:
            raise RuntimeError("csindex 返回空数据")
        df = pd.DataFrame(rows)
        out = df[["tradeDate", "peg"]].rename(columns={"tradeDate": "date", "peg": "pe"})
        out["date"] = pd.to_datetime(out["date"], format="%Y%m%d")
        out = out.dropna().drop_duplicates("date").sort_values("date")
        out.to_csv(PE_FILE, index=False)
        return out.set_index("date")["pe"]
    except Exception as e:
        if os.path.exists(PE_FILE):
            print(f"[WARN] H30269 PE 拉取失败({e})，使用本地缓存 {PE_FILE}")
            df = pd.read_csv(PE_FILE, parse_dates=["date"])
            return df.set_index("date")["pe"]
        raise


def build_signal_dy(force: bool = False, hist_start: str = HIST_START) -> pd.DataFrame:
    """DY 信号表：pe, dy_pct_5y, dy_pct_full, adjust_5y, adjust_full"""
    pe = fetch_h30269_pe(force, hist_start)
    df = pd.DataFrame({"pe": pe}).dropna()
    pe_pct_5y = df["pe"].rolling(ROLL_WINDOW, min_periods=MIN_OBS).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)
    pe_pct_full = df["pe"].expanding(min_periods=250).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)
    df["dy_pct_5y"] = 1.0 - pe_pct_5y        # 分红率稳定 → DY 分位 ≈ 1 − PE 分位
    df["dy_pct_full"] = 1.0 - pe_pct_full
    df["adjust_5y"] = df["dy_pct_5y"].map(band_adjust)
    df["adjust_full"] = df["dy_pct_full"].map(band_adjust)
    return df


def main():
    df = build_signal_dy()
    df.to_csv(os.path.join(OUT, "timing_signal_dy.csv"), encoding="utf-8-sig",
              index_label="date", float_format="%.4f")
    row = df.iloc[-1]
    print(f"信号区间: {df.index[0].date()} ~ {df.index[-1].date()}, {len(df)} 个交易日")
    print(f"\n最新信号({df.index[-1].date()}):")
    print(f"  红利低波 PE      = {row['pe']:.2f}")
    print(f"  DY 5年滚动分位   = {row['dy_pct_5y']:.1%}  → hlb {row['adjust_5y']:+.0%}")
    print(f"  DY 全历史分位    = {row['dy_pct_full']:.1%}  → hlb {row['adjust_full']:+.0%}")
    print(f"\n已保存: {os.path.join(OUT, 'timing_signal_dy.csv')}")


if __name__ == "__main__":
    main()
