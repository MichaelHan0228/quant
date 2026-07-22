"""
全天候策略数据下载（miniQMT 版）
================================
替代 fetch_data.py：ETF 日线改走本地 miniQMT（xtquant），解决两个问题：
  1. 腾讯接口单次 800 根上限导致长区间静默截断（2023-01~2026-12 段约 866 根）
  2. fetch_data.py 里 DIVIDEND_PROJ 写死 D:/研究/quant 路径，全新机器拉不了

数据源：
  - ETF 日线（前复权/不复权）：miniQMT 本地行情服务（需先打开 miniQMT 并登录）
  - H30269 / H20269 中证红利低波指数：券商 QMT 未订阅中证全收益指数，
    保留中证指数官网接口（与 dividend_dca_backtest 相同口径）

运行环境（重要）：
  xtquant 官方只适配到 Python 3.13（无 cp314 编译件），本机请用：
    "D:/code_tool_packages/tools/python313/python.exe" fetch_data_qmt.py
  依赖: pip install xtquant pandas requests（py3.13 环境）

输出: data/*.csv  列: date,open,close,high,low,vol（与腾讯版 schema 一致，backtest.py 直接可用）
"""
import os
import time
import datetime as dt
import requests
import pandas as pd
from xtquant import xtdata

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
os.makedirs(DATA_DIR, exist_ok=True)

START = "20140101"   # 2014 起：覆盖 513500/518880/511010/511880 最早上市日，供 2015 延长回测使用
TODAY = dt.date.today().strftime("%Y%m%d")

# 代码 -> QMT 代码（.SH/.SZ）。511010 仅作 60/40 基准备用，510300 为沪深300基准
ETFS = {
    "513500": "513500.SH",   # 标普500ETF (QDII)
    "518880": "518880.SH",   # 黄金ETF
    "511010": "511010.SH",   # 国债ETF(5年，基准备用)
    "511260": "511260.SH",   # 十年国债ETF
    "159985": "159985.SZ",   # 豆粕ETF
    "511880": "511880.SH",   # 货币ETF
    "510300": "510300.SH",   # 沪深300ETF（基准）
}

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.csindex.com.cn/"}


def fetch_etf_qmt(qmt_code: str, dividend_type: str = "front") -> pd.DataFrame:
    """miniQMT 日线。dividend_type: front=前复权, none=不复权"""
    xtdata.download_history_data(qmt_code, period="1d", start_time=START, end_time=TODAY)
    d = xtdata.get_market_data_ex(
        [], [qmt_code], period="1d", start_time=START, end_time=TODAY,
        dividend_type=dividend_type,
    )
    df = d[qmt_code]
    if df.empty:
        raise RuntimeError(f"{qmt_code} 无数据（dividend_type={dividend_type}），miniQMT 是否在运行？")
    # 交易日以行索引(yyyyMMdd)为准：time 列在不同时段/品种上时区口径不一致，不能直接用
    out = pd.DataFrame({
        "date": pd.to_datetime(df.index.astype(str), format="%Y%m%d"),
        "open": df["open"].astype(float).values,
        "close": df["close"].astype(float).values,
        "high": df["high"].astype(float).values,
        "low": df["low"].astype(float).values,
        "vol": df["volume"].astype(float).values,
    })
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def fetch_csindex(code: str, start: str = "20150101") -> pd.DataFrame:
    """中证指数官网日线（H30269 价格指数 / H20269 全收益），2015 起供延长回测"""
    url = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
    params = {"indexCode": code, "startDate": start, "endDate": TODAY}
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    rows = r.json().get("data") or []
    if not rows:
        raise RuntimeError(f"中证指数官网 {code} 返回空")
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["tradeDate"], format="%Y%m%d")
    df = df[["date", "open", "high", "low", "close"]].sort_values("date").reset_index(drop=True)
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    return df


