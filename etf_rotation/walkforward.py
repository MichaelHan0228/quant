# -*- coding: utf-8 -*-
"""Walk-forward 样本外验证（QMT 数据，2014 起）。

每年底用截至当年的全部历史做参数网格选优（样本内卡玛最高），冻结参数跑
下一自然年（样本外），逐年滚动；最后把所有样本外年份的日收益拼接成一条
"如果当年真的每年调一次参实盘运行"的净值曲线，并与当前默认参数（窗口25/
权重0.3,0.3,0.4/止损10%）同期表现对比。

网格：窗口 {20,25,30} x 权重 {(0.3,.3,.4),(1/3,1/3,1/3),(0.4,.3,.3)} x 止损 {8%,10%,12%}
固定：阈值 1.5、Top2 等权、只卖破位(stop_sell_all=False)、国债替补池。
因子矩阵因果无未来函数，每个窗口全历史只算一次，各轮复用。

用法: python walkforward.py
"""
import os
import time

import numpy as np
import pandas as pd

from backtest import run_backtest
from factors import Params, composite_scores, factor_matrices

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data_qmt")

POOL = ["512890", "159949", "513100", "518880", "159985"]
BOND = "511010"
TRAIN_START = "2014-01-01"
OOS_YEARS = list(range(2019, 2027))   # 2026 为部分年（数据至 2026-08-12）

WINDOWS = [20, 25, 30]
WEIGHTS = [(0.3, 0.3, 0.4), (1 / 3, 1 / 3, 1 / 3), (0.4, 0.3, 0.3)]
STOPS = [0.10, 0.12, 0.14]   # 12~15% 为参数平台（2014 起回测验证），8% 已确认过紧
THRESHOLD = 1.5
TOP_K = 2

DEFAULT = (25, (0.3, 0.3, 0.4), 0.13)  # 当前默认配置，作对比基准


def load_panel(codes) -> dict:
    panel = {}
    for code in codes:
        df = pd.read_csv(os.path.join(DATA_DIR, f"{code}.csv"), dtype={"date": str})
        panel[code] = df.set_index("date")
    return panel


def perf(nav: pd.Series):
    n = len(nav)
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (252 / n) - 1 if n > 1 else 0.0
    dd = (nav / nav.cummax() - 1).min()
    ret = nav.pct_change().dropna()
    sharpe = ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else 0.0
    return ann, dd, sharpe


def main():
    t_start = time.time()
    print("加载 QMT 数据...", flush=True)
    panel = load_panel(POOL + [BOND])
    union = None
    for df in panel.values():
        union = df.index if union is None else union.union(df.index)
    dates = union.sort_values()

    # 因子矩阵按窗口缓存（因果：t 日因子只依赖 t 日及之前数据，无未来函数）
    fm_cache = {}
    for w in WINDOWS:
        t0 = time.time()
        fm_cache[w] = factor_matrices(panel, dates, Params.with_window(w))
        print(f"窗口 {w} 因子矩阵完成 ({time.time() - t0:.0f}s)", flush=True)

    def masked_scores(window, weights):
        s = composite_scores(fm_cache[window], weights)
        full = s[POOL].notna().all(axis=1)
        s.loc[full, BOND] = np.nan
        return s

    def make_params(w, wt, sp):
        return Params.with_window(w, weights=wt, threshold=THRESHOLD,
                                  top_k=TOP_K, stop_pct=sp, stop_sell_all=False)

    results = []
    oos_rets = []
    for Y in OOS_YEARS:
        train_end = f"{Y - 1}-12-31"
        # ---- 样本内选参（卡玛最高，年化收益做平手裁决）----
        best = None
        for w in WINDOWS:
            for wt in WEIGHTS:
                s_tr = masked_scores(w, wt).loc[:train_end]
                for sp in STOPS:
                    res = run_backtest(s_tr, panel, make_params(w, wt, sp),
                                       start=TRAIN_START)
                    ann, dd, _ = perf(res["nav"])
                    calmar = ann / abs(dd) if dd < 0 else np.inf
                    if best is None or (calmar, ann) > best[0]:
                        best = ((calmar, ann), (w, wt, sp))
        w, wt, sp = best[1]
        # ---- 冻结参数，跑样本外年份 ----
        res = run_backtest(masked_scores(w, wt), panel, make_params(w, wt, sp),
                           start=TRAIN_START)
        nav = res["nav"]
        rets = nav.pct_change().loc[f"{Y}-01-01":f"{Y}-12-31"].dropna()
        oos_rets.append(rets)
        oos_ret = (1 + rets).prod() - 1
        results.append({"year": Y, "window": w, "weights": wt, "stop": sp,
                        "is_calmar": best[0][0], "oos_ret": oos_ret})
        print(f"OOS {Y}: 最优 窗口{w} 权重{tuple(round(x,3) for x in wt)} 止损{sp:.0%} "
              f"(样本内卡玛 {best[0][0]:.2f}) → 样本外收益 {oos_ret:.2%}", flush=True)

    # ---- 拼接样本外净值 ----
    all_rets = pd.concat(oos_rets)
    stitched = (1 + all_rets).cumprod()
    ann, dd, sharpe = perf(stitched)

    # ---- 对比基准：默认参数同期表现 ----
    res_def = run_backtest(masked_scores(*DEFAULT[:2], ), panel,
                           make_params(*DEFAULT), start=TRAIN_START)
    nav_def = res_def["nav"]
    rets_def = nav_def.pct_change().loc["2019-01-01":].dropna()
    base = (1 + rets_def).cumprod()
    ann_b, dd_b, sharpe_b = perf(base)

    print("\n========== Walk-forward 结果汇总 ==========")
    df = pd.DataFrame(results)
    df["weights"] = df["weights"].apply(lambda x: "/".join(f"{v:.2f}" for v in x))
    print(df.to_string(index=False))
    print(f"\n样本外拼接 (2019~2026-08):")
    print(f"  年化 {ann:.2%} | 最大回撤 {dd:.2%} | 夏普 {sharpe:.2f} | 卡玛 {ann/abs(dd) if dd<0 else float('inf'):.2f}")
    print(f"默认参数同期 (2019~2026-08):")
    print(f"  年化 {ann_b:.2%} | 最大回撤 {dd_b:.2%} | 夏普 {sharpe_b:.2f} | 卡玛 {ann_b/abs(dd_b) if dd_b<0 else float('inf'):.2f}")
    print(f"样本外/样本内(默认参数全样本年化见此前回测 20.42%) 衰减: "
          f"{ann / 0.2042:.0%}" if ann > 0 else "")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.sans-serif"] = ["SimHei", "Microsoft YaHei"]
    plt.rcParams["axes.unicode_minus"] = False
    fig, ax = plt.subplots(figsize=(12, 6))
    stitched.plot(ax=ax, label="walk-forward OOS (yearly re-optimized)")
    base.plot(ax=ax, label="default params (in-sample chosen)")
    ax.set_title("Walk-forward out-of-sample vs default params (2019~2026-08)")
    ax.legend()
    ax.grid(alpha=0.3)
    out = os.path.join(BASE, "output", "walkforward_oos.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out, dpi=120, bbox_inches="tight")
    print(f"\n净值对比图: {out}")
    print(f"总耗时 {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()
