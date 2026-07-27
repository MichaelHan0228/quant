# -*- coding: utf-8 -*-
"""标普500 ERP 防御层回测（叠加在 v3 正式版之上）
==================================================================
信号：标普500 ERP = 盈利收益率(100/CAPE) − 美国10年期国债收益率
  CAPE（席勒市盈率）+ 美10Y：multpl.com 月度表（1871 起），
  缓存 data_long/sp500_cape.csv、data_long/us10y.csv。
规则（每月检查日，滞回两档）：
  ERP ≤ 减仓线 → 标普500 从 20% 减到低配档（钱停泊货币ETF）
  ERP ≥ 回补线 → 回到 20%
锚点（155 年全历史分位）：
  5%分位 = −2.40% / 10%分位 = −1.68% / 20%分位 = −0.45%
  默认 减仓线 = −1.7%（≈10%分位，极端贵才减）、回补线 = −0.5%（≈20%分位）。
对比：v3正式版（无防御层） vs v3+标普防御层（低配 10% / 15% 两档）。
"""
import os

import pandas as pd

import backtest_combo as bc
from backtest import metrics, yearly

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")

ERP_CFG = bc.VARIANTS_A500["combo20_erp"]


def build_sp500_erp() -> pd.Series:
    """标普500 ERP 月度序列（100/CAPE − 美10Y）"""
    cape = pd.read_csv(os.path.join(bc.DATA_LONG, "sp500_cape.csv"),
                       parse_dates=["date"]).set_index("date")["cape"]
    y10 = pd.read_csv(os.path.join(bc.DATA_LONG, "us10y.csv"),
                      parse_dates=["date"]).set_index("date")["y10"]
    df = pd.concat([cape.rename("cape"), y10.rename("y10")], axis=1,
                   join="inner").dropna().sort_index()
    erp = 100 / df["cape"] - df["y10"]
    erp.name = "erp_sp500"
    return erp


def run_sample(panel, dy_sig, signals, start, tag):
    w = bc.V3_WEIGHTS
    variants = {
        "v3正式版": None,
        "v3+标普防御(低配10%)": {"sig": signals["erp_sp500"], "cut": -1.7,
                              "restore": -0.5, "floor": 0.10},
        "v3+标普防御(低配15%)": {"sig": signals["erp_sp500"], "cut": -1.7,
                              "restore": -0.5, "floor": 0.15},
    }
    results, splogs = {}, {}
    for name, sp_cfg in variants.items():
        eq, log, alog, dlog, trades, fees, splog = bc.run_backtest_combo(
            panel, w, dy_sig, signals, ERP_CFG, start, name, sp500_cfg=sp_cfg)
        results[name] = (eq, log, fees)
        splogs[name] = splog
        if sp_cfg:
            safe = name.replace("(", "").replace(")", "").replace("+", "_").replace("%", "")
            eq.to_csv(os.path.join(OUT, f"sp500def_nav_{safe}_{tag}.csv"),
                      index=False, encoding="utf-8-sig")
            if not splog.empty:
                splog.to_csv(os.path.join(OUT, f"sp500def_decisions_{safe}_{tag}.csv"),
                             index=False, encoding="utf-8-sig")

    print("\n" + "=" * 96)
    print(f"标普500 ERP防御层对比（{start[:4]}-01 ~ {panel.index[-1].date()}，100万，{tag}）")
    print("=" * 96)
    print(f"{'版本':<22}{'总收益':>9}{'年化':>8}{'最大回撤':>9}{'夏普':>7}{'Calmar':>8}{'调仓':>5}{'费用':>7}")
    for name, (eq, log, fees) in results.items():
        m = metrics(eq)
        print(f"{name:<22}{m['total']:>8.1f}%{m['ann']:>7.2f}%{m['mdd']:>8.1f}%"
              f"{m['sharpe']:>7.2f}{m['calmar']:>8.2f}{len(log):>5}{fees:>7,.0f}")

    years = sorted({y for eq, _, _ in results.values() for y in yearly(eq)})
    ymap = {name: yearly(eq) for name, (eq, _, _) in results.items()}
    print(f"\n年度收益（{tag}）:")
    print(f"{'年份':<6}" + "".join(f"{c:>24}" for c in results))
    for y in years:
        line = f"{y:<6}"
        for name in results:
            v = ymap[name].get(y)
            line += f"{(f'{v:+.1f}%' if v is not None else '-'):>24}"
        print(line)

    for name, splog in splogs.items():
        if splog is not None and not splog.empty:
            print(f"\n标普防御层决策（{name}，{tag}）:")
            print(splog.to_string(index=False))


def main():
    print("构建信号与面板 ...")
    dy_sig = bc.build_dy_signal()
    erp_sp = build_sp500_erp()
    signals = {"erp_a500": bc.build_a500_erp_series(), "erp_sp500": erp_sp}
    print(f"  标普ERP {erp_sp.index[0].date()} ~ {erp_sp.index[-1].date()}，"
          f"最新 {erp_sp.iloc[-1]:.2f}%（减仓线 -1.7% / 回补线 -0.5%）")

    panel_s = bc.load_panel()
    panel_s[bc.A500_LEG] = bc.load_a500_series(panel_s.index)
    panel_s = panel_s[panel_s.index >= pd.Timestamp("2020-01-01")]

    panel_l = bc.build_panel_long()
    panel_l[bc.A500_LEG] = bc.load_a500_series(panel_l.index, hs300=panel_l["hs300"])
    panel_l = panel_l[panel_l.index >= pd.Timestamp("2015-01-01")]

    run_sample(panel_s, dy_sig, signals, "2020-01-01", "短样本")
    run_sample(panel_l, dy_sig, signals, "2015-01-01", "长样本")
    print(f"\n输出已保存: {OUT}")


if __name__ == "__main__":
    main()
