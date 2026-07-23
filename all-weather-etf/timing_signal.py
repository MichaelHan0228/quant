"""
股债性价比(ERP)择时信号
========================
ERP = 沪深300盈利收益率(1/PE_TTM) − 10年期国债收益率

分位数：当前 ERP 在过去 5 年滚动窗口中的分位（利率中枢下移，不能用全历史比绝对值）
档位（作用于全天候股票腿 hlb，债券腿 bond10 反向对冲）：
    分位 ≥80%   → +10pp   股票极便宜
    60%~80%     → +5pp
    40%~60%     →  0      中性
    20%~40%     → −5pp
    <20%        → −10pp   股票极贵

数据源（均免费、无 key）：
  - 沪深300 PE_TTM 日频：中证指数官网 index-perf（返回字段 peg 实为市盈率）
  - 10年期国债收益率日频：东财 datacenter 中债国债收益率曲线 RPTA_WEB_TREASURYYIELD

用法：
    python timing_signal.py            # 拉数/读缓存，输出最新信号与档位
    from timing_signal import build_signal
"""
import os
import time
import random

import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
OUT = os.path.join(BASE, "output")
os.makedirs(DATA, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

PE_FILE = os.path.join(DATA, "csi300_pe.csv")
Y10_FILE = os.path.join(DATA, "cgb10y.csv")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
HIST_START = "2014-01-01"     # 2020 回测起点前留足 5 年滚动窗口（2015 起回测用 2009-01-01）
ROLL_WINDOW = 1250            # 5 年 ≈ 1250 个交易日
MIN_OBS = 750                 # 窗口不足 3 年不出信号（视为中性）

BANDS = [(0.80, 0.10), (0.60, 0.05), (0.40, 0.0), (0.20, -0.05), (0.0, -0.10)]


# ── 数据拉取 ─────────────────────────────────────────────────────────────

def fetch_csi300_pe(force: bool = False, hist_start: str = HIST_START) -> pd.Series:
    """沪深300 PE_TTM 日频序列（中证指数官网）。失败时回退本地缓存。"""
    if not force and os.path.exists(PE_FILE):
        df = pd.read_csv(PE_FILE, parse_dates=["date"])
        if (pd.Timestamp.today() - df["date"].max()).days <= 7 and \
                df["date"].min() <= pd.Timestamp(hist_start) + pd.Timedelta(days=31):
            return df.set_index("date")["pe"]
    try:
        r = requests.get(
            "https://www.csindex.com.cn/csindex-home/perf/index-perf",
            params={"indexCode": "000300",
                    "startDate": hist_start.replace("-", ""),
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
        return out.set_index("date")["pe"]
    except Exception as e:
        if os.path.exists(PE_FILE):
            print(f"[WARN] 中证指数拉取失败({e})，使用本地缓存 {PE_FILE}")
            df = pd.read_csv(PE_FILE, parse_dates=["date"])
            return df.set_index("date")["pe"]
        raise


def fetch_cgb10y(force: bool = False, hist_start: str = HIST_START) -> pd.Series:
    """10年期国债收益率日频序列（东财中债收益率曲线，EMM00166466=10年）。失败时回退本地缓存。"""
    if not force and os.path.exists(Y10_FILE):
        df = pd.read_csv(Y10_FILE, parse_dates=["date"])
        if (pd.Timestamp.today() - df["date"].max()).days <= 7 and \
                df["date"].min() <= pd.Timestamp(hist_start) + pd.Timedelta(days=31):
            return df.set_index("date")["y10"]
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA})
    try:
        rows = []
        for page in range(1, 30):
            r = sess.get(url, params={
                "reportName": "RPTA_WEB_TREASURYYIELD", "columns": "ALL",
                "filter": f"(SOLAR_DATE>='{hist_start}')",
                "pageNumber": str(page), "pageSize": "500",
                "sortColumns": "SOLAR_DATE", "sortTypes": "-1",
                "source": "WEB", "client": "WEB"}, timeout=30)
            data = (r.json().get("result") or {}).get("data") or []
            if not data:
                break
            rows.extend(data)
            time.sleep(1.0 + random.uniform(0.1, 0.5))   # 东财防封：串行限流
        if not rows:
            raise RuntimeError("东财收益率曲线返回空数据")
        df = pd.DataFrame(rows)
        out = df[["SOLAR_DATE", "EMM00166466"]].rename(
            columns={"SOLAR_DATE": "date", "EMM00166466": "y10"})
        out["date"] = pd.to_datetime(out["date"])
        out = out.dropna().drop_duplicates("date").sort_values("date")
        out.to_csv(Y10_FILE, index=False)
        return out.set_index("date")["y10"]
    except Exception as e:
        if os.path.exists(Y10_FILE):
            print(f"[WARN] 东财收益率拉取失败({e})，使用本地缓存 {Y10_FILE}")
            df = pd.read_csv(Y10_FILE, parse_dates=["date"])
            return df.set_index("date")["y10"]
        raise


# ── 信号计算 ─────────────────────────────────────────────────────────────

def band_adjust(pct: float) -> float:
    """ERP 分位 → 股票腿权重调整量"""
    if pd.isna(pct):
        return 0.0
    for thr, adj in BANDS:
        if pct >= thr:
            return adj
    return -0.10


def build_signal(force: bool = False, hist_start: str = HIST_START) -> pd.DataFrame:
    """构建完整信号表：date, pe, y10, erp, pct(5年滚动分位), adjust"""
    pe = fetch_csi300_pe(force, hist_start)
    y10 = fetch_cgb10y(force, hist_start)
    # 国债收益率对齐到指数交易日（收益率工作日更密，前向填充）
    y10r = y10.reindex(pe.index.union(y10.index)).ffill().reindex(pe.index)
    df = pd.DataFrame({"pe": pe, "y10": y10r}).dropna()
    df["erp"] = 100.0 / df["pe"] - df["y10"]
    df["pct"] = df["erp"].rolling(ROLL_WINDOW, min_periods=MIN_OBS).apply(
        lambda x: (x <= x[-1]).mean(), raw=True)
    df["adjust"] = df["pct"].map(band_adjust)
    return df


def latest_signal(df: pd.DataFrame, date=None) -> dict:
    """截至 date 的最新信号（date=None 取最后一天）"""
    sub = df[df.index <= pd.Timestamp(date)] if date is not None else df
    row = sub.iloc[-1]
    return {"date": sub.index[-1], "pe": row["pe"], "y10": row["y10"],
            "erp": row["erp"], "pct": row["pct"], "adjust": row["adjust"]}


def main():
    df = build_signal()
    df.to_csv(os.path.join(OUT, "timing_signal.csv"), encoding="utf-8-sig",
              index_label="date", float_format="%.4f")
    s = latest_signal(df)
    print(f"信号区间: {df.index[0].date()} ~ {df.index[-1].date()}, {len(df)} 个交易日")
    print(f"\n最新信号({s['date'].date()}):")
    print(f"  沪深300 PE(TTM) = {s['pe']:.2f}")
    print(f"  10年国债收益率  = {s['y10']:.3f}%")
    print(f"  ERP             = {s['erp']:.3f}%  (盈利收益率−10Y)")
    print(f"  5年滚动分位     = {s['pct']:.1%}")
    print(f"  股票腿调整      = {s['adjust']:+.0%}")
    print(f"\n已保存: {os.path.join(OUT, 'timing_signal.csv')}")


if __name__ == "__main__":
    main()