def check_integrity(name: str, df: pd.DataFrame, expect_start: str):
    """完整性检查：行数、起止日期、最大相邻间隔；异常打印 WARNING"""
    first, last = df["date"].iloc[0], df["date"].iloc[-1]
    gaps = df["date"].diff().dt.days.dropna()
    max_gap = int(gaps.max()) if len(gaps) else 0
    gap_date = df["date"].iloc[int(gaps.idxmax())] if max_gap > 0 else None
    msg = f"  {name}: {len(df)} 行, {first.date()} ~ {last.date()}, 最大间隔 {max_gap} 天"
    warns = []
    if str(first.date()) > expect_start:
        warns.append(f"起点晚于 {expect_start}")
    if max_gap > 15:   # 春节/国庆正常休市最长约11个自然日（2020疫情休市11天）
        warns.append(f"{gap_date.date()} 前有 {max_gap} 天缺口（疑似数据缺失）")
    if (dt.datetime.now() - last).days > 7:
        warns.append("末尾数据超过 7 天未更新")
    print(msg + ("  <<< WARNING: " + "; ".join(warns) if warns else ""))


def main():
    print(f"下载区间 {START} ~ {TODAY}（miniQMT 日线）")
    for code, qmt_code in ETFS.items():
        df = fetch_etf_qmt(qmt_code, "front")
        df.to_csv(os.path.join(DATA_DIR, f"{code}.csv"), index=False, encoding="utf-8-sig")
        # 各品种上市日不同：513500 于 2014-01-15、511260 于 2017-08-24、159985 于 2019-12-05 上市
        expect = {"513500": "2014-01-15", "511260": "2017-08-24", "159985": "2019-12-05"}.get(code, "2014-01-03")
        check_integrity(code, df, expect)
        time.sleep(0.2)

    # 563020 红利低波ETF：前复权 + 不复权都存（backtest 用 qfq，校验用 raw）
    qfq = fetch_etf_qmt("563020.SH", "front")
    qfq.to_csv(os.path.join(DATA_DIR, "563020_qfq.csv"), index=False, encoding="utf-8-sig")
    check_integrity("563020_qfq", qfq, "2023-12-14")
    raw = fetch_etf_qmt("563020.SH", "none")
    raw.to_csv(os.path.join(DATA_DIR, "563020_raw.csv"), index=False, encoding="utf-8-sig")
    check_integrity("563020_raw", raw, "2023-12-14")

    # 中证红利低波指数：QMT 无此指数，走中证指数官网
    for idx in ("H30269", "H20269"):
        df = fetch_csindex(idx)
        df.to_csv(os.path.join(DATA_DIR, f"{idx.lower()}.csv"), index=False, encoding="utf-8-sig")
        check_integrity(idx, df, "2015-01-10")
        time.sleep(1)

    # 数据校验：指数 vs ETF 重叠段偏差 + 隐含分红率（与 dividend 项目同口径）
    h30269 = pd.read_csv(os.path.join(DATA_DIR, "h30269.csv"), parse_dates=["date"])
    m = pd.merge(h30269[["date", "close"]].rename(columns={"close": "idx"}),
                 raw[["date", "close"]].rename(columns={"close": "etf"}), on="date")
    m["ratio"] = m["idx"] / m["etf"]
    print(f"\n重叠段 {m['date'].iloc[0].date()} ~ {m['date'].iloc[-1].date()}: "
          f"指数/ETF 比值 首 {m['ratio'].iloc[0]:.1f} 末 {m['ratio'].iloc[-1]:.1f} "
          f"波动 {m['ratio'].std() / m['ratio'].mean() * 100:.2f}%")
    h20269 = pd.read_csv(os.path.join(DATA_DIR, "h20269.csv"), parse_dates=["date"])
    tr = pd.merge(pd.read_csv(os.path.join(DATA_DIR, "h30269.csv"), parse_dates=["date"])[["date", "close"]].rename(columns={"close": "pr"}),
                  h20269[["date", "close"]].rename(columns={"close": "tr"}), on="date")
    tr["dr"] = tr["tr"] / tr["pr"]
    tr["year"] = tr["date"].dt.year
    for y, g in tr.groupby("year"):
        if y < 2020:
            continue
        yld = (g["dr"].iloc[-1] / g["dr"].iloc[0] - 1) * 100
        print(f"  {y} 隐含分红收益率: {yld:.2f}%")
    print(f"\n完成，输出目录: {DATA_DIR}")


if __name__ == "__main__":
    main()
