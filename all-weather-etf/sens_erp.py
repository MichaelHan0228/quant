# -*- coding: utf-8 -*-
"""ERP 版四线敏感性测试
==========================
基准四条线：建仓 5.2 / 满仓 5.6 / 缩仓 4.7 / 清仓 4.3（combo20_erp）。
扰动：进场两线（建仓/满仓）与出场两线（缩仓/清仓）分别 ±0.3% 组合（3×3=9 组），
另加整体 ±0.15% 两组，共 11 组，检验好成绩是规则的本事还是参数的运气。

判定：若各组年化围绕基准小幅波动且普遍优于 combo20（分位口径），规则稳健；
若只有基准组突出、一挪就崩，则基准成绩是过拟合产物。
"""
import os

import pandas as pd

import backtest_combo as bc
from backtest import VARIANTS, metrics

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")

BASE_LINES = (5.6, 5.2, 4.7, 4.3)   # (满仓, 建仓, 缩仓, 清仓)


def build_configs() -> dict:
    cfgs = {}
    for di in (-0.3, 0.0, 0.3):
        for do in (-0.3, 0.0, 0.3):
            name = f"进{di:+.1f}/出{do:+.1f}"
            cfgs[name] = (BASE_LINES[0] + di, BASE_LINES[1] + di,
                          BASE_LINES[2] + do, BASE_LINES[3] + do)
    cfgs["均匀-0.15"] = tuple(x - 0.15 for x in BASE_LINES)
    cfgs["均匀+0.15"] = tuple(x + 0.15 for x in BASE_LINES)
    return cfgs


def main():
    print("加载信号与面板 ...")
    dy_sig = bc.build_dy_signal()
    signals = {"erp_a500": bc.build_a500_erp_series()}

    panel_s = bc.load_panel()
    panel_s[bc.A500_LEG] = bc.load_a500_series(panel_s.index)
    panel_s = panel_s[panel_s.index >= pd.Timestamp("2020-01-01")]

    panel_l = bc.build_panel_long()
    panel_l[bc.A500_LEG] = bc.load_a500_series(panel_l.index, hs300=panel_l["hs300"])
    panel_l = panel_l[panel_l.index >= pd.Timestamp("2015-01-01")]

    w = VARIANTS["aggressive"]
    rows = []
    for name, lines in build_configs().items():
        cfg = {"t1": 0.10, "cap": 0.20, "kind": "abs_high", "sig": "erp_a500",
               "lines": lines, "leg": bc.A500_LEG}
        rec = {"参数": name, "满仓": lines[0], "建仓": lines[1],
               "缩仓": lines[2], "清仓": lines[3]}
        for tag, panel, start in [("短", panel_s, "2020-01-01"),
                                  ("长", panel_l, "2015-01-01")]:
            eq, log, alog, dlog, trades, fees, _splog = bc.run_backtest_combo(
                panel, w, dy_sig, signals, cfg, start, name)
            m = metrics(eq)
            rec[f"{tag}年化"] = round(m["ann"], 2)
            rec[f"{tag}回撤"] = round(m["mdd"], 1)
            rec[f"{tag}夏普"] = round(m["sharpe"], 2)
            rec[f"{tag}调仓"] = len(log)
        rows.append(rec)
        print(f"  {name}: 短{rec['短年化']}%/{rec['短回撤']}%  长{rec['长年化']}%/{rec['长回撤']}%")

    df = pd.DataFrame(rows).sort_values("长年化", ascending=False)
    df.to_csv(os.path.join(OUT, "sens_erp.csv"), index=False, encoding="utf-8-sig")

    print("\n" + "=" * 96)
    print("ERP 四线敏感性（按长样本年化排序；基准=进+0.0/出+0.0）")
    print("参照：combo20 分位口径 短年化 12.23 / 长年化 10.42")
    print("=" * 96)
    print(f"{'参数':<14}{'满仓':>5}{'建仓':>5}{'缩仓':>5}{'清仓':>5}"
          f"{'短年化':>8}{'短回撤':>8}{'长年化':>8}{'长回撤':>8}{'长夏普':>7}{'长调仓':>6}")
    for _, r in df.iterrows():
        mark = " ←基准" if r["参数"] == "进+0.0/出+0.0" else ""
        print(f"{r['参数']:<14}{r['满仓']:>5.1f}{r['建仓']:>5.1f}{r['缩仓']:>5.1f}{r['清仓']:>5.1f}"
              f"{r['短年化']:>8.2f}{r['短回撤']:>8.1f}{r['长年化']:>8.2f}{r['长回撤']:>8.1f}"
              f"{r['长夏普']:>7.2f}{r['长调仓']:>6}{mark}")
    anns = df["长年化"]
    print(f"\n长样本年化: 最高 {anns.max():.2f} / 最低 {anns.min():.2f} / "
          f"均值 {anns.mean():.2f} / 离散 {anns.max()-anns.min():.2f}pp")
    anns_s = df["短年化"]
    print(f"短样本年化: 最高 {anns_s.max():.2f} / 最低 {anns_s.min():.2f} / "
          f"均值 {anns_s.mean():.2f} / 离散 {anns_s.max()-anns_s.min():.2f}pp")
    print(f"\n输出已保存: {os.path.join(OUT, 'sens_erp.csv')}")


if __name__ == "__main__":
    main()
