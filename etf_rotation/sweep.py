"""参数遍历：因子窗口 × 权重 × 调仓阈值 网格回测。

因子矩阵只依赖窗口，按窗口预计算后复用，权重/阈值组合直接走评分+回测。
注意：网格最优存在过拟合风险，结果仅作参考，勿直接照搬最优参数。
"""
from pathlib import Path

import pandas as pd

from backtest import run_backtest
from data import load_panel
from factors import Params, composite_scores, factor_matrices

OUT_DIR = Path(__file__).parent / "output"

WINDOWS = [20, 25, 30]
WEIGHTS = [
    (0.3, 0.3, 0.4),   # 文章默认
    (1 / 3, 1 / 3, 1 / 3),
    (0.4, 0.3, 0.3),
    (0.3, 0.4, 0.3),
    (0.25, 0.25, 0.5),
    (0.5, 0.25, 0.25),
]
THRESHOLDS = [1.2, 1.3, 1.5, 2.0]


def run_sweep() -> pd.DataFrame:
    print("加载数据...")
    panel = load_panel()
    common = None
    for df in panel.values():
        common = df.index if common is None else common.intersection(df.index)
    dates = common.sort_values()

    rows = []
    for w in WINDOWS:
        print(f"预计算因子矩阵 window={w} ...")
        params = Params.with_window(w)
        fm = factor_matrices(panel, dates, params)
        for weights in WEIGHTS:
            scores = composite_scores(fm, weights)
            for th in THRESHOLDS:
                p = Params.with_window(w, weights=weights, threshold=th)
                r = run_backtest(scores, panel, p)
                m = r["metrics"]
                rows.append({
                    "window": w,
                    "weights": "/".join(f"{x:.2f}" for x in weights),
                    "threshold": th,
                    "年化": m["年化收益"], "夏普": m["夏普"],
                    "最大回撤": m["最大回撤"], "卡玛": m["卡玛"],
                    "调仓次数": m["调仓次数"], "累计收益": m["累计收益"],
                })
    df = pd.DataFrame(rows)
    df["_sharpe"] = pd.to_numeric(df["夏普"])
    df = df.sort_values("_sharpe", ascending=False).drop(columns="_sharpe")
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / "sweep_results.csv"
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n共 {len(df)} 组参数，已保存: {out}")
    print("\n夏普 TOP 10（注意过拟合风险）:")
    print(df.head(10).to_string(index=False))
    return df


if __name__ == "__main__":
    run_sweep()
