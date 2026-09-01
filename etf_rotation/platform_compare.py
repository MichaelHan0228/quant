"""平台止损三方案 vs 纯浮盈收紧：净值/回撤曲线对比图 + 逐年收益表。

数据走 CSV 缓存，因子矩阵只计算一次，四个配置共用评分。

用法: python platform_compare.py
输出: output/platform_nav_compare.png + 终端逐年收益表
"""
from dataclasses import replace
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from backtest import run_backtest
from data import load_panel
from factors import Params, composite_scores, factor_matrices

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

CODES = ["512890", "159949", "513100", "518880", "159985", "511260"]
START = "2020-01-01"

BASE = Params.with_window(
    25, weights=(0.3, 0.3, 0.4), threshold=1.5, top_k=2,
    stop_pct=0.13, stop_sell_all=False, stop_cd=5,
    profit_trigger=0.20, profit_stop=0.08)

VARIANTS = {
    "纯浮盈收紧(基准)": BASE,
    "swing箱体": replace(BASE, platform_mode="swing"),
    "prevhigh前高": replace(BASE, platform_mode="prevhigh"),
    "volprofile筹码": replace(BASE, platform_mode="volprofile"),
}


def main():
    panel = load_panel(CODES)
    union = None
    for df in panel.values():
        union = df.index if union is None else union.union(df.index)
    dates = union.sort_values()
    fm = factor_matrices(panel, dates, BASE)
    scores = composite_scores(fm, BASE.weights)

    navs, yearly = {}, {}
    for name, p in VARIANTS.items():
        r = run_backtest(scores, panel, p, start=START)
        navs[name] = r["nav"]
        yearly[name] = r["metrics"]["逐年收益"]
        m = r["metrics"]
        print(f"{name}: 年化{m['年化收益']} 夏普{m['夏普']} "
              f"回撤{m['最大回撤']} 卡玛{m['卡玛']} 调仓{m['调仓次数']}")

    # 逐年收益表
    years = sorted({y for v in yearly.values() for y in v})
    tbl = pd.DataFrame({name: [yearly[name].get(y, "-") for y in years]
                        for name in VARIANTS}, index=years)
    print("\n逐年收益:")
    print(tbl.to_string())

    # 净值 + 回撤图
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    for name, nav in navs.items():
        ax1.plot(nav.index, nav.values, label=name, linewidth=1.2)
        dd = nav / nav.cummax() - 1
        ax2.plot(dd.index, dd.values, linewidth=0.9)
    ax1.set_title("平台止损三方案 vs 纯浮盈收紧（6池，2020-01 起）")
    ax1.set_ylabel("净值")
    ax1.legend()
    ax1.grid(alpha=0.3)
    ax2.set_ylabel("回撤")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    out = Path(__file__).parent / "output" / "platform_nav_compare.png"
    fig.savefig(out, dpi=120)
    print(f"\n已保存: {out}")


if __name__ == "__main__":
    main()
