"""报告输出：终端摘要 + CSV + 净值/回撤图。"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from data import ETF_POOL

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei"]
plt.rcParams["axes.unicode_minus"] = False

OUT_DIR = Path(__file__).parent / "output"


def print_metrics(metrics: dict):
    print("\n===== 回测绩效 =====")
    for k, v in metrics.items():
        if k == "逐年收益":
            print("  逐年收益:")
            for y, r in v.items():
                print(f"    {y}: {r}")
        else:
            print(f"  {k}: {v}")


def save_report(result: dict, tag: str = "backtest"):
    OUT_DIR.mkdir(exist_ok=True)
    nav, trades = result["nav"], result["trades"]
    nav.to_csv(OUT_DIR / f"{tag}_nav.csv", header=True)
    trades.to_csv(OUT_DIR / f"{tag}_trades.csv", index=False)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(nav.index, nav.values, lw=1.2, label="策略净值")
    ax1.set_yscale("log")
    ax1.legend()
    ax1.grid(alpha=0.3)
    ax1.set_title("ETF 多因子动量轮动策略")
    dd = nav / nav.cummax() - 1
    ax2.fill_between(dd.index, dd.values, 0, color="tomato", alpha=0.6)
    ax2.set_ylabel("回撤")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    png = OUT_DIR / f"{tag}_nav.png"
    fig.savefig(png, dpi=120)
    plt.close(fig)
    print(f"\n已保存: {OUT_DIR / (tag + '_nav.csv')}")
    print(f"已保存: {OUT_DIR / (tag + '_trades.csv')}")
    print(f"已保存: {png}")


def print_trades(trades: pd.DataFrame, last_n: int = 10):
    if trades.empty:
        return
    t = trades.copy()
    t["名称"] = t["code"].map(lambda c: ETF_POOL.get(c, ("", c))[1])
    print(f"\n===== 最近 {last_n} 笔交易 =====")
    print(t.tail(last_n).to_string(index=False))
