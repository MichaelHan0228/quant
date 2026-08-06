"""
v3 增强实验：国债腿 10% 下限 + 政金债替换（2015 长样本）
============================================================
变体：
  a) v3 原版（floor=0，511260）
  b) v3 + 国债下限10%（A500 凑钱不卖穿对冲引擎）
  c) v3 + 政金债（511520 替换 511260，同久期票息 +0.2%/年）
  d) v3 + 下限 + 政金债

政金债序列口径：
  511520 真实行情仅 2023-04-12 起；之前用 panel 的 bond10(511260) 日收益
  + 0.002/年 票息超额反推拼接（超额系数实测自 2023-04 ~ 2026-07 重叠期：
  511520 年化超 511260 0.20%）。回测后期为真实行情，前期为重建近似。
"""
import os

import pandas as pd
import requests

import backtest_combo as bc
from backtest import metrics, yearly

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
DATA_LONG = os.path.join(BASE, "data_long")
UA = "Mozilla/5.0"
UPLIFT = 0.0020          # 政金债相对国债的年化票息超额（重叠期实测）
PZH_LISTING = pd.Timestamp("2023-04-12")


def fetch_511520() -> pd.Series:
    cache = os.path.join(DATA_LONG, "511520.csv")
    if os.path.exists(cache):
        df = pd.read_csv(cache, parse_dates=["date"])
        if (pd.Timestamp.today() - df["date"].max()).days <= 7:
            return df.set_index("date")["close"]
    param = "sh511520,day,2023-01-01,2026-12-31,800,qfq"
    r = requests.get(f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param={param}",
                     headers={"User-Agent": UA}, timeout=20)
    rows = r.json()["data"]["sh511520"]
    rows = rows.get("qfqday") or rows.get("day") or []
    df = pd.DataFrame(rows).iloc[:, :2]
    df.columns = ["date", "close"]
    df["date"] = pd.to_datetime(df["date"])
    df["close"] = df["close"].astype(float)
    df.to_csv(cache, index=False)
    return df.set_index("date")["close"]


def make_pzj_series(panel: pd.DataFrame) -> pd.Series:
    """政金债腿：真实 511520（2023-04 起） + bond10 收益+票息超额 反推（之前）"""
    real = fetch_511520()
    b10 = panel["bond10"]
    ret = b10.pct_change() + UPLIFT / 252
    anchor_date = real.index[0]
    anchor_px = real.iloc[0]
    pre = ret[ret.index < anchor_date].dropna()
    px = anchor_px
    vals = {}
    for dt in reversed(pre.index):
        px = px / (1.0 + pre.loc[dt])
        vals[dt] = px
    s = pd.concat([pd.Series(vals).sort_index(), real])
    return s.reindex(panel.index).ffill().bfill()


def run(panel, dy_sig, signals, floor, label):
    bc.BOND_FLOOR = floor
    return bc.run_backtest_combo(panel, bc.V3_WEIGHTS, dy_sig, signals,
                                 bc.VARIANTS_A500["combo20_erp"], "2015-01-01", label)


def main():
    print("构建信号与长样本面板 ...")
    dy_sig = bc.build_dy_signal()
    signals = {"a500": bc.load_a500_signal(), "s300": bc.build_hs300_signal(),
               "pe_a500": bc.build_a500_pe_series(), "erp_a500": bc.build_a500_erp_series()}
    panel_l = bc.build_panel_long()
    panel_l[bc.A500_LEG] = bc.load_a500_series(panel_l.index, hs300=panel_l["hs300"])
    panel_l = panel_l[panel_l.index >= pd.Timestamp("2015-01-01")]

    print("构建政金债序列 ...")
    pzj = make_pzj_series(panel_l)
    panel_p = panel_l.copy()
    panel_p["bond10"] = pzj

    variants = [
        ("v3原版", panel_l, 0.0),
        ("v3+国债下限10%", panel_l, 0.10),
        ("v3+政金债", panel_p, 0.0),
        ("v3+下限+政金债", panel_p, 0.10),
    ]
    rows = []
    yearly_map = {}
    for tag, panel, floor in variants:
        eq, log, alog, dlog, trades, fees, _ = run(panel, dy_sig, signals, floor, tag)
        m = metrics(eq)
        rows.append({"变体": tag, "总收益%": round(m["total"], 1), "年化%": round(m["ann"], 2),
                     "回撤%": round(m["mdd"], 1), "夏普": round(m["sharpe"], 2),
                     "Calmar": round(m["calmar"], 2), "调仓": len(log), "费用": round(fees)})
        yearly_map[tag] = yearly(eq)
        eq.to_csv(os.path.join(OUT, f"nav_v3enh_{tag}.csv"), index=False, encoding="utf-8-sig")
        print(f"{tag}: 年化{m['ann']:.2f}% 回撤{m['mdd']:.1f}% 夏普{m['sharpe']:.2f} "
              f"Calmar{m['calmar']:.2f} 费用{fees:.0f}", flush=True)

    print("\n" + "=" * 80)
    print("v3 增强实验（2015-01 ~ 2026-07，100万）")
    print("=" * 80)
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n年度收益:")
    years = sorted({y for m in yearly_map.values() for y in m})
    print(f"{'年份':<6}" + "".join(f"{t:>18}" for t in yearly_map))
    for y in years:
        line = f"{y:<6}"
        for t in yearly_map:
            v = yearly_map[t].get(y)
            line += f"{(f'{v:+.1f}%' if v is not None else '-'):>18}"
        print(line)


if __name__ == "__main__":
    main()
