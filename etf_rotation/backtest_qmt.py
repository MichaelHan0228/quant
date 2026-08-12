# -*- coding: utf-8 -*-
"""2014 年起长周期回测（QMT 数据源）—— 标的未上市时用国债ETF(511010)替补。

规则：候选池 = 已上市且过 warmup 的策略池标的；只要 5 只标的有任何一只
尚未进入候选，511010 就加入候选池参与轮动；5 只全部就绪后 511010 立刻退出
（次日起评分置 NaN，自然触发换仓）。同时跑「无替补动态池」作对照。

用法: python backtest_qmt.py          （默认 python3.14，需 pandas/sklearn）
"""
import os
from dataclasses import replace

import numpy as np
import pandas as pd

from backtest import run_backtest
from factors import Params, composite_scores, factor_matrices
from report import print_metrics

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data_qmt")

POOL = ["512890", "159949", "513100", "518880", "159985"]
BOND = "511010"
START = "2014-01-01"


def load_panel(codes) -> dict:
    panel = {}
    for code in codes:
        df = pd.read_csv(os.path.join(DATA_DIR, f"{code}.csv"), dtype={"date": str})
        panel[code] = df.set_index("date")
        print(f"  {code}: {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}, {len(df)} 根")
    return panel


def main():
    params = Params.with_window(
        25, weights=(0.3, 0.3, 0.4), threshold=1.5, top_k=2, stop_pct=0.10)

    print("加载 QMT 数据...")
    panel = load_panel(POOL + [BOND])
    union = None
    for df in panel.values():
        union = df.index if union is None else union.union(df.index)
    dates = union.sort_values()

    print("计算因子...")
    fm = factor_matrices(panel, dates, params)
    scores = composite_scores(fm, params.weights)

    # 方案A：国债替补 —— 5 只标的未全部就绪期间，511010 参与轮动
    scores_a = scores.copy()
    full_ready = scores_a[POOL].notna().all(axis=1)
    first_full = full_ready[full_ready].index[0]
    scores_a.loc[full_ready, BOND] = np.nan
    print(f"\n5 只标的全部就绪日: {first_full}（次日起国债ETF退出候选池）")

    print("\n========== 方案A：国债ETF替补（2014 起） ==========")
    res_a = run_backtest(scores_a, panel, params, start=START)
    print_metrics(res_a["metrics"])

    # 方案C：国债替补 + 止损只卖破位标的（不连坐清空）
    print("\n========== 方案C：国债替补 + 只卖破位标的 ==========")
    params_c = replace(params, stop_sell_all=False)
    res_c = run_backtest(scores_a, panel, params_c, start=START)
    print_metrics(res_c["metrics"])

    # 方案B：无替补动态池（原版逻辑，2014 起只有 2 只标的可轮）
    print("\n========== 方案B：无替补动态池（对照） ==========")
    res_b = run_backtest(scores[POOL], panel, params, start=START)
    print_metrics(res_b["metrics"])

    # 净值对比图
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(12, 6))
    res_a["nav"].plot(ax=ax, label="A: bond stand-in (stop sells all)")
    res_c["nav"].plot(ax=ax, label="C: bond stand-in + stop sells broken only")
    res_b["nav"].plot(ax=ax, label="B: dynamic pool only", alpha=0.6)
    ax.set_title("2014~ QMT backtest: stop-loss scope comparison")
    ax.legend()
    ax.grid(alpha=0.3)
    out = os.path.join(BASE, "output", "qmt_2014_bond_substitute.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out, dpi=120, bbox_inches="tight")
    print(f"\n净值对比图: {out}")

    # 方案A 早期调仓记录（验证替补/换出行为）
    tr = res_a["trades"]
    early = tr[tr["date"] <= "2020-06-30"]
    print(f"\n方案A 2020-07 前的调仓（含国债替补期）: 共 {len(early)} 笔")
    print(early.to_string(index=False))


if __name__ == "__main__":
    main()
